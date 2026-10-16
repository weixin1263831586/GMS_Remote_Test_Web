import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from foundation.outbound import ResolvedOutboundTarget, UnsafeOutboundURL
from foundation.outbound_http import open_outbound


def test_pinned_transport_uses_validated_ip_and_preserves_host():
    hosts = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hosts.append(self.headers['Host'])
            self.send_response(302 if self.path == '/redirect' else 200)
            self.send_header('Location', 'http://127.0.0.1:1/escape')
            self.end_headers()
            self.wfile.write(b'pinned')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://unresolvable.invalid:{server.server_port}'
    target = ResolvedOutboundTarget(origin, 'unresolvable.invalid', server.server_port, ('127.0.0.1',))
    try:
        with open_outbound(urllib.request.Request(origin + '/'), target) as response:
            assert response.read() == b'pinned'
        with pytest.raises(urllib.error.HTTPError) as error:
            open_outbound(urllib.request.Request(origin + '/redirect'), target)
        assert error.value.code == 302
        assert hosts == [f'unresolvable.invalid:{server.server_port}'] * 2
        with pytest.raises(UnsafeOutboundURL):
            open_outbound(urllib.request.Request('http://other.invalid/'), target)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
