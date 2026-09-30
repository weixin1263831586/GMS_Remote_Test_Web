"""Redmine page and explicitly allowlisted UI assets."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from foundation.error_model import ApiError


page_router = APIRouter()
_UI_DIR = Path(__file__).with_name("ui")
PAGE_SCRIPTS = (
    "core.js",
    "settings.js",
    "issues.js",
    "statistics.js",
    "knowledge.js",
    "markdown-table.js",
    "daily-brief-diagnosis.js",
    "daily-brief-session.js",
    "daily-brief-timeline.js",
    "analysis-timeline.js",
    "single-issue.js",
    "daily-brief.js",
    "page.js",
)
_ASSET_TYPES = {
    **{name: "application/javascript" for name in PAGE_SCRIPTS},
    "daily-brief-session.css": "text/css",
}
_LEGACY_ASSETS = frozenset({
    "page.js", "markdown-table.js", "daily-brief-diagnosis.js",
    "daily-brief-session.js", "daily-brief-session.css", "daily-brief-timeline.js",
})


@page_router.get("/redmine-agent", response_class=HTMLResponse)
async def redmine_agent_page():
    html = (_UI_DIR / "page.html").read_text(encoding="utf-8")
    html = html.replace(
        "{{REDMINE_CSS}}",
        (_UI_DIR / "page.css").read_text(encoding="utf-8").rstrip(),
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _asset_response(asset_name: str) -> Response:
    media_type = _ASSET_TYPES.get(asset_name)
    if media_type is None:
        raise ApiError.not_found("UI asset not found")
    return Response(
        (_UI_DIR / asset_name).read_text(encoding="utf-8"),
        media_type=media_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@page_router.get("/redmine-agent/assets/{asset_name}", include_in_schema=False)
def redmine_agent_asset(asset_name: str):
    """Serve only named JS/CSS assets; never expose directories or other sources."""
    return _asset_response(asset_name)


@page_router.get("/redmine-agent/{asset_name}", include_in_schema=False)
def redmine_agent_legacy_asset(asset_name: str):
    """Compatibility for previously served resource URLs."""
    if asset_name not in _LEGACY_ASSETS:
        raise ApiError.not_found("UI asset not found")
    if asset_name == "page.js":
        # Old documents load one page.js. Keep its complete dependency bundle.
        body = "\n;\n".join(
            (_UI_DIR / name).read_text(encoding="utf-8") for name in PAGE_SCRIPTS
            if name == "page.js" or name not in _LEGACY_ASSETS
        )
        return Response(
            body, media_type="application/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return _asset_response(asset_name)
