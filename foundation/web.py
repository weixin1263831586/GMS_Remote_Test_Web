from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


def build_templates(path: Path) -> Jinja2Templates:
    templates = Jinja2Templates(directory=path)
    templates.env.globals['url_for'] = (
        lambda endpoint, filename='': (
            f'/static/{filename}' if endpoint == 'static' else f'/{endpoint}'
        )
    )
    return templates


def build_static_files(path: Path) -> StaticFiles:
    return StaticFiles(directory=path)
