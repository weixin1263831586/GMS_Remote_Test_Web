from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


page_router = APIRouter()


@page_router.get("/redmine-agent", response_class=HTMLResponse)
async def redmine_agent_page():
    ui_dir = Path(__file__).with_name("ui")
    html = (ui_dir / "page.html").read_text(encoding="utf-8")
    html = html.replace(
        "{{REDMINE_CSS}}",
        (ui_dir / "page.css").read_text(encoding="utf-8").rstrip(),
    )
    html = html.replace(
        "{{REDMINE_JS}}",
        (ui_dir / "page.js").read_text(encoding="utf-8").rstrip(),
    )
    return HTMLResponse(html)
