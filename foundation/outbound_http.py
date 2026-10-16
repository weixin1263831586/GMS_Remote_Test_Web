"""Synchronous HTTP streaming with validated DNS addresses and no redirects."""

from __future__ import annotations

import http.client
import socket
import urllib.request

from .outbound import UnsafeOutboundURL, normalized_hostname


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_outbound(request, target, *, timeout=60, context=None):
    """Connect to a prevalidated address while retaining Host and TLS SNI."""
    def connection(host, *, secure=False, **kwargs):
        if secure:
            conn = http.client.HTTPSConnection(host, context=context, **kwargs)
        else:
            conn = http.client.HTTPConnection(host, **kwargs)
        if normalized_hostname(conn.host) != normalized_hostname(target.hostname) or conn.port != target.port:
            raise UnsafeOutboundURL("Connection differs from the authorized target")

        def connect(address, connect_timeout=timeout, source_address=None):
            last_error = None
            for ip in target.addresses:
                try:
                    return socket.create_connection((ip, target.port), connect_timeout, source_address)
                except OSError as exc:
                    last_error = exc
            raise last_error or UnsafeOutboundURL("Authorized target has no addresses")

        conn._create_connection = connect
        return conn

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(connection, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(lambda host, **kw: connection(host, secure=True, **kw), req)

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirects(), HTTPHandler(), HTTPSHandler(),
    )
    return opener.open(request, timeout=timeout)
