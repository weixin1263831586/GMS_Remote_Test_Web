"""Detail-only Critical Fix matches remain visible across search and navigation."""

from urllib.parse import parse_qs, urlsplit

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeMainlineDetailSearchTests(RuntimeUiHarness):
    def set_critical_filter(self, page, control_id, *values):
        button = page.locator('#' + control_id)
        button.click()
        panel = page.locator('#critical-filter-panel')
        expect(panel).to_be_visible()
        button_box = button.bounding_box()
        panel_box = panel.bounding_box()
        self.assertAlmostEqual(panel_box['x'], button_box['x'], delta=1)
        self.assertAlmostEqual(panel_box['width'], button_box['width'], delta=1)
        geometry = panel.evaluate("""el => ({
            overflowX: getComputedStyle(el).overflowX,
            horizontalOverflow: el.scrollWidth - el.clientWidth
        })""")
        self.assertEqual(geometry['overflowX'], 'hidden')
        self.assertLessEqual(geometry['horizontalOverflow'], 1)
        panel.locator('.critical-filter-clear').click()
        for value in values:
            panel.locator(f'input[value="{value}"]').check()
        button.click()
        expect(panel).to_be_hidden()

    def test_detail_matches_are_visible_and_escaped_after_repeated_navigation(self):
        page = self.new_page()
        page.set_viewport_size({'width': 1792, 'height': 856})
        errors, mutations = [], []
        state = {'complete': True}
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: mutations.append(request.method)
                if '/api/gms-update-center/' in request.url and request.method != 'GET' else None)

        def respond(route):
            path = urlsplit(route.request.url).path
            if path.endswith('/summary'):
                data = {'total': 0, 'by_type': []}
            elif path.endswith('/critical-patches/gerrit'):
                data = {'configured': True, 'available': True, 'error': '', 'items': {
                    'A-568181732': {
                        'status': 'missing', 'basis': 'commit', 'upstream_total': 2,
                        'upstream_merged': 0, 'upstream_open': 0, 'truncated': False,
                        'changes': [], 'change_count': 0,
                        'versions': [
                            {'version': '16', 'status': 'missing', 'upstream_total': 2, 'upstream_merged': 0},
                            {'version': '17', 'status': 'merged', 'upstream_total': 1, 'upstream_merged': 1},
                        ],
                    },
                }}
            elif path.endswith('/critical-patches'):
                data = {'available': True, 'coverage_complete': state['complete'], 'total': 1, 'items': [{
                    'patch_id': 'A-568181732', 'description': 'Bluetooth correction',
                    'year': '2026', 'bulletin_month': 'November', 'deadline': 'March 26, 2027',
                    'patch_kind': 'strongly_recommended',
                    'android_versions': 'Android 16, Android 16 QPR2, Android 17',
                    'detail_excerpt': 'CVE-2026-58835 <img src=x onerror="window.evidenceInjected=true">',
                    'detail_status': 'ready' if state['complete'] else 'stale',
                    'bulletin_url': 'https://example.com/official#568181732',
                }]}
            else:
                data = {'total': 0, 'items': []}
            route.fulfill(json={'success': True, **data})

        page.route('**/api/gms-update-center/exemptions*', respond)
        page.route('**/api/gms-update-center/critical-patches*', respond)
        page.route('**/api/gms-update-center/critical-patches/**', respond)
        page.route('**/api/gms-update-center/security-patches*', respond)
        page.route('**/api/gms-update-center/exemptions/**', respond)
        try:
            for index in range(3):
                state['complete'] = index != 1
                page.goto(f'{self.base_url}/gms-update-center?tab=critical')
                expect(page.locator('#tables-area')).to_contain_text('A-568181732')
                expect(page.locator('#gerrit_status')).to_be_visible()
                expect(page.locator('.critical-version')).to_have_text('16, 16 QPR2, 17')
                expect(page.locator('.critical-kind')).to_have_text('强烈推荐')
                expect(page.locator('.critical-matrix-table thead tr')).to_have_count(1)
                expect(page.locator('.critical-matrix-table thead th').nth(8)).to_have_text('Gerrit合入')
                expect(page.locator('.critical-version-label')).to_have_text(['16', '16 QPR2', '17'])
                expect(page.locator('.critical-matrix-table thead th').last).to_have_text('来源')
                cells = page.locator('.critical-matrix-table tbody tr').first.locator('td')
                expect(cells.nth(0)).to_have_text('2026')
                expect(cells.nth(1)).to_have_text('November')
                expect(cells.nth(2)).to_have_text('2027-03-26')
                expect(cells.nth(3)).to_have_text('A-568181732')
                expect(page.locator('.gerrit-cell')).to_contain_text('未合入')
                expect(page.locator('td[data-version="16"]')).to_contain_text('0/2')
                expect(page.locator('td[data-version="16 QPR2"]')).to_contain_text('未能核对')
                expect(page.locator('td[data-version="17"]')).to_contain_text('1/1')
                page.locator('.gerrit-cell button').click()
                expect(page.locator('#critical-detail-dialog')).to_be_visible()
                expect(page.locator('#critical-detail-body')).to_contain_text('CVE-2026-58835')
                expect(page.locator('#critical-detail-body img')).to_have_count(0)
                page.locator('#critical-detail-done').click()
                expect(page.locator('#critical-detail-dialog')).to_be_hidden()
                self.set_critical_filter(page, 'critical_version', '17')
                expect(page.locator('.critical-version-label')).to_have_text(['17'])
                self.set_critical_filter(page, 'critical_version')
                self.set_critical_filter(page, 'gerrit_status', 'merged')
                expect(page.locator('#tables-area tr', has_text='A-568181732')).to_have_count(0)
                self.set_critical_filter(page, 'gerrit_status')
                expect(page.locator('#tables-area tr', has_text='A-568181732')).to_be_visible()
                page.locator('#gerrit-refresh').click()
                expect(page.locator('.gerrit-cell')).to_contain_text('未合入')
                page.locator('#q').fill('CVE-2026-58835')
                page.locator('#q').press('Enter')
                expect(page.locator('#section-badge')).to_contain_text('全局搜索')
                page.locator('#tables-area details summary').click()
                expect(page.locator('#tables-area')).to_contain_text('CVE-2026-58835')
                expect(page.locator('#tables-area img')).to_have_count(0)
                self.assertFalse(page.evaluate('Boolean(window.evidenceInjected)'))
                if not state['complete']:
                    expect(page.locator('#pageinfo')).to_contain_text('详情未完整同步')
                page.locator('#q').fill('')
                page.locator('#q').press('Enter')
                page.locator('[data-tab="exemptions"]').click()
                expect(page.locator('#gerrit_status')).to_be_hidden()
                expect(page.locator('#gerrit-refresh')).to_be_hidden()
                page.locator('[data-tab="critical"]').click()
                expect(page.locator('#tables-area')).to_contain_text('A-568181732')
            self.assertEqual(mutations, [])
            self.assert_no_page_errors(errors)
        finally:
            page.close()


    def test_version_matrix_filters_all_pages_and_distinguishes_not_applicable(self):
        page = self.new_page()
        page.set_viewport_size({'width': 1792, 'height': 856})
        records = [
            {'item_key': f'row-{i}', 'patch_id': f'A-{100000+i}', 'year': '2026',
             'bulletin_month': 'November', 'description': 'Synthetic fix',
             'patch_kind': 'mandatory', 'android_versions': '12, 13, 17' if i < 100 else '16, 16 QPR2, 17',
             'detail_status': 'ready'}
            for i in range(101)
        ]
        records[-1].update(year='2025', bulletin_month='May', patch_kind='strongly_recommended')
        statuses = {r['item_key']: {'status': 'missing', 'versions': [
            {'version': '17', 'status': 'missing', 'upstream_total': 1, 'upstream_merged': 0},
        ]} for r in records}
        statuses['row-100'] = {'status': 'partial', 'versions': [
            {'version': '16', 'status': 'merged', 'upstream_total': 1, 'upstream_merged': 1},
            {'version': '17', 'status': 'merged', 'upstream_total': 1, 'upstream_merged': 1},
        ]}

        def respond(route):
            url = urlsplit(route.request.url)
            if url.path.endswith('/summary'):
                data = {'total': 0, 'by_type': []}
            elif url.path.endswith('/critical-patches/gerrit'):
                data = {'configured': True, 'items': statuses, 'error': ''}
            elif url.path.endswith('/critical-patches'):
                query = parse_qs(url.query)
                start = int(query.get('offset', ['0'])[0])
                limit = int(query.get('limit', ['100'])[0])
                data = {'available': True, 'coverage_complete': True, 'total': len(records),
                        'items': records[start:start+limit]}
            else:
                data = {'total': 0, 'items': []}
            route.fulfill(json={'success': True, **data})

        page.route('**/api/gms-update-center/exemptions*', respond)
        page.route('**/api/gms-update-center/critical-patches*', respond)
        page.route('**/api/gms-update-center/critical-patches/**', respond)
        page.route('**/api/gms-update-center/security-patches*', respond)
        page.route('**/api/gms-update-center/exemptions/**', respond)
        try:
            page.goto(f'{self.base_url}/gms-update-center?tab=critical')
            expect(page.locator('#gerrit_status')).to_be_enabled()
            expect(page.locator('#pageinfo')).to_have_text('1-100 / 101')
            expect(page.locator('#section-badge')).to_be_hidden()
            expect(page.locator('.critical-matrix-table thead tr')).to_have_count(1)
            expect(page.locator('.critical-matrix-table thead select')).to_have_count(0)
            expect(page.locator('#gerrit_status')).to_be_visible()
            expect(page.locator('.filters #critical-legacy-toggle')).to_be_visible()
            toggle_box = page.locator('#critical-legacy-toggle').bounding_box()
            query_box = page.locator('[data-click="runQuery"]').bounding_box()
            self.assertAlmostEqual(toggle_box['x'] - query_box['x'] - query_box['width'], 8, delta=1)
            self.assertAlmostEqual(toggle_box['x'] + toggle_box['width'], 1792 - 8, delta=1)
            toolbar = page.evaluate("""() => {
                const controls = [...document.querySelectorAll('.mainline-toolbar .filters > *, .mainline-toolbar .tabs button, #critical-legacy-toggle')]
                    .filter(el => el.getClientRects().length).map(el => el.getBoundingClientRect());
                const search = document.querySelector('#q').getBoundingClientRect();
                const wrapper = document.querySelector('.mainline-toolbar');
                return {centerSpread: Math.max(...controls.map(r=>r.top+r.height/2)) -
                        Math.min(...controls.map(r=>r.top+r.height/2)), searchWidth:search.width,
                        overflow:wrapper.scrollWidth-wrapper.clientWidth};
            }""")
            # Eight tabs may require a second toolbar row; no horizontal overflow is allowed.
            self.assertLessEqual(toolbar['searchWidth'], 240)
            self.assertLessEqual(toolbar['overflow'], 1)
            tabs_box = page.locator('.mainline-toolbar > .tabs').bounding_box()
            search_box = page.locator('#q').bounding_box()
            self.assertAlmostEqual(tabs_box['x'], 8, delta=1)
            self.assertGreaterEqual(search_box['x'], 7)
            for tab in ('exemptions', 'patch', 'critical'):
                page.locator(f'[data-tab="{tab}"]').click()
                expect(page.locator(f'[data-tab="{tab}"]')).to_have_class('active')
                current = page.locator('.mainline-toolbar > .tabs').bounding_box()
                self.assertAlmostEqual(current['x'], tabs_box['x'], delta=1)
                last = page.locator('#critical-legacy-toggle' if tab == 'critical' else '[data-click="runQuery"]').bounding_box()
                self.assertAlmostEqual(last['x'] + last['width'], 1792 - 8, delta=1)
                if tab == 'critical':
                    expect(page.locator('#gerrit_status')).to_be_enabled()
                expect(page.locator('#section-badge')).to_be_hidden()
            headers = page.locator('.critical-version-label')
            expect(headers).to_have_text(['16', '16 QPR2', '17'])
            page.locator('#critical-legacy-toggle').click()
            expect(headers).to_have_text(['12', '13', '16', '16 QPR2', '17'])
            expect(page.locator('#pageinfo')).to_have_text('1-100 / 101')
            expect(page.locator('#section-badge')).to_be_hidden()
            page.locator('#critical-legacy-toggle').click()
            expect(headers).to_have_text(['16', '16 QPR2', '17'])
            self.set_critical_filter(page, 'critical_version', '12')
            expect(headers).to_have_text(['12'])
            expect(page.locator('#critical-legacy-toggle')).to_be_disabled()
            self.set_critical_filter(page, 'critical_version')
            expect(headers).to_have_text(['16', '16 QPR2', '17'])
            self.set_critical_filter(page, 'gerrit_status', 'partial')
            expect(page.locator('#pageinfo')).to_have_text('1-1 / 1')
            expect(page.locator('#tables-area')).to_contain_text('A-100100')
            self.set_critical_filter(page, 'gerrit_status')
            self.set_critical_filter(page, 'critical_version', '16')
            self.set_critical_filter(page, 'gerrit_status', 'merged')
            expect(page.locator('#pageinfo')).to_have_text('1-1 / 1')
            expect(page.locator('td[data-version="16"]')).to_contain_text('已合入')
            self.set_critical_filter(page, 'gerrit_status')
            self.set_critical_filter(page, 'critical_version', '16 QPR2')
            expect(page.locator('td[data-version="16 QPR2"]')).to_contain_text('未能核对')
            self.set_critical_filter(page, 'critical_version', '12', '16')
            expect(headers).to_have_text(['12', '16'])
            expect(page.locator('#pageinfo')).to_have_text('1-100 / 101')
            self.set_critical_filter(page, 'critical_year', '2025', '2026')
            self.set_critical_filter(page, 'critical_month', 'May', 'November')
            self.set_critical_filter(page, 'critical_kind', 'mandatory', 'strongly_recommended')
            self.set_critical_filter(page, 'gerrit_status', 'merged', 'missing')
            # Version 12 is unknown, not missing; only the applicable version 16 is merged.
            expect(page.locator('#pageinfo')).to_have_text('1-1 / 1')
            expect(page.locator('#tables-area')).to_contain_text('A-100100')
            self.set_critical_filter(page, 'gerrit_status', 'merged', 'unknown')
            expect(page.locator('#pageinfo')).to_have_text('1-100 / 101')
            self.set_critical_filter(page, 'critical_year', '2025')
            expect(page.locator('#pageinfo')).to_have_text('1-1 / 1')
            self.set_critical_filter(page, 'critical_month', 'November')
            expect(page.locator('#pageinfo')).to_have_text('0 / 0')
            self.set_critical_filter(page, 'critical_month')
            self.set_critical_filter(page, 'critical_kind', 'mandatory')
            expect(page.locator('#pageinfo')).to_have_text('0 / 0')
            self.set_critical_filter(page, 'critical_kind')
            page.locator('#gerrit-refresh').click()
            expect(page.locator('#pageinfo')).to_have_text('1-1 / 1')
            expect(page.locator('#critical_version')).to_have_text('已选 2 项')
            page.locator('#critical_version').click()
            page.keyboard.press('Escape')
            expect(page.locator('#critical-filter-panel')).to_be_hidden()
        finally:
            page.close()


    def test_all_mainline_tabs_share_table_and_pager_geometry(self):
        page = self.new_page()
        page.set_viewport_size({'width': 1792, 'height': 856})

        def respond(route):
            path = urlsplit(route.request.url).path
            if path.endswith('/summary'):
                data = {'total': 0, 'by_type': []}
            elif path.endswith('/critical-patches/gerrit'):
                data = {'configured': True, 'items': {}, 'error': ''}
            elif path.endswith('/critical-patches'):
                data = {'total': 1, 'coverage_complete': True, 'items': [{
                    'item_key': 'geometry', 'patch_id': 'A-123456789', 'year': '2026',
                    'bulletin_month': 'November', 'description': 'Geometry sample',
                    'android_versions': '16, 17', 'patch_kind': 'mandatory',
                }]}
            elif path.endswith('/security-patches'):
                data = {'total': 1, 'items': [{'cve_id': 'CVE-2026-12345'}]}
            else:
                data = {'total': 1, 'items': [{'exemption_id': '123456789'}]}
            route.fulfill(json={'success': True, **data})

        page.route('**/api/gms-update-center/exemptions*', respond)
        page.route('**/api/gms-update-center/critical-patches*', respond)
        page.route('**/api/gms-update-center/critical-patches/**', respond)
        page.route('**/api/gms-update-center/security-patches*', respond)
        page.route('**/api/gms-update-center/exemptions/**', respond)
        try:
            page.goto(f'{self.base_url}/gms-update-center?tab=exemptions')
            for viewport in ({'width': 1792, 'height': 856}, {'width': 1440, 'height': 900}):
                page.set_viewport_size(viewport)
                first = None
                for tab in ('exemptions', 'patch', 'critical'):
                    page.locator(f'[data-tab="{tab}"]').click()
                    expect(page.locator('.table-split')).to_be_visible()
                    expect(page.locator('.table-head-table thead')).to_contain_text(
                        {'exemptions': '豁免ID', 'patch': 'CVE 编号', 'critical': 'Gerrit合入'}[tab]
                    )
                    expect(page.locator('#pageinfo')).to_have_text('1-1 / 1')
                    if tab == 'critical':
                        expect(page.locator('.critical-matrix-table')).to_be_visible()
                        expect(page.locator('#gerrit_status')).to_be_enabled()
                    expect(page.locator('#section-badge')).to_be_hidden()
                    geometry = page.evaluate("""() => {
                        const t = document.querySelector('.table-split').getBoundingClientRect();
                        const p = document.querySelector('.pager').getBoundingClientRect();
                        const edge = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ui-page-edge'));
                        return {left:t.left, right:innerWidth-t.right, top:t.top, bottom:t.bottom,
                                gap:p.top-t.bottom, pagerRight:innerWidth-p.right,
                                pagerBottom:innerHeight-p.bottom, pagerHeight:p.height, edge};
                    }""")
                    for key in ('left', 'right', 'gap', 'pagerRight', 'pagerBottom'):
                        self.assertAlmostEqual(geometry[key], geometry['edge'], delta=1, msg=f'{tab}: {key}')
                    if first is None:
                        first = geometry
                    else:
                        for key in ('bottom', 'pagerHeight'):
                            self.assertAlmostEqual(geometry[key], first[key], delta=1, msg=f'{tab}: {key}')
        finally:
            page.close()


    def test_gerrit_loading_reuses_refresh_button_without_shifting_table(self):
        page = self.new_page()
        page.set_viewport_size({'width': 1792, 'height': 856})
        pending = []
        record = {'item_key': 'synthetic', 'patch_id': 'A-123456789', 'year': '2026',
                  'bulletin_month': 'November', 'description': 'Synthetic fix',
                  'android_versions': '16, 17', 'patch_kind': 'mandatory', 'detail_status': 'ready'}
        result = {'configured': True, 'items': {'synthetic': {
            'status': 'merged', 'upstream_total': 2, 'upstream_merged': 2,
            'versions': [
                {'version': version, 'status': 'merged', 'upstream_total': 1, 'upstream_merged': 1}
                for version in ('16', '17')
            ],
        }}}

        def respond(route):
            path = urlsplit(route.request.url).path
            if path.endswith('/critical-patches/gerrit'):
                pending.append(route)
                return
            if path.endswith('/critical-patches'):
                data = {'total': 1, 'items': [record], 'coverage_complete': True}
            else:
                data = {'total': 0, 'by_type': [], 'items': []}
            route.fulfill(json={'success': True, **data})

        page.route('**/api/gms-update-center/exemptions*', respond)
        page.route('**/api/gms-update-center/critical-patches*', respond)
        page.route('**/api/gms-update-center/critical-patches/**', respond)
        page.route('**/api/gms-update-center/security-patches*', respond)
        page.route('**/api/gms-update-center/exemptions/**', respond)
        try:
            page.goto(f'{self.base_url}/gms-update-center?tab=critical')
            refresh = page.locator('#gerrit-refresh')
            table = page.locator('.critical-matrix-table')
            expect(table).to_be_visible()
            expect(refresh).to_have_text('核对中…')
            expect(refresh).to_be_disabled()
            expect(page.locator('#section-badge')).to_be_hidden()
            expect(page.locator('.gerrit-cell')).to_contain_text('待核对')
            before = table.bounding_box()
            row_before = table.locator('tbody tr').first.bounding_box()
            button_before = refresh.bounding_box()
            self.assertTrue(pending)
            pending.pop(0).fulfill(json={'success': True, **result})
            expect(refresh).to_have_text('重新核对')
            expect(refresh).to_be_enabled()
            expect(page.locator('.gerrit-cell')).to_contain_text('已合入')
            expect(page.locator('#section-badge')).to_be_hidden()
            for key in ('x', 'y', 'width', 'height'):
                self.assertAlmostEqual(table.bounding_box()[key], before[key], delta=1, msg=key)
                self.assertAlmostEqual(table.locator('tbody tr').first.bounding_box()[key], row_before[key], delta=1, msg=key)
                self.assertAlmostEqual(refresh.bounding_box()[key], button_before[key], delta=1, msg=key)
            refresh.click()
            expect(refresh).to_have_text('核对中…')
            expect(page.locator('.gerrit-cell')).to_contain_text('已合入')
            expect(page.locator('#section-badge')).to_be_hidden()
            pending.pop(0).fulfill(status=503, json={'success': False, 'error': 'Synthetic failure'})
            expect(refresh).to_have_text('重新核对')
            expect(page.locator('.gerrit-cell')).to_contain_text('已合入')
            expect(page.locator('#section-badge')).to_contain_text('保留上次核对结果')
        finally:
            page.close()


    def test_update_center_change_details_and_filters(self):
        page = self.new_page()
        page.set_viewport_size({'width': 1792, 'height': 856})
        queries = []

        def respond(route):
            url = urlsplit(route.request.url)
            queries.append((url.path, parse_qs(url.query)))
            if url.path.endswith('/changes'):
                data = {'data': {'items': [{
                    'source_key': 'gms_bulletins', 'entity_type': 'gms_bulletin',
                    'entity_key': 'synthetic', 'change_type': 'changed',
                    'before': {'patch_id': 'A-123456789', 'description': 'Original'},
                    'after': {'patch_id': 'A-123456789', 'description': 'Updated <img src=x onerror=alert(1)>'},
                    'detected_at': '2026-11-24T10:00:00+00:00',
                }]}, 'meta': {'total': 1}}
            elif url.path.endswith('/security-patches'):
                data = {'items': [{'cve_id': 'CVE-2026-12345', 'mainline_module': 'NFC',
                                   'version_note': 'Complete synthetic note'}], 'total': 1}
            else:
                data = {'data': {'items': []}, 'meta': {'total': 0}, 'items': [], 'total': 0}
            route.fulfill(json={'success': True, **data})

        page.route('**/api/gms-update-center/**', respond)
        page.route('**/api/gms-update-center/critical-patches*', respond)
        page.route('**/api/gms-update-center/critical-patches/**', respond)
        page.route('**/api/gms-update-center/security-patches*', respond)
        page.route('**/api/gms-update-center/exemptions/**', respond)
        try:
            page.goto(f'{self.base_url}/gms-update-center?tab=changes')
            expect(page.locator('#tables-area')).to_contain_text('A-123456789')
            expect(page.locator('#tables-area')).not_to_contain_text('content_hash')
            expect(page.locator('.pager [data-a0="-1"]')).to_be_disabled()
            expect(page.locator('.pager [data-a0="1"]')).to_be_disabled()
            page.locator('#tables-area button', has_text='查看').click()
            expect(page.locator('#critical-detail-body')).to_contain_text('变更前：')
            expect(page.locator('#critical-detail-body')).to_contain_text('Original')
            expect(page.locator('#critical-detail-body')).to_contain_text('Updated')
            expect(page.locator('#critical-detail-body img')).to_have_count(0)
            expect(page.locator('#critical-detail-copy')).to_be_visible()
            page.locator('#critical-detail-done').click()
            page.locator('[data-tab="requirements"]').click()
            page.locator('#monitor_version').select_option('Android 16')
            page.locator('#requirement_kind').select_option('changed')
            expect(page.locator('#pageinfo')).to_have_text('0 / 0')
            page.wait_for_function("document.querySelector('#requirement_kind').value === 'changed'")
            page.locator('[data-tab="patch"]').click()
            page.locator('#cve_year').fill('2026')
            page.locator('#cve_module').fill('NFC')
            with page.expect_response(lambda response: '/security-patches?' in response.url
                                      and 'release_year=2026' in response.url
                                      and 'mainline_module=NFC' in response.url):
                page.locator('#cve_module').press('Enter')
            expect(page.locator('#tables-area')).to_contain_text('CVE-2026-12345')
            expect(page.locator('#tables-area button', has_text='Complete synthetic note')).to_be_visible()
            page.locator('#tables-area button', has_text='Complete synthetic note').click()
            expect(page.locator('#critical-detail-body')).to_contain_text('Complete synthetic note')
            self.assertTrue(any(q.get('release_year') == ['2026'] and q.get('mainline_module') == ['NFC'] for _, q in queries))
        finally:
            page.close()
