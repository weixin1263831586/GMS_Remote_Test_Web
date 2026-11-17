"""Summarize Actions modal-by-modal screenshots without concealing missing cases."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from tests.runtime_ui.test_modal_inventory_runner import (
    SHELL_CASES,
    VIEWPORTS,
    WORKSPACE_CASES,
)


def build_report(directory: Path) -> tuple[int, int]:
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "summary.json":
            continue
        try:
            entries.append(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            entries.append({"key": path.stem, "status": "failed", "error": str(exc)})
    expected = {
        f"{area}--{identifier}--{viewport['width']}x{viewport['height']}"
        for area, identifier in (
            [("shell", identifier) for _, identifier in SHELL_CASES]
            + [(area, identifier) for area, _, _, identifier in WORKSPACE_CASES]
        )
        for viewport in VIEWPORTS
    }
    by_key = {entry.get("key"): entry for entry in entries}
    for key in sorted(expected - set(by_key)):
        by_key[key] = {
            "key": key,
            "status": "missing",
            "error": "Actions runner did not record this required modal/viewport",
        }
    failures = [entry for entry in by_key.values() if entry.get("status") != "passed"]
    summary = {
        "expected": len(expected),
        "recorded": len(entries),
        "passed": sum(item.get("status") == "passed" for item in by_key.values()),
        "failed_or_missing": len(failures),
        "errors": [
            {"key": item.get("key"), "error": item.get("error", "unknown")}
            for item in sorted(failures, key=lambda x: x.get("key", ""))
        ],
        "cases": [by_key[key] for key in sorted(by_key)],
    }
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cards = []
    for entry in summary["cases"]:
        label = html.escape(entry.get("key", "unknown"))
        status = html.escape(entry.get("status", "missing"))
        message = html.escape(entry.get("error", ""))
        picture = entry.get("screenshot")
        img = (
            f'<a href="{html.escape(picture)}"><img loading="lazy" '
            f'src="{html.escape(picture)}" alt="{label}"></a>'
            if picture else "<p>没有截图</p>"
        )
        cards.append(
            f'<article><h2>{label}</h2><p>{status}</p>{img}<pre>{message}</pre></article>'
        )
    page = (
        '<!doctype html><meta charset="utf-8"><title>逐弹框 Actions 验收台账</title>'
        '<style>body{background:#15111f;color:#e8ddf5;font:14px sans-serif;'
        'margin:24px}main{display:grid;grid-template-columns:repeat(auto-fill,'
        'minmax(320px,1fr));gap:16px}article{border:1px solid #6f4a89;'
        'padding:12px}img{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}'
        'h2{font-size:14px}</style>'
        f'<h1>逐弹框 Actions Runner 验收台账</h1>'
        f'<p>要求 {len(expected)} 项视口检查；通过 {summary["passed"]} 项；'
        f'失败或缺失 {len(failures)} 项。所有图像为隔离测试环境截图。</p>'
        '<main>' + "".join(cards) + "</main>"
    )
    (directory / "index.html").write_text(page, encoding="utf-8")
    print(
        f"Modal matrix required={len(expected)} recorded={len(entries)} "
        f"passed={summary['passed']} failed_or_missing={len(failures)}"
    )
    return len(expected), len(failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--fail-incomplete", action="store_true")
    args = parser.parse_args()
    _, missing_or_failed = build_report(args.directory)
    if args.fail_incomplete and missing_or_failed:
        raise SystemExit(1)
