"""Origin-pinned urllib transport for Controller credentials and packages."""

from __future__ import annotations

import ssl
import urllib.request


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request, *, timeout=60, context=None):
    handlers = [NoRedirects()]
    if isinstance(context, ssl.SSLContext):
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)
