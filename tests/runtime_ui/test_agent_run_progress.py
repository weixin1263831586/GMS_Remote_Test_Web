"""Completed workflow cards must render terminal status without a false wait."""

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAgentRunProgressTests(RuntimeUiHarness):
    def test_agent_times_use_beijing_time_in_any_browser_timezone(self):
        page = self.new_page()
        try:
            devtools = page.context.new_cdp_session(page)
            devtools.send('Emulation.setTimezoneOverride', {'timezoneId': 'America/Los_Angeles'})
            self.goto_shell(page)
            result = page.evaluate("""() => {
                const timestamp = '2026-11-10T07:47:15.123456Z';
                renderAgentMessages({messages: [{role: 'assistant', content: '工作流已完成',
                    created_at: timestamp}]});
                renderAgentSteps({status: 'done', steps: [{title: 'ATS 工作流结束', status: 'done',
                    data: {automation_run_id: 'ats_time', progress: {status: 'completed', updated_at: timestamp}}
                }]});
                return {
                    message: document.querySelector('.agent-time').textContent,
                    run: document.querySelector('.agent-run-stage-time').textContent,
                    legacy: formatAgentTime('2026-11-10T15:47:15'),
                    offset: formatAgentTime('2026-11-10T15:47:15+08:00'),
                    midnight: formatAgentTime('2026-11-10T16:00:00Z'),
                    empty: formatAgentTime(''), invalid: formatAgentTime('invalid'),
                };
            }""")
            assert result == {
                'message': '15:47:15', 'run': '15:47:15', 'legacy': '15:47:15',
                'offset': '15:47:15', 'midnight': '00:00:00', 'empty': '', 'invalid': '',
            }
        finally:
            page.close()

    def test_download_card_updates_bytes_speed_and_percent(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate("""() => {
                const progress = {status: 'artifact_fetching', artifact_download: {
                    total_bytes: 4 * 1024**3, downloaded_bytes: 1024**3, bytes_per_second: 20 * 1024**2,
                }};
                const render = () => renderAgentSteps({status: 'running', steps: [{
                    title: '创建 ATS 工作流', status: 'running',
                    data: {automation_run_id: 'ats_download', progress},
                }]});
                render();
                const before = document.getElementById('agent-steps').textContent;
                progress.artifact_download.downloaded_bytes = 2 * 1024**3;
                render();
                const steps = document.getElementById('agent-steps');
                return {before, after: steps.textContent, bars: steps.querySelectorAll('progress').length};
            }""")
            assert '固件下载中' in result['before']
            assert '25.0%' in result['before']
            assert '50.0%' in result['after']
            assert '2 GB / 4 GB' in result['after']
            assert '20 MB/s' in result['after']
            assert result['bars'] == 1
        finally:
            page.close()

    def test_ats_download_progress_survives_repeated_navigation(self):
        page = self.new_page()
        try:
            for _ in range(2):
                page.goto(self.base_url + '/automation?tab=runs', wait_until='domcontentloaded')
                page.wait_for_function("document.body.dataset.automationReady === 'true'")
                result = page.evaluate("""() => {
                    const download = {total_bytes: 4 * 1024**3, downloaded_bytes: 2 * 1024**3,
                        bytes_per_second: 20 * 1024**2};
                    renderLiveProgress({artifact_download: download});
                    return {text: document.getElementById('automation-live-progress').textContent,
                        label: statusLabel('artifact_fetching'),
                        bars: document.querySelectorAll('#automation-live-progress progress').length,
                        unsafe: renderArtifactDownload({total_bytes: '<script>', downloaded_bytes: 1})};
                }""")
                assert result['label'] == '固件下载中'
                assert '50.0%' in result['text']
                assert '2.00 GiB / 4.00 GiB' in result['text']
                assert '20.0 MiB/s' in result['text']
                assert result['bars'] == 1
                assert result['unsafe'] == ''
                self.goto_shell(page)
        finally:
            page.close()

    def test_terminal_card_with_legacy_status_does_not_wait_for_reporting(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            text = page.evaluate("""() => {
                renderAgentSteps({status: 'error', steps: [{
                    title: 'ATS 工作流结束', status: 'error', detail: '固件缺失',
                    data: {automation_run_id: 'ats_test', status: 'flash_failed'},
                }]});
                return document.getElementById('agent-steps').textContent;
            }""")
            assert '刷机失败' in text
            assert '等待状态上报' not in text
            assert '刷机失败：失败' not in text
        finally:
            page.close()

    def test_failed_run_reason_is_visible_and_escaped(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate("""() => {
                renderAgentSteps({status: 'error', steps: [{
                    title: '创建 ATS 工作流', status: 'error',
                    detail: '原因：<script>unsafe</script> missing image',
                    data: {automation_run_id: 'ats_test', progress: {status: 'flash_failed'}},
                }]});
                const steps = document.getElementById('agent-steps');
                return {text: steps.textContent, scripts: steps.querySelectorAll('script').length};
            }""")
            assert 'missing image' in result['text']
            assert result['scripts'] == 0
        finally:
            page.close()
