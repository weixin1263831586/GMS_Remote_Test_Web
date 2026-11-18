"""Measure the visible animation core across the shell/iframe handoff."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness


GEOMETRY = """root => {
    const box = node => {
        const rect = node.getBoundingClientRect();
        return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2,
            width: rect.width, height: rect.height};
    };
    const select = suffix => root.querySelector(
        '.embedded-frame-skeleton-' + suffix + ', .loading-orbit-' + suffix
    );
    return {bar: box(select('bar')), core: box(select('panel:nth-child(2)')),
        satellites: box(select('cards')), glow: box(select('panels'))};
}"""


class RuntimeLoadingOrbitHandoffTests(RuntimeUiHarness):
    def assert_same_origin(self, geometry, label):
        for layer in ('core', 'satellites', 'glow'):
            for axis in ('x', 'y'):
                self.assertAlmostEqual(
                    geometry[layer][axis], geometry['bar'][axis], delta=0.6,
                    msg=f'{label}: {layer} and ring have different origins: {geometry}',
                )

    def test_four_dashboards_share_viewport_origin_across_loading_handoff(self):
        for width, height in ((1440, 960), (1920, 1080), (900, 700)):
            page = self.new_page()
            held_documents, held_api = [], []
            page.set_viewport_size({'width': width, 'height': height})
            page.add_init_script("sessionStorage.setItem('gms_cluster_active_tab', 'management')")

            def hold_api(route, _request, pending=held_api):
                path = route.request.url.split('?', 1)[0]
                if path.endswith('/api/cluster/status'):
                    payload = {'success': True, 'enabled': True, 'local_worker_id': 'ats-worker-controller'}
                elif path.endswith('/api/gerrit-dashboard/config'):
                    payload = {'success': True, 'data': {'personal_profiles': [
                        {'id': 'demo', 'name': 'Demo', 'owner': 'demo@example.test'},
                    ]}}
                else:
                    pending.append(route)
                    return
                route.fulfill(content_type='application/json', body=json.dumps(payload))

            for domain in ('redmine-agent', 'gerrit-dashboard', 'cluster', 'automation', 'build'):
                page.route(f'**/api/{domain}/**', hold_api)
            try:
                self.goto_shell(page)
                centers = []
                for name, frame_id in (
                    ('redmine-agent', 'redmine-agent-frame'),
                    ('gerrit-dashboard', 'gerrit-dashboard-frame'),
                    ('cluster', 'cluster-frame'),
                    ('automation', 'automation-frame'),
                ):
                    with self.subTest(page=name, width=width, height=height):
                        page.route(f'**/{name}', lambda route, _request, pending=held_documents: pending.append(route))
                        page.evaluate('name => switchPage(name, null)', name)
                        page.wait_for_function(
                            'id => document.getElementById(id).closest(".embedded-frame-shell")'
                            '.querySelector(".embedded-frame-skeleton-bar") !== null', arg=frame_id,
                        )
                        frame_node = page.locator('#' + frame_id)
                        shell = page.locator(f'#{frame_id} >> xpath=..')
                        before = shell.evaluate(GEOMETRY)
                        self.assert_same_origin(before, f'{name} shell')
                        origin = frame_node.bounding_box()
                        expected_x = origin['x'] + origin['width'] / 2
                        self.assertAlmostEqual(before['core']['x'], expected_x, delta=0.6,
                                               msg=f'{name}: shell must use the complete iframe viewport')
                        page.wait_for_timeout(50)
                        self.assertEqual(len(held_documents), 1)
                        held_documents.pop().continue_()
                        frame = frame_node.element_handle().content_frame()
                        if name == 'cluster':
                            # Cluster management uses the compact table
                            # skeleton, not the legacy full-screen orbit.
                            frame.wait_for_selector(
                                '#workers .cluster-loading-message', state='attached'
                            )
                            page.wait_for_function(
                                'id => document.getElementById(id).closest(".embedded-frame-shell")'
                                '.dataset.frameState === "ready"', arg=frame_id,
                            )
                            bounds = frame.evaluate("""() => {
                                const pane = document.querySelector('#tab-management');
                                const workers = document.querySelector('#workers');
                                const label = workers.querySelector('.cluster-loading-message');
                                return {
                                    tabVisible: !pane.hidden,
                                    skeletonHeight: label.getBoundingClientRect().height,
                                    right: workers.getBoundingClientRect().right,
                                    viewport: window.innerWidth,
                                };
                            }""")
                            self.assertTrue(bounds['tabVisible'], bounds)
                            self.assertGreater(bounds['skeletonHeight'], 0, bounds)
                            self.assertLessEqual(bounds['right'], bounds['viewport'] + 1, bounds)
                            page.evaluate("switchPage('test', null)")
                            page.evaluate("switchPage('cluster', null)")
                            frame.wait_for_selector(
                                '#workers .cluster-loading-message', state='attached'
                            )
                            page.evaluate("switchPage('test', null)")
                            continue
                        frame.wait_for_selector('.loading-orbit-bar', state='attached')
                        stage = frame.locator('.loading-orbit').first
                        hidden = stage.evaluate(GEOMETRY)
                        self.assert_same_origin(hidden, f'{name} iframe before reveal')
                        page.wait_for_function(
                            'id => document.getElementById(id).closest(".embedded-frame-shell")'
                            '.dataset.frameState === "ready"', arg=frame_id,
                        )
                        visible = stage.evaluate(GEOMETRY)
                        self.assert_same_origin(visible, f'{name} iframe after reveal')
                        centers.append((name, visible['core']['x'] + origin['x']))
                        self.assertAlmostEqual(centers[-1][1], expected_x, delta=0.6)
                        for layer in ('bar', 'core', 'satellites', 'glow'):
                            for axis in ('x', 'y'):
                                self.assertAlmostEqual(
                                    before[layer][axis], visible[layer][axis] + origin[axis], delta=0.6,
                                    msg=f'{name} {width}x{height}: {layer} jumps at reveal: '
                                    f'{before} -> {visible}, iframe={origin}',
                                )
                                self.assertAlmostEqual(hidden[layer][axis], visible[layer][axis], delta=0.6)
                        page.evaluate("switchPage('test', null)")
                        page.evaluate('name => switchPage(name, null)', name)
                        self.assert_same_origin(stage.evaluate(GEOMETRY), f'{name} after re-entry')
                        page.evaluate("switchPage('test', null)")
                # Three other dashboards share the orbit design; cluster
                # uses the separately verified table skeleton.
                self.assertEqual(len(centers), 3)
                self.assertLessEqual(max(value for _, value in centers) - min(value for _, value in centers), 0.6,
                                     msg=f'Dashboard centers differ at {width}x{height}: {centers}')
            finally:
                for route in held_documents + held_api:
                    try:
                        route.abort()
                    except Exception:
                        pass
                page.close()
