#!/usr/bin/env python3
"""Capture *actual rendered* Shell modal screenshots for visual PR review.

Usage:
 python tools/scripts/maintenance/capture_modal_gallery.py \
   --url https://127.0.0.1:5001/#test --insecure \
   --ids tailscale-info-modal,firmware-modal,username-detect-modal

Use --storage-state to load an authenticated Playwright session.
This tool only opens and closes dialogs; it never submits form operations.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5001/#test")
    parser.add_argument("--output", type=Path, default=Path("artifacts/modal-gallery"))
    parser.add_argument("--ids", help="Comma-separated modal IDs; default all Shell modal IDs")
    parser.add_argument("--storage-state", type=Path, help="Optional Playwright auth JSON")
    parser.add_argument("--insecure", action="store_true", help="Allow self-signed local HTTPS")
    parser.add_argument("--width", type=int, default=1672)
    parser.add_argument("--height", type=int, default=941)
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit nonzero if visual geometry, border or backdrop checks fail",
    )
    return parser.parse_args()


def filename_for(modal_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", modal_id) + ".png"


def capture() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records: list[tuple[str, str | None, str | None]] = []
    audits: dict[str, dict] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context_args: dict = {
            "viewport": {"width": args.width, "height": args.height},
            "ignore_https_errors": args.insecure,
            "device_scale_factor": 1,
        }
        if args.storage_state:
            context_args["storage_state"] = str(args.storage_state)
        context = browser.new_context(**context_args)
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function("typeof ModalManager !== 'undefined'", timeout=15000)
        # 页面加载时可能自动弹出对话框（如客户端用户名识别），先全部关闭，
        # 避免它们叠在后续截图下面造成"背景有另一个弹框"的误判。
        page.wait_for_timeout(800)
        page.evaluate(
            """() => document.querySelectorAll('.modal.show[id]').forEach(el => {
                   try { ModalManager.close(el.id) } catch (e) {}
               })"""
        )
        page.wait_for_timeout(200)
        if args.ids:
            ids = [part.strip() for part in args.ids.split(",") if part.strip()]
        else:
            ids = page.locator(".modal[id]").evaluate_all(
                "els => els.map(el => el.id).filter(Boolean)"
            )
        for modal_id in ids:
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", modal_id):
                records.append((modal_id, None, "Invalid modal ID"))
                continue
            try:
                if page.locator(f"#{modal_id}").count() != 1:
                    raise ValueError("Not a unique Shell modal")
                page.evaluate("(id) => ModalManager.open(id)", modal_id)
                modal = page.locator(f"#{modal_id} .modal-content")
                modal.wait_for(state="visible", timeout=5000)
                page.wait_for_timeout(150)
                shot = filename_for(modal_id)
                modal.screenshot(path=str(args.output / shot), animations="disabled", timeout=15000)
                audit = page.locator(f"#{modal_id}").evaluate(
                    """overlay => {
                        const panel = overlay.querySelector('.modal-content');
                        const rect = panel.getBoundingClientRect();
                        const style = getComputedStyle(panel);
                        const mask = getComputedStyle(overlay);
                        const cssColor = mask.backgroundColor;
                        const colorMatch = cssColor.match(
                            /rgba?\\(\\s*[\\d.]+\\s*,\\s*[\\d.]+\\s*,\\s*[\\d.]+(?:\\s*,\\s*([\\d.]+))?\\s*\\)/
                        );
                        const opacity = colorMatch ? Number(colorMatch[1] ?? 1) : null;
                        const rectWithin = rect.left >= -2 && rect.top >= -2 &&
                            rect.right <= innerWidth + 2 && rect.bottom <= innerHeight + 2;
                        const outlined = style.borderTopStyle !== 'none' &&
                            parseFloat(style.borderTopWidth) >= 1 &&
                            style.borderTopColor !== 'rgba(0, 0, 0, 0)';
                        const isTech = panel.classList.contains('tech-dialog');
                        const metrics = {
                            viewport: [innerWidth, innerHeight],
                            box: {x:rect.x,y:rect.y,width:rect.width,height:rect.height},
                            overflowX: panel.scrollWidth > panel.clientWidth + 2,
                            outlined, clipPath:style.clipPath,
                            backdropColor:cssColor, backdropOpacity:opacity,
                            techDialog:isTech,
                            title:panel.querySelector('.modal-title')?.textContent.trim() ?? null,
                        };
                        metrics.failures = [];
                        if (!rectWithin) metrics.failures.push('panel outside viewport');
                        if (!outlined) metrics.failures.push('missing solid outer border');
                        if (isTech && style.clipPath !== 'none') {
                            metrics.failures.push('outer border clipped');
                        }
                        if (opacity !== null && opacity > .60) {
                            metrics.failures.push('backdrop darkens page by more than 60%');
                        }
                        return metrics;
                    }"""
                )
                audits[modal_id] = audit

                page.screenshot(
                    path=str(args.output / shot.replace(".png", "_full.png")),
                    animations="disabled",
                    full_page=False,
                )
                records.append((modal_id, shot, None))
            except (PlaywrightError, ValueError) as exc:
                records.append((modal_id, None, str(exc).splitlines()[0]))
            finally:
                try:
                    page.evaluate("(id) => ModalManager.close(id)", modal_id)
                except PlaywrightError:
                    pass
        context.close()
        browser.close()

    html_rows = []
    for modal_id, shot, error in records:
        if shot:
            issues = audits.get(modal_id, {}).get("failures", [])
            if issues:
                html_rows.append(
                    f'<p style="color:#ffbdc4">CHECK {html.escape(modal_id)}: '
                    f'{html.escape(", ".join(issues))}</p>'
                )
            html_rows.append(
                f'<figure><figcaption>{html.escape(modal_id)}</figcaption>'
                f'<a href="{html.escape(shot.replace(".png", "_full.png"))}">'
                f'<img src="{html.escape(shot.replace(".png", "_full.png"))}" loading="lazy">'
                '</a><details><summary>查看弹框局部</summary>'
                f'<a href="{html.escape(shot)}"><img src="{html.escape(shot)}" loading="lazy"></a>'
                '</details>'
                "</figure>"
            )
        else:
            html_rows.append(f'<p>{html.escape(modal_id)}: {html.escape(error or "failed")}</p>')
    result = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>真实弹框截图审核</title>'
        '<style>body{background:#101019;color:#eae4f7;font:14px sans-serif;'
        'max-width:1400px;margin:auto;padding:20px}'
        'figure{border:1px solid #594075;padding:16px;margin:18px 0}'
        'img{max-width:100%;height:auto;display:block;margin:auto}'
        'figcaption{font-weight:bold;padding-bottom:10px}</style>'
        '<h1>GMS 弹框真实渲染审核</h1>'
        + "".join(html_rows)
        + "</html>"
    )
    (args.output / "index.html").write_text(result, encoding="utf-8")
    (args.output / "results.json").write_text(
        json.dumps(
            [{"id": name, "file": shot, "error": error, "audit": audits.get(name)}
             for name, shot, error in records],
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    success = sum(shot is not None for _, shot, _ in records)
    failures = sum(bool(audit["failures"]) for audit in audits.values())
    print(
        f"Captured {success}/{len(records)} modals; "
        f"{failures} visual audit failures; {args.output.resolve()}"
    )
    for name, audit in audits.items():
        if audit["failures"]:
            print(f"  FAIL {name}: {', '.join(audit['failures'])}")
    return 0 if success == len(records) and (not args.strict or failures == 0) else 1


if __name__ == "__main__":
    raise SystemExit(capture())
