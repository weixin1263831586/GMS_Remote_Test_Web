from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response


page_router = APIRouter()


@page_router.get("/redmine-agent", response_class=HTMLResponse)
async def redmine_agent_page():
    ui_dir = Path(__file__).with_name("ui")
    html = (ui_dir / "page.html").read_text(encoding="utf-8")
    html = html.replace(
        "{{REDMINE_CSS}}",
        (ui_dir / "page.css").read_text(encoding="utf-8").rstrip(),
    )
    # CSS is embedded in this iframe page.  Do not let a previously opened
    # Redmine frame retain an older embedded stylesheet after a UI deploy.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@page_router.get("/redmine-agent/page.js")
def redmine_agent_page_js():
    """页面脚本走静态资源（CSP 收紧后禁止 inline <script>）。"""
    js = Path(__file__).with_name("ui") / "page.js"
    return Response(
        js.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )
