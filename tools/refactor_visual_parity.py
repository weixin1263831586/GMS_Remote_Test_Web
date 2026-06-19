#!/usr/bin/env python3
"""Compare normalized shell screenshots between main and refactor worktrees."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright


PAGES = [
    "test",
    "desktop",
    "terminal",
    "users",
    "devices",
    "reports",
    "report-analysis",
    "apk-analysis",
    "test-suites",
    "api-docs",
    "architecture",
    "websites",
    "tools",
    "security-audit",
    "agent",
    "automation",
    "redmine-agent",
    "gerrit-dashboard",
    "gms-assistant",
]

DEFAULT_MAIN_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKTREE_ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.output: list[str] = []
        env = os.environ.copy()
        env["GMS_PORT"] = str(port)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        if not self.process.stdout:
            return
        for line in self.process.stdout:
            self.output.append(line)

    def wait_ready(self, timeout: float = 40.0) -> None:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"server exited early in {self.root}:\n{''.join(self.output)}"
                )
            try:
                with urlopen(f"{self.base_url}/api/system/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except Exception as exc:  # pragma: no cover - diagnostic path
                last_error = exc
                time.sleep(0.25)
        raise RuntimeError(f"server did not start in {self.root}: {last_error}")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.thread.join(timeout=2)
        if self.process.stdout:
            self.process.stdout.close()


def route_payload(path: str) -> dict[str, Any] | list[dict[str, Any]]:
    if path.endswith("/api/system/health"):
        return {"success": True, "status": "healthy"}
    if path.endswith("/api/system/docs"):
        return {"success": True, "apis": [], "total": 0}
    if path.endswith("/api/devices/list"):
        return [{"device_id": "E2E1", "status": "online", "locked": False}]
    if path.endswith("/api/devices/management"):
        devices = [
            {
                "serial_no": "E2E1",
                "model": "Pixel",
                "soc_model": "gs-test",
                "android_version": "16",
                "battery_level": "80",
                "status": "online",
                "source_type": "local",
                "source_host": "localhost",
                "locked_by": "",
            }
        ]
        return {"success": True, "devices": devices, "device_list": ["E2E1"]}
    if path.endswith("/api/config-explorer/devices"):
        return {"success": True, "devices": [{"device_id": "E2E1", "model": "Pixel"}]}
    if path.endswith("/api/config-explorer/packages"):
        return {"success": True, "packages": []}
    if path.endswith("/api/config-explorer/packages/all"):
        return {"success": True, "packages": []}
    if path.endswith("/api/config-explorer/packages-with-path"):
        return {"success": True, "packages": []}
    if path.endswith("/api/config-explorer/features"):
        return {"success": True, "features": []}
    if path.endswith("/api/config-explorer/props"):
        return {"success": True, "props": []}
    if path.endswith("/api/config-explorer"):
        return {"success": True, "data": {}, "items": []}
    if path.endswith("/api/test/suites"):
        return {"success": True, "suites": [], "items": []}
    if path.endswith("/api/reports/list") or path.endswith("/api/reports"):
        return {"success": True, "reports": [], "items": [], "total": 0}
    if "/statistics" in path:
        return {"success": True, "data": {"summary": {}, "users": [], "trends": {}, "items": []}}
    if path.endswith("/api/websites/load") or path.endswith("/api/websites/sync"):
        return {"success": True, "tools": {}, "last_updated": None}
    if path.endswith("/api/automation/runs"):
        return {"success": True, "data": {"items": [], "events": []}, "items": []}
    return {
        "success": True,
        "message": "visual parity mock",
        "data": {},
        "items": [],
        "tasks": [],
        "task_id": "visual-task",
        "status": "completed",
    }


def install_mocks(page) -> None:
    page.add_init_script(
        """
        (() => {
          const fixedNow = Date.parse('2026-01-02T03:04:05Z');
          const OriginalDate = Date;
          class FixedDate extends OriginalDate {
            constructor(...args) {
              super(...(args.length ? args : [fixedNow]));
            }
            static now() { return fixedNow; }
          }
          FixedDate.UTC = OriginalDate.UTC;
          FixedDate.parse = OriginalDate.parse;
          window.Date = FixedDate;
          Math.random = () => 0.123456789;
          localStorage.setItem('gms_sidebar_visible_pages', JSON.stringify([
            'test', 'desktop', 'terminal', 'users', 'devices', 'reports',
            'report-analysis', 'test-suites', 'apk-analysis', 'security-audit',
            'api-docs', 'architecture', 'websites', 'tools', 'gms-assistant',
            'automation', 'redmine-agent', 'gerrit-dashboard', 'agent'
          ]));
        })();
        """
    )

    def handle(route) -> None:
        path = route.request.url.split("://", 1)[-1]
        path = "/" + path.split("/", 1)[1] if "/" in path else "/"
        path = path.split("?", 1)[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(route_payload(path), ensure_ascii=False),
        )

    page.route("**/api/**", handle)


NORMALIZE_SCRIPT = """
() => {
  const styleId = '__visual_parity_normalize';
  if (!document.getElementById(styleId)) {
    const style = document.createElement('style');
    style.id = styleId;
    style.textContent = `
      *, *::before, *::after {
        animation: none !important;
        transition: none !important;
        caret-color: transparent !important;
        scroll-behavior: auto !important;
      }
      *::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
      * { scrollbar-width: none !important; }
      video, canvas { visibility: hidden !important; }
    `;
    document.head.appendChild(style);
  }
  const normalizeText = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      node.textContent = node.textContent
        .replace(/\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?/g, '2026-01-02 03:04:05')
        .replace(/\\b\\d{2}:\\d{2}:\\d{2}\\b/g, '03:04:05')
        .replace(/\\b\\d+(?:\\.\\d+)?\\s*(?:ms|秒|s)\\b/g, '0ms');
      return;
    }
    for (const child of Array.from(node.childNodes)) normalizeText(child);
  };
  normalizeText(document.body);

  for (const selector of ['#log-output', '#test-log', '#log-content', '.log-output', '.terminal-output']) {
    document.querySelectorAll(selector).forEach((el) => {
      el.textContent = '[03:04:05] normalized log\\n[03:04:05] normalized log\\n[03:04:05] normalized log';
    });
  }

  for (const iframe of document.querySelectorAll('iframe')) {
    const id = iframe.id || 'iframe';
    if (['redmine-agent-frame', 'gerrit-dashboard-frame', 'gms-assistant-frame', 'vnc-frame'].includes(id)) {
      iframe.removeAttribute('src');
      iframe.srcdoc = '<!doctype html><html><head><style>html,body{margin:0;width:100%;height:100%;background:#f8fafc;font:14px Arial;color:#334155;overflow:hidden}</style></head><body><div id="' + id + '-placeholder"></div></body></html>';
    }
  }
  document.querySelectorAll('.toast, .notification, .modal-backdrop').forEach((el) => el.remove());
}
"""


def normalize(page) -> None:
    page.evaluate(NORMALIZE_SCRIPT)
    for frame in page.frames:
        try:
            frame.evaluate(NORMALIZE_SCRIPT)
        except Exception:
            pass
    page.wait_for_timeout(120)
    page.evaluate(NORMALIZE_SCRIPT)


def open_shell_page(page, base_url: str, page_name: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".sidebar-item[data-page]")
    page.evaluate(
        """
        () => {
          if (typeof applySidebarVisibility === 'function') {
            applySidebarVisibility([
              'test', 'desktop', 'terminal', 'users', 'devices', 'reports',
              'report-analysis', 'test-suites', 'apk-analysis', 'security-audit',
              'api-docs', 'architecture', 'websites', 'tools', 'gms-assistant',
              'automation', 'redmine-agent', 'gerrit-dashboard', 'agent'
            ]);
          }
        }
        """
    )
    page.locator(f'.sidebar-item[data-page="{page_name}"]').click()
    page.wait_for_function(
        """pageName => {
          const element = document.querySelector(`#page-${pageName}`);
          return element && element.classList.contains('active');
        }""",
        arg=page_name,
    )
    page.wait_for_timeout(350)
    normalize(page)


def screenshot_page(browser, base_url: str, output: Path, page_name: str) -> str:
    page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
    page.set_default_timeout(10000)
    install_mocks(page)
    try:
        open_shell_page(page, base_url, page_name)
        page.screenshot(path=output, full_page=False, animations="disabled", caret="hide")
        return hashlib.sha256(output.read_bytes()).hexdigest()
    finally:
        page.close()


def screenshot_device_modal(browser, base_url: str, output: Path) -> str:
    page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
    page.set_default_timeout(10000)
    install_mocks(page)
    try:
        open_shell_page(page, base_url, "devices")
        page.wait_for_selector('button:has-text("device info")', state="attached")
        page.wait_for_function("typeof openDeviceConfigExplorer === 'function'")
        page.evaluate("openDeviceConfigExplorer('E2E1')")
        page.wait_for_selector("#device-config-modal.show")
        normalize(page)
        page.screenshot(path=output, full_page=False, animations="disabled", caret="hide")
        return hashlib.sha256(output.read_bytes()).hexdigest()
    finally:
        page.close()


def image_diff(main_path: Path, worktree_path: Path, diff_path: Path) -> dict[str, Any]:
    main = Image.open(main_path).convert("RGBA")
    worktree = Image.open(worktree_path).convert("RGBA")
    diff = ImageChops.difference(main, worktree)
    bbox = diff.getbbox()
    if not bbox:
        return {"pixels": 0, "bbox": None}
    diff.save(diff_path)
    pixels = 0
    for pixel in diff.getdata():
        if pixel != (0, 0, 0, 0):
            pixels += 1
    crop_main = main.crop(bbox)
    crop_worktree = worktree.crop(bbox)
    crop_main.save(diff_path.with_name(diff_path.stem.replace("diff-", "main-crop-") + ".png"))
    crop_worktree.save(diff_path.with_name(diff_path.stem.replace("diff-", "worktree-crop-") + ".png"))
    return {"pixels": pixels, "bbox": list(bbox)}


def run(args: argparse.Namespace) -> int:
    main_root = args.main_root.resolve()
    worktree_root = args.worktree_root.resolve()
    out_dir = args.output.resolve()
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    main_server = Server(main_root, free_port())
    worktree_server = Server(worktree_root, free_port())
    try:
        main_server.wait_ready()
        worktree_server.wait_ready()
        results = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for name in [*PAGES, "device-info-modal"]:
                    main_png = out_dir / f"main-{name}.png"
                    worktree_png = out_dir / f"worktree-{name}.png"
                    if name == "device-info-modal":
                        main_hash = screenshot_device_modal(browser, main_server.base_url, main_png)
                        worktree_hash = screenshot_device_modal(browser, worktree_server.base_url, worktree_png)
                    else:
                        main_hash = screenshot_page(browser, main_server.base_url, main_png, name)
                        worktree_hash = screenshot_page(browser, worktree_server.base_url, worktree_png, name)
                    diff = image_diff(main_png, worktree_png, out_dir / f"diff-{name}.png")
                    results.append(
                        {
                            "name": name,
                            "main_sha256": main_hash,
                            "worktree_sha256": worktree_hash,
                            **diff,
                        }
                    )
                    print(f"{name}: {diff['pixels']} px diff", flush=True)
            finally:
                browser.close()
    finally:
        main_server.close()
        worktree_server.close()

    summary = {
        "main_root": str(main_root),
        "worktree_root": str(worktree_root),
        "viewport": {"width": 1440, "height": 960, "device_scale_factor": 1},
        "pages": results,
        "total_diff_pixels": sum(item["pixels"] for item in results),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"total: {summary['total_diff_pixels']} px diff")
    return 0 if summary["total_diff_pixels"] == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-root", type=Path, default=DEFAULT_MAIN_ROOT)
    parser.add_argument("--worktree-root", type=Path, default=DEFAULT_WORKTREE_ROOT)
    parser.add_argument("--output", type=Path, default=Path("tmp/visual-parity"))
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
