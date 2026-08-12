#!/usr/bin/env python3
"""Generate current UI guide images from an isolated authenticated runtime."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "web/static/images/guide"
VIEWPORT = {"width": 1600, "height": 920}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def json_response(route, payload: dict) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )


def install_routes(page) -> None:
    page.route(
        "**/api/apk/tasks",
        lambda route: json_response(route, {
            "success": True,
            "data": {"tasks": [
                {"task_id": "apk-current", "filename": "DemoApp.apk", "status": "completed", "progress": 100},
                {"task_id": "apk-previous", "filename": "Settings.apk", "status": "error", "progress": 63},
            ]},
        }),
    )
    page.route(
        "**/api/apk/status/**",
        lambda route: json_response(route, {
            "success": True,
            "data": {"task_id": "apk-current", "filename": "DemoApp.apk", "status": "completed", "progress": 100},
        }),
    )
    page.route(
        "**/api/apk/manifest/**",
        lambda route: json_response(route, {
            "success": True,
            "data": {
                "manifest": {
                    "package": "com.example.demo",
                    "versionName": "16.0",
                    "versionCode": "160",
                    "minSdkVersion": "29",
                    "targetSdkVersion": "35",
                },
                "raw_xml": (
                    '<manifest package="com.example.demo">\n'
                    '  <uses-permission android:name="android.permission.INTERNET" />\n'
                    '  <application android:label="DemoApp" />\n'
                    "</manifest>"
                ),
            },
        }),
    )

    cluster_payloads = {
        "/api/cluster/status": {
            "success": True,
            "enabled": True,
            "effective_enabled": True,
            "remote_dispatch_enabled": True,
            "local_worker_id": "ats-worker-controller",
        },
        "/api/cluster/workers": {"success": True, "workers": [
            {
                "id": "ats-worker-controller",
                "name": "Controller",
                "hostname": "controller",
                "address": "10.10.10.10",
                "status": "online",
                "running_jobs": 0,
                "cpu_percent": 31,
                "memory_percent": 46,
                "disk_percent": 58,
                "capabilities": {"command_agent": True, "usbip_client": True, "adb_proxy": True},
            },
            {
                "id": "ats-worker-02",
                "name": "Lab Worker 02",
                "hostname": "lab-worker-02",
                "address": "10.10.10.22",
                "status": "busy",
                "running_jobs": 1,
                "cpu_percent": 64,
                "memory_percent": 53,
                "disk_percent": 41,
                "capabilities": {"command_agent": True, "usbip_client": True, "adb_proxy": True},
            },
        ]},
        "/api/cluster/devices": {"success": True, "devices": [
            {"id": "ats-worker-controller:LOCAL001", "worker_id": "ats-worker-controller", "serial": "LOCAL001", "state": "available", "transport": "local_usb", "properties": {"model": "RK3576"}},
            {"id": "ats-worker-02:REMOTE001", "worker_id": "ats-worker-02", "serial": "REMOTE001", "state": "allocated", "transport": "usbip", "properties": {"model": "RK3572"}},
            {"id": "ats-worker-02:REMOTE002", "worker_id": "ats-worker-02", "serial": "REMOTE002", "state": "available", "transport": "adb_proxy", "properties": {"model": "RK3576"}},
        ]},
        "/api/cluster/suites": {"success": True, "suites": [
            {"worker_id": "ats-worker-controller", "suite_key": "cts-16-r2", "suite_type": "CTS", "suite_version": "16_r2", "path": "/opt/gms/cts", "available": True},
            {"worker_id": "ats-worker-02", "suite_key": "gts-13-r5", "suite_type": "GTS", "suite_version": "13_r5", "path": "/opt/gms/gts", "available": True},
        ]},
        "/api/cluster/jobs": {"success": True, "jobs": [
            {"id": "job-demo-running", "assigned_worker_id": "ats-worker-02", "status": "running", "created_at": "2026-08-12T10:30:00+08:00", "leases": [{"serial": "REMOTE001"}]},
        ]},
        "/api/cluster/worker-tests": {"success": True, "tests": [
            {"worker_id": "ats-worker-02", "status": "running", "source": "managed", "suite_type": "CTS", "devices": ["REMOTE001"], "elapsed_seconds": 1280},
        ]},
        "/api/cluster/suite-library": {"success": True, "archives": []},
    }

    def cluster_route(route) -> None:
        path = route.request.url.split("?", 1)[0].split("127.0.0.1:", 1)[-1]
        path = "/" + path.split("/", 1)[1] if "/" in path else path
        if path.endswith("/vpn-status") or path == "/api/vpn/status":
            json_response(route, {"success": True, "connected": True})
            return
        payload = cluster_payloads.get(path)
        if payload is None:
            route.continue_()
            return
        json_response(route, payload)

    page.route("**/api/cluster/**", cluster_route)
    page.route("**/api/vpn/status", cluster_route)
    for suffix in ("load", "save", "sync"):
        page.route(
            f"**/api/websites/{suffix}",
            lambda route: json_response(route, {"success": True, "tools": {}}),
        )


ANNOTATION_SCRIPT = """
({items, title, notes}) => {
  document.querySelectorAll('[data-guide-annotation]').forEach(node => node.remove());
  document.querySelectorAll('[data-guide-highlight]').forEach(node => {
    node.style.removeProperty('outline');
    node.style.removeProperty('outline-offset');
    node.removeAttribute('data-guide-highlight');
  });
  document.querySelectorAll('[data-guide-position-host]').forEach(node => {
    node.style.position = node.dataset.guideOriginalPosition || '';
    node.removeAttribute('data-guide-original-position');
    node.removeAttribute('data-guide-position-host');
  });
  const style = document.createElement('style');
  style.dataset.guideAnnotation = 'true';
  style.textContent = `
    *,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}
    .guide-shot-badge{position:absolute;z-index:2147483647;width:24px;height:24px;border-radius:50%;
      display:flex;align-items:center;justify-content:center;background:#ffb000;color:#111;font:700 13px Arial;
      box-shadow:0 0 0 2px #111,0 2px 8px rgba(0,0,0,.55);pointer-events:none}
    .guide-shot-card{position:fixed;right:18px;bottom:18px;z-index:2147483646;width:min(520px,44vw);
      padding:16px 18px;border:2px solid #ffb000;border-radius:10px;background:rgba(7,10,16,.96);
      color:#eef2ff;box-shadow:0 12px 36px rgba(0,0,0,.55);font:13px/1.55 Arial,sans-serif;pointer-events:none}
    .guide-shot-card h3{margin:0 0 9px;font-size:18px}.guide-shot-card div{display:flex;gap:8px;margin:4px 0}
    .guide-shot-card b{flex:0 0 20px;height:20px;border-radius:50%;background:#ffb000;color:#111;text-align:center;line-height:20px}
  `;
  document.head.append(style);
  items.forEach(item => {
    const target = document.querySelector(item.selector);
    if (!target) return;
    const rect = target.getBoundingClientRect();
    target.dataset.guideHighlight = 'true';
    target.style.outline = '3px solid #ffb000';
    target.style.outlineOffset = '2px';
    const badge = document.createElement('span');
    badge.dataset.guideAnnotation = 'true';
    badge.className = 'guide-shot-badge';
    badge.textContent = item.number;
    const host = target.parentElement || document.body;
    host.dataset.guideOriginalPosition = host.style.position || '';
    host.dataset.guidePositionHost = 'true';
    if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
    const hostRect = host.getBoundingClientRect();
    badge.style.left = `${rect.left - hostRect.left - (host.clientLeft || 0) + (host.scrollLeft || 0) + 2}px`;
    badge.style.top = `${rect.top - hostRect.top - (host.clientTop || 0) + (host.scrollTop || 0) + 2}px`;
    host.append(badge);
  });
  const card = document.createElement('aside');
  card.dataset.guideAnnotation = 'true';
  card.className = 'guide-shot-card';
  card.innerHTML = `<h3>${title}</h3>` + notes.map((note, index) =>
    `<div><b>${index + 1}</b><span>${note}</span></div>`).join('');
  document.body.append(card);
}
"""


def annotate(page_or_frame, title: str, items: list[tuple[str, str]], notes: list[str]) -> None:
    payload = {
        "title": title,
        "items": [{"selector": selector, "number": number} for number, selector in items],
        "notes": notes,
    }
    page_or_frame.evaluate(ANNOTATION_SCRIPT, payload)
    # Several views finish an async status render after their first visible
    # result.  Recalculate against the settled layout so badges stay attached
    # to their highlighted controls in the captured guide image.
    page_or_frame.wait_for_timeout(300)
    page_or_frame.evaluate(ANNOTATION_SCRIPT, payload)
    page_or_frame.evaluate("""() => {
      const highlighted = [...document.querySelectorAll('[data-guide-highlight]')]
        .map(node => node.getBoundingClientRect().toJSON());
      const badges = [...document.querySelectorAll('.guide-shot-badge')]
        .map(node => node.getBoundingClientRect().toJSON());
      return {highlighted, badges};
    }""")


def clear_annotations(page_or_frame) -> None:
    page_or_frame.evaluate("""
        () => {
          document.querySelectorAll('[data-guide-annotation]').forEach(node => node.remove());
          document.querySelectorAll('[data-guide-highlight]').forEach(node => {
            node.style.removeProperty('outline');
            node.style.removeProperty('outline-offset');
            node.removeAttribute('data-guide-highlight');
          });
          document.querySelectorAll('[data-guide-position-host]').forEach(node => {
            node.style.position = node.dataset.guideOriginalPosition || '';
            node.removeAttribute('data-guide-original-position');
            node.removeAttribute('data-guide-position-host');
          });
        }
    """)


def close_elevation(page) -> None:
    page.evaluate("""
        () => {
          if (document.querySelector('#elevate-modal.show')) ModalManager.close('elevate-modal');
        }
    """)


def capture(page, filename: str) -> None:
    page.screenshot(
        path=OUTPUT / filename,
        full_page=False,
        caret="hide",
    )


def capture_apk(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('apk-analysis', null)")
    page.evaluate("""
        async () => {
          window.apkCurrentTaskId = 'apk-current';
          await loadApkTaskHistory(true);
          await pollApkStatus();
        }
    """)
    page.wait_for_function(
        "document.querySelector('#apk-manifest-info')?.textContent.includes('com.example.demo')"
    )
    page.evaluate("""
        () => {
          document.querySelector('#apk-analysis-status').style.display = 'block';
          document.querySelector('#apk-analysis-result').style.display = 'block';
          document.querySelector('#apk-btn-download').style.display = 'inline-flex';
        }
    """)
    close_elevation(page)
    page.wait_for_timeout(600)
    annotate(
        page,
        "APK 分析：任务可恢复、结果可追溯",
        [("1", ".sidebar-item[data-page='apk-analysis']"), ("2", "#apk-task-history"), ("3", "#apk-upload-zone"), ("4", "#apk-btn-download"), ("5", ".apk-tab-actions"), ("6", "#apk-manifest-info")],
        [
            "进入 APK 分析，支持 APK 与 JAR。",
            "从“最近任务”恢复刷新前的分析进度或历史结果。",
            "上传新文件会创建独立任务，不会覆盖其他用户任务。",
            "完成后可下载反编译源码；失败任务可重新分析。",
            "在 Manifest、权限和源码间切换并搜索定位。",
            "先核对包名、版本、SDK，再检查权限和实现。",
        ],
    )
    capture(page, "apk-analysis.png")


def capture_usbip(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('test', null)")
    page.evaluate("""
        () => {
          const source = document.querySelector('#usbip-source-host');
          const target = document.querySelector('#usbip-target-worker');
          const devices = document.querySelector('#usbip-source-device');
          source.replaceChildren(new Option('device-user@10.10.10.20', 'device-user@10.10.10.20'));
          target.replaceChildren(new Option('Controller', 'ats-worker-controller'), new Option('Lab Worker 02', 'ats-worker-02'));
          target.value = 'ats-worker-02';
          devices.replaceChildren(new Option('1-8 · RK3576 · SERIAL-002', '1-8'), new Option('1-9 · RK3572 · SERIAL-003', '1-9'));
          devices.options[0].selected = true;
          document.querySelector('#usbip-assignments').innerHTML = `
            <div class="adb-proxy-assignment routing-status-attached">
              <div class="adb-proxy-assignment-info">device-user@10.10.10.20 → Controller · 1-7｜设备：SERIAL-001｜已接入</div>
              <div class="device-routing-actions"><button class="btn-xxs btn-danger">断开</button></div>
            </div>`;
          document.querySelector('#usbip-attach-message').textContent =
            '来源需 Windows usbipd + SSH；目标需 Linux USB/IP。操作期间同一来源由服务端互斥保护。';
          document.querySelector('#usbip-attach-submit').disabled = false;
          ModalManager.open('usbip-attach-modal');
        }
    """)
    annotate(
        page,
        "本地设备：USB/IP 精确接入",
        [("1", "#usbip-source-host"), ("2", "#usbip-target-worker"), ("3", "#usbip-source-device"), ("4", "#usbip-assignments"), ("5", "#usbip-attach-submit")],
        [
            "选择实际连接 Android USB 的 Windows 来源主机。",
            "目标可为 Controller 或具备 USB/IP 能力的在线 Worker。",
            "按 BUSID 多选设备；同一设备同一时间只接入一个目标。",
            "当前接入逐端口显示；可单独断开且不会影响同来源其他端口。",
            "连接/断开期间服务端拒绝同来源重复操作；测试占用时禁止断开。",
        ],
    )
    capture(page, "usbip-local-device.png")
    page.evaluate("ModalManager.close('usbip-attach-modal')")


def capture_agent(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('agent', null)")
    page.evaluate("""
        () => {
          const model = document.querySelector('#agent-model-status');
          model.textContent = '分析模型：Local GLM · 检测可用 42ms';
          model.dataset.state = 'available';
          renderAgentSession({
            session_id: 'guide-session', status: 'planning',
            messages: [
              {id:'u1', role:'user', created_at:'2026-08-12T10:31:00', content:'在 Lab Worker 02 上运行 CtsWifiTestCases，失败后 retry 2 次并分析报告。'},
              {id:'a1', role:'assistant', kind:'plan', created_at:'2026-08-12T10:31:01', content:`已生成执行计划，需要确认后开始。
- Worker: Lab Worker 02
- 套件: CTS 16_r2
- 模块: CtsWifiTestCases
- Retry: 最多 2 次
- 完成动作: 报告分析`, data:{plan:{}}},
            ],
            steps: [
              {status:'done', title:'解析指令', detail:'确定性规则已识别 Worker、模块、Retry 与报告动作。'},
              {status:'running', title:'等待确认', detail:'写操作不会自动开始；确认后才申请设备并执行。'},
              {status:'warning', title:'分析模型边界', detail:'本地/备用模型用于报告和知识分析，不负责绕过权限或任意执行项目代码。'},
            ],
          });
        }
    """)
    close_elevation(page)
    annotate(
        page,
        "对话 Agent：受控编排，不是无人值守接管",
        [("1", "#agent-chat-messages"), ("2", "#agent-input"), ("3", "#agent-model-status"), ("4", "#agent-steps"), ("5", "#agent-chat-messages button[onclick='confirmAgentPlan()']")],
        [
            "自然语言会被解析为受支持的页面、查询和操作计划。",
            "输入测试、设备、Retry 或分析意图；未支持动作会明确拒绝。",
            "分析模型状态单独显示；模型不可用不影响确定性指令路由。",
            "步骤区展示执行、等待确认和失败位置，便于人工核对。",
            "写操作必须确认并经过权限、设备租约和 Worker 能力检查。",
        ],
    )
    capture(page, "agent-routing.png")


def capture_cluster(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('cluster', null)")
    frame = page.frame_locator("#cluster-frame")
    frame.locator("#dashboard-stats").wait_for(state="visible")
    page.wait_for_timeout(700)
    child = page.locator("#cluster-frame").element_handle().content_frame()
    child.evaluate("""
        () => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => String(input).includes('/api/cluster/')
            ? new Promise(() => {}) : originalFetch(input, init);
          document.querySelector('#dash-refresh-charts').click();
        }
    """)
    child.wait_for_function("document.querySelector('#dash-refresh-charts').textContent.includes('刷新中')")
    close_elevation(page)
    annotate(
        child,
        "主机集群：刷新时保留稳定页面",
        [("1", "#dashboard-stats"), ("2", "#dash-refresh-charts"), ("3", "#dash-gauges"), ("4", "#dashboard-tests")],
        [
            "已渲染的主机、设备和任务摘要在刷新期间继续保留。",
            "按钮显示“刷新中”并禁用，防止重复刷新，不再整页黑屏。",
            "资源图表仅在新数据齐备后原子替换，旧内容不会跳动。",
            "局部接口失败只提示对应错误，其余面板仍可查看和操作。",
        ],
    )
    capture(page, "cluster-refresh.png")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    port = free_port()
    runtime = tempfile.TemporaryDirectory(prefix="gms-guide-")
    env = os.environ.copy()
    env.update({
        "GMS_PORT": str(port),
        "GMS_DATA_ROOT": runtime.name,
        "GMS_ENV": "development",
        "GMS_SKIP_RUNTIME_ENV": "1",
        "ATS_WORKER_ENABLED": "0",
        "GMS_AUTH_REQUIRED": "true",
        "GMS_SECURE_COOKIES": "false",
    })
    server = subprocess.Popen(
        ["python", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    drain = threading.Thread(
        target=lambda: output.extend(iter(server.stdout.readline, "")) if server.stdout else None,
        daemon=True,
    )
    drain.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if server.poll() is not None:
                raise RuntimeError("guide server exited early:\n" + "".join(output[-80:]))
            try:
                if urlopen(f"{base_url}/api/system/health", timeout=1).status == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("guide server did not become ready")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport=VIEWPORT, bypass_csp=True)
            status = page.request.get(f"{base_url}/api/auth/status").json()
            endpoint = "setup" if status.get("setup_required") else "login"
            login = page.request.post(
                f"{base_url}/api/auth/{endpoint}",
                data={"username": "guide-admin", "password": "GuideAdmin-2026!", "display_name": "Guide Admin"},
            )
            if not login.ok:
                raise RuntimeError(f"guide login failed: {login.text()}")
            install_routes(page)
            page.add_init_script("""
                localStorage.setItem('gms_sidebar_visible_pages', JSON.stringify([
                  'test','desktop','terminal','users','devices','reports','report-analysis',
                  'test-suites','apk-analysis','architecture','automation','cluster','agent'
                ]));
            """)
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_selector(".sidebar-item[data-page]")
            page.evaluate("""
                () => {
                  document.querySelectorAll('.modal.show').forEach(modal => ModalManager.close(modal.id));
                  window.requestElevatedAccess = async () => false;
                  applySidebarVisibility(['test','desktop','terminal','users','devices','reports','report-analysis','test-suites','apk-analysis','architecture','automation','cluster','agent']);
                }
            """)
            capture_apk(page)
            capture_usbip(page)
            capture_agent(page)
            capture_cluster(page)
            browser.close()
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        if server.stdout:
            server.stdout.close()
        drain.join(timeout=2)
        runtime.cleanup()


if __name__ == "__main__":
    main()
