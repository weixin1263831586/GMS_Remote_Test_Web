"""Keep public architecture views aligned with the canonical topology."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CANONICAL = (ROOT / "docs/architecture/platform-topology.mmd").read_text(
    encoding="utf-8"
).strip()


def _first_mermaid(path: str) -> str:
    content = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(r"```mermaid\n(.*?)\n```", content, re.DOTALL)
    assert match, f"missing Mermaid topology in {path}"
    return match.group(1).strip()


def test_markdown_topologies_match_canonical_definition():
    assert _first_mermaid("README.md") == CANONICAL
    assert _first_mermaid("docs/architecture/overview.md") == CANONICAL


def test_web_architecture_covers_canonical_roles_and_flows():
    content = (ROOT / "web/templates/architecture.html").read_text(encoding="utf-8")
    for label in (
        "FastAPI Controller",
        "Agent Runtime",
        "Linux Worker A",
        "Linux Worker B",
        "Windows USB Source",
        "Linux USB Source",
        "Heartbeat / Poll / ACK",
        "命令随 Poll 响应返回",
        "USB/IP 接入示例",
        "生产入口为 HTTPS",
    ):
        assert label in content
    assert "http://&lt;test_host&gt;:5001" not in content
