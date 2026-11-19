"""File search must keep the newest browser query and respect explicit clearing."""

from tests.runtime_ui.harness import RuntimeUiHarness


class SuiteBrowserSearchRaceTests(RuntimeUiHarness):
    def test_new_query_and_clear_invalidate_older_requests(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof searchSuiteFiles === 'function'")
            result = page.evaluate(
                """async () => {
                  const originalSearch = window.searchSuiteFilesInSuite;
                  const originalLocate = window.locateSuiteSearchResult;
                  const originalSuites = _browserSuitesCache;
                  const pending = new Map();
                  window.searchSuiteFilesInSuite = (_suite, query) =>
                    new Promise(resolve => pending.set(query, resolve));
                  window.locateSuiteSearchResult = async item => {
                    window.__lastSuiteSearchTarget = item.path;
                  };
                  _browserSuitesCache = [{
                    tools_path: '/suite/tools', test_type: 'cts',
                    version: '17_r1', worker_id: 'ats-worker-controller'
                  }];
                  const input = document.getElementById('suite-file-search');
                  try {
                    input.value = 'old';
                    const old = searchSuiteFiles();
                    input.value = 'new';
                    const newest = searchSuiteFiles();
                    pending.get('new')([{
                      name: 'new-match', path: 'new-match',
                      suite_label: 'CTS 17_r1', suite_path: '/suite/tools',
                      type: 'file'
                    }]);
                    await newest;
                    pending.get('old')([{
                      name: 'old-match', path: 'old-match',
                      suite_label: 'CTS 17_r1', suite_path: '/suite/tools',
                      type: 'file'
                    }]);
                    await old;
                    const results = document.getElementById('suite-search-results');
                    const newestWins = results.textContent.includes('new-match')
                        && !results.textContent.includes('old-match')
                        && window.__lastSuiteSearchTarget === 'new-match';

                    input.value = 'pending';
                    const third = searchSuiteFiles();
                    clearSuiteFileSearch();
                    pending.get('pending')([{
                      name: 'stale-after-clear', path: 'stale-after-clear',
                      suite_label: 'CTS 17_r1', suite_path: '/suite/tools',
                      type: 'file'
                    }]);
                    await third;
                    return {
                      newestWins,
                      clearWins: results.textContent === ''
                        && results.style.display === 'none'
                        && !results.textContent.includes('stale-after-clear')
                    };
                  } finally {
                    window.searchSuiteFilesInSuite = originalSearch;
                    window.locateSuiteSearchResult = originalLocate;
                    _browserSuitesCache = originalSuites;
                    delete window.__lastSuiteSearchTarget;
                  }
                }"""
            )
            self.assertTrue(result["newestWins"], result)
            self.assertTrue(result["clearWins"], result)
        finally:
            page.close()
