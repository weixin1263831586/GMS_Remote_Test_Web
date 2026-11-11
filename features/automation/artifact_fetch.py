"""Same-origin Jenkins artifact downloads.

Redirects are followed only while every hop stays on the configured Jenkins
origin. ``requests`` default redirect handling would contact the Location
target, including a link-local address, before the caller could reject it.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

import requests


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


def open_jenkins_artifact(
    url: str,
    *,
    configured_url: str,
    auth,
    verify: bool,
    timeout: tuple[int, int] = (20, 3600),
):
    """GET ``url`` without leaving the configured Jenkins origin.

    The returned response is still open. The caller closes it.
    """

    configured = urlparse(configured_url)
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        response = requests.get(
            current,
            auth=auth,
            verify=verify,
            stream=True,
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = str(response.headers.get('Location') or '')
        response.close()
        if not location:
            raise ValueError('Jenkins artifact redirect is missing a location')
        nxt = urljoin(current, location)
        parsed = urlparse(nxt)
        if (
            parsed.scheme != configured.scheme
            or parsed.netloc != configured.netloc
            or parsed.username
            or parsed.password
        ):
            raise ValueError('Jenkins redirected the artifact outside its configured origin')
        current = nxt
    raise ValueError('Jenkins artifact redirect limit exceeded')
