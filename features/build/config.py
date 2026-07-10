from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from features.build.models import BuildServerConfig, BuildTemplateConfig


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


class BuildConfigRepository:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)

    def load(self) -> dict[str, Any]:
        return _read_json(self.config_path)

    def list_servers(self) -> list[dict[str, Any]]:
        data = self.load()
        items = []
        for raw in data.get("servers") or []:
            if not isinstance(raw, dict):
                continue
            try:
                item = BuildServerConfig(**raw).model_dump()
            except Exception:
                continue
            if item["id"]:
                items.append(item)
        return items

    def get_server(self, server_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_servers() if item["id"] == server_id), None)

    def list_templates(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        data = self.load()
        items = []
        for raw in data.get("templates") or []:
            if not isinstance(raw, dict):
                continue
            try:
                item = BuildTemplateConfig(**raw).model_dump()
            except Exception:
                continue
            if item["id"] and (not enabled_only or item["enabled"]):
                items.append(item)
        return items

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_templates() if item["id"] == template_id), None)
