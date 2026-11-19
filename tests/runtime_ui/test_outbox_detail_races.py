"""Historical-mail details must not display an older response after selection changes."""

from tests.runtime_ui.harness import RuntimeUiHarness


class OutboxDetailRacesTests(RuntimeUiHarness):
    def test_newest_selection_wins_and_close_invalidates_pending_response(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof showOutboxDetail === 'function'")
            result = page.evaluate(
                """async () => {
                  const oldApi = window.apiCall;
                  const pending = new Map();
                  window.apiCall = (url, ...args) =>
                    url.startsWith('/api/email/outbox/')
                      ? new Promise(resolve => pending.set(url, resolve))
                      : oldApi(url, ...args);
                  try {
                    const first = showOutboxDetail('first');
                    const second = showOutboxDetail('second');
                    pending.get('/api/email/outbox/second')({
                      data: {id: 'second', subject: 'Latest mail'}
                    });
                    await second;
                    pending.get('/api/email/outbox/first')({
                      data: {id: 'first', subject: 'Outdated mail'}
                    });
                    await first;
                    const body = document.getElementById('outbox-detail-body');
                    const newestWins = body.textContent.includes('Latest mail')
                      && !body.textContent.includes('Outdated mail');

                    const pendingClose = showOutboxDetail('closed');
                    closeOutboxDetailModal();
                    pending.get('/api/email/outbox/closed')({
                      data: {id: 'closed', subject: 'Should not render'}
                    });
                    await pendingClose;
                    return {
                      newestWins,
                      closeWins: !body.textContent.includes('Should not render'),
                    };
                  } finally {
                    window.apiCall = oldApi;
                  }
                }"""
            )
            self.assertTrue(result["newestWins"], result)
            self.assertTrue(result["closeWins"], result)
        finally:
            page.close()
