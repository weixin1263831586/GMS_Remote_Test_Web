"""noVNC URL construction shared by the system desktop and device screens."""

from __future__ import annotations


NOVNC_WEB_PORT = 6080


def novnc_url(
    host: str,
    *,
    web_port: int = NOVNC_WEB_PORT,
    autoconnect: bool = True,
    resize: str = 'scale',
) -> str:
    """Return a noVNC URL with autoconnect and scaling mode preset.

    The *resize* query parameter maps to noVNC's 'resize' setting:
      - 'off'     -> no scaling
      - 'scale'   -> local scaling
      - 'remote'  -> remote resizing
    """
    params = []
    if autoconnect:
        params.append('autoconnect=true')
    if resize:
        params.append(f'resize={resize}')
    if params:
        return f'http://{host}:{web_port}/vnc.html?{"&".join(params)}'
    return f'http://{host}:{web_port}/vnc.html'
