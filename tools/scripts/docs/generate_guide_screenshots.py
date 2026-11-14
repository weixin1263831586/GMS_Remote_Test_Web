#!/usr/bin/env python3
"""Generate current UI guide images from an isolated authenticated runtime."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root
from guide_fixtures import DEVICES, REPORTS, SUITE, install_guide_fixtures


ROOT = find_repo_root()
OUTPUT = ROOT / "web/static/images/guide"
VIEWPORT = {"width": 1600, "height": 920}
CAPTURED: set[str] = set()
ANNOTATION: dict = {}


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
    install_guide_fixtures(page)
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
    .toast{visibility:hidden!important}
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
  const obstacles = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.textContent.trim() || getComputedStyle(node.parentElement).visibility === 'hidden') continue;
    const range = document.createRange();
    range.selectNode(node);
    obstacles.push(...range.getClientRects());
  }
  document.querySelectorAll('input, select, textarea').forEach(control => {
    if (getComputedStyle(control).visibility !== 'hidden') obstacles.push(control.getBoundingClientRect());
  });
  const occupied = obstacles.filter(rect => rect.width && rect.height && rect.bottom > 0
    && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth);
  items.forEach(item => {
    const target = document.querySelector(item.selector);
    if (!target) throw new Error(`Missing guide control: ${item.selector}`);
    const rect = target.getBoundingClientRect();
    if (!rect.width || !rect.height || getComputedStyle(target).visibility === 'hidden')
      throw new Error(`Hidden guide control: ${item.selector}`);
    target.dataset.guideHighlight = 'true';
    target.style.outline = '3px solid #ffb000';
    target.style.outlineOffset = '2px';
    const badge = document.createElement('span');
    badge.dataset.guideAnnotation = 'true';
    badge.className = 'guide-shot-badge';
    badge.textContent = item.number;
    badge.style.position = 'fixed';
    const row = target.closest('label, .form-row')?.getBoundingClientRect() || rect;
    const candidates = [
      [rect.left - 13, rect.top - 28], [rect.left - 28, rect.top - 28],
      [rect.right + 4, rect.top - 28], [rect.right - 12, rect.top - 28],
      [rect.left - 28, rect.top + 2], [rect.right + 4, rect.top + 2],
      [row.left - 28, rect.top + (rect.height - 24) / 2],
      [row.right + 4, rect.top + (rect.height - 24) / 2],
      [rect.left - 28, rect.bottom + 4], [rect.right + 4, rect.bottom + 4],
    ].map(([x, y], index) => {
      x = Math.max(4, Math.min(innerWidth - 28, x));
      y = Math.max(4, Math.min(innerHeight - 28, y));
      const overlap = occupied.reduce((sum, other) => sum
        + Math.max(0, Math.min(x + 26, other.right) - Math.max(x - 2, other.left))
        * Math.max(0, Math.min(y + 26, other.bottom) - Math.max(y - 2, other.top)), 0);
      return {x, y, score: overlap * 100 + index};
    });
    const best = candidates.reduce((a, b) => a.score <= b.score ? a : b);
    badge.style.left = `${best.x}px`;
    badge.style.top = `${best.y}px`;
    occupied.push({left: best.x - 4, top: best.y - 4, right: best.x + 28, bottom: best.y + 28});
    document.body.append(badge);
  });
}
"""


def annotate(page_or_frame, title: str, items: list[tuple[str, str]], notes: list[str]) -> None:
    global ANNOTATION
    payload = {
        "title": title,
        "items": [{"selector": selector, "number": number} for number, selector in items],
        "notes": notes,
    }
    ANNOTATION = payload
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
    page.evaluate("document.fonts.ready")
    screenshot = base64.b64encode(page.screenshot(caret='hide')).decode('ascii')
    width = page.viewport_size['width']
    notes = ''.join(
        f'<div class="note"><b>{index}</b><span>{html.escape(note)}</span></div>'
        for index, note in enumerate(ANNOTATION['notes'], 1)
    )
    # Keep all controls visible: the explanation sits below the authentic UI
    # capture, rather than obscuring buttons or modal fields with an overlay.
    preview = page.context.new_page()
    preview.set_viewport_size({'width': width, 'height': 1200})
    try:
        preview.set_content(f'''<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">
          <style>
          body,figure{{margin:0;background:#15141e;color:#eef2ff;font:14px/1.6 "Noto Sans CJK SC",sans-serif}}
          figure{{width:{width}px}}img{{display:block;width:100%}}
          figcaption{{padding:16px 24px 20px;border-top:2px solid #ffb000}}
          h3{{margin:0 0 12px;font-size:19px}}small{{float:right;font-size:12px;color:#aaa;font-weight:400}}
          .notes{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px 24px}}
          .note{{display:flex;align-items:baseline;gap:9px}}b{{flex:0 0 22px;text-align:center;
          border-radius:50%;background:#ffb000;color:#111;font:700 13px/22px sans-serif}}
          </style><figure id="guide-figure"><img src="data:image/png;base64,{screenshot}">
          <figcaption><h3>{html.escape(ANNOTATION['title'])}<small>演示数据 · 当前界面操作示例</small></h3>
          <div class="notes">{notes}</div></figcaption></figure></html>''')
        preview.evaluate('document.fonts.ready')
        preview.locator('#guide-figure').screenshot(path=OUTPUT / filename)
    finally:
        preview.close()
    CAPTURED.add(filename)
    print(f"Captured {filename}", flush=True)


def capture_apk(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('apk-analysis', null)")
    page.evaluate("""
        async () => {
          await loadApkTaskHistory(true);
          if (window.apkCurrentTaskId !== 'apk-current') await restoreApkTask('apk-current');
          setApkUploadEmpty(false);
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
            '来源需 Windows usbipd 或 Linux usbipd + SSH；目标需 Linux USB/IP。同一来源的接入操作互斥。';
          document.querySelector('#usbip-attach-submit').disabled = false;
          ModalManager.open('usbip-attach-modal');
        }
    """)
    annotate(
        page,
        "本地设备：USB/IP 精确接入",
        [("1", "#usbip-source-host"), ("2", "#usbip-target-worker"), ("3", "#usbip-source-device"), ("4", "#usbip-assignments"), ("5", "#usbip-attach-submit")],
        [
            "选择实际连接 Android USB 的 Windows / Ubuntu 来源主机。",
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
    page.evaluate('async () => { await loadAgentModelStatus(false, true); }')
    page.evaluate("""
        () => {
          renderAgentSession({
            session_id: 'guide-session', status: 'planning',
            title: 'CTS 模块测试与失败分析（演示）',
            agent_backend: {mode: 'kkagent', state: 'ready', detail: '按项目工具注册表规划，Controller 校验权限并执行。'},
            messages: [
              {id:'u1', role:'user', created_at:'2026-11-14T10:31:00', content:'在 Controller Worker 上运行 CtsWifiTestCases，失败后 retry 2 次并分析报告。'},
              {id:'a1', role:'assistant', kind:'plan', created_at:'2026-11-14T10:31:01', content:`已查询可用资源并完成预检，请核对后确认。
- Worker: Controller
- 套件: CTS 16_r2
- 模块: CtsWifiTestCases
- Retry: 最多 2 次
- 完成动作: 报告分析`, data:{plan:{type:'workflow_draft', preflight:{ready:true, blocks:[]}}}},
            ],
            steps: [
              {status:'done', title:'查询资源', detail:'kkagent 通过项目工具核对 Worker、设备和套件。'},
              {status:'done', title:'资源预检', detail:'设备、套件与 Worker 能力匹配；生成 ATS 运行草案。'},
              {status:'running', title:'等待确认', detail:'当前计划需用户确认，执行仍受权限与设备租约约束。'},
            ],
          });
        }
    """)
    close_elevation(page)
    annotate(
        page,
        "对话 Agent：资源查询、预检与确认",
        [("1", "#agent-engine-status"), ("2", "#agent-model-status"), ("3", "#agent-chat-messages"), ("4", "#agent-steps"), ("5", "#agent-chat-messages button[data-click='confirmAgentPlan']")],
        [
            "对话引擎由 kkagent 规划并选择项目工具，状态单独显示。",
            "分析模型负责报告与知识分析，与对话引擎分别查看。",
            "先查询资源并补齐参数，再核对 Worker、设备、套件和重试次数。",
            "步骤区显示预检、执行、等待确认与错误；失败可按提示重试。",
            "需确认的计划通过预检后才可执行；阻塞时先处理原因再重新规划。",
        ],
    )
    page.evaluate('async () => { await loadAgentModelStatus(false, true); }')
    model = page.locator('#agent-model-status')
    if model.get_attribute('data-state') != 'available' or 'demo-analyzer' not in (model.get_attribute('title') or ''):
        raise RuntimeError(f'Guide analysis model is not ready: {model.text_content()}')
    capture(page, "agent-routing.png")


def capture_cluster(page) -> None:
    clear_annotations(page)
    page.bring_to_front()
    page.evaluate("switchPage('cluster', null)")
    frame = page.frame_locator("#cluster-frame")
    frame.locator('#dashboard-stats[aria-busy="false"]').wait_for(state="visible")
    child = page.locator("#cluster-frame").element_handle().content_frame()
    child.wait_for_function(
        "serial => document.querySelector('#dashboard-tests').textContent.includes(serial)",
        arg=DEVICES[1]['serial'],
    )
    child.evaluate("""
        () => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => String(input).includes('/api/cluster/')
            ? new Promise(() => {}) : originalFetch(input, init);
          document.querySelector('#refresh').click();
        }
    """)
    child.wait_for_function("document.querySelector('#refresh').getAttribute('aria-busy') === 'true'")
    if not child.locator('#refresh').is_disabled():
        raise RuntimeError('Guide cluster refresh must reject duplicate clicks')
    close_elevation(page)
    annotate(
        child,
        "主机集群：刷新时保留稳定页面",
        [("1", "#dashboard-stats"), ("2", "#refresh"), ("3", "#dash-gauges"), ("4", "#dashboard-tests")],
        [
            "已渲染的主机、设备和任务摘要在刷新期间继续保留。",
            "刷新期间按钮暂不可点击，防止重复刷新。",
            "资源图表仅在新数据齐备后原子替换，旧内容不会跳动。",
            "局部接口失败只提示对应错误，其余面板仍可查看和操作。",
        ],
    )
    child.wait_for_function(
        '() => Object.values(dashCharts).every(chart => !chart || chart.getZr().animation.isFinished())'
    )
    capture(page, "cluster-refresh.png")


def capture_test_workflows(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('test', null)")
    page.wait_for_function("window.GmsWorkspace?.isReady?.() !== false")
    page.wait_for_timeout(700)
    page.evaluate("""({devices, suite}) => {
      state.devices = devices;
      state.selectedDevices = new Set([devices[0].device_id]);
      renderDevices();
      const host = document.querySelector('#cluster-worker');
      host.replaceChildren(new Option('Controller（演示）', 'ats-worker-controller'));
      host.disabled = false;
      document.querySelector('#test-type').value = 'CTS';
      const select = document.querySelector('#test-suite');
      select.replaceChildren(new Option('android-cts-16_r2', suite.tools_path));
      document.querySelector('#test-module').value = 'CtsWifiTestCases';
      document.querySelector('#test-case').value = 'android.net.wifi.cts.WifiManagerTest';
      document.querySelector('#device-host').value = '192.0.2.20';
      document.querySelector('#local-server').value = '192.0.2.10';
      document.querySelector('#script-path').value = '/opt/gms/run_GMS_Test_Auto.sh';
      switchLogTab('system');
    }""", {'devices': DEVICES, 'suite': SUITE})
    annotate(page, '测试参数：先选主机，再确认套件与范围', [
        ('1', '#cluster-worker'), ('2', '#test-type'), ('3', '#test-suite'),
        ('4', '#test-module'), ('5', '#test-case'), ('6', '#retry-result'),
    ], [
        '先核对测试 Worker，设备与套件必须来自同一执行主机。',
        '选择 CTS / GTS / VTS / STS 等测试类型。',
        '确认套件版本与目标 Android 版本相符。',
        '按需填写测试模块；模块名必须存在于当前套件。',
        '指定用例可缩小测试范围；留空使用模块或套件默认范围。',
        'Retry 使用同一 Worker 的原始报告，开始后查看测试日志。',
    ])
    capture(page, 'test-parameters.png')
    clear_annotations(page)
    annotate(page, '设备投屏：设备画面显示在主机桌面', [
        ('1', '#device-canvas'), ('2', '[data-click="showDeviceScreen"]'),
        ('3', '.sidebar-item[data-page="desktop"]'), ('4', '[data-log-tab="system"]'),
    ], [
        '选择当前 Worker 下可用的设备；占用与离线设备不可操作。',
        '点击设备投屏，在目标 Worker 上启动 scrcpy。',
        '进入主机桌面查看 scrcpy 窗口；画面来自所选 Worker。',
        '连接与设备操作的提示在系统日志中查看。',
    ])
    capture(page, 'device-screen.png')
    clear_annotations(page)
    annotate(page, '设备烧写：核对目标与固件，跟踪系统日志', [
        ('1', '#device-canvas'), ('2', '#btn-device-info'), ('3', '#btn-burn-firmware'),
        ('4', '#btn-burn-gsi'), ('5', '[data-log-tab="system"]'),
    ], [
        '先确认唯一目标设备、所属 Worker 与接入方式。',
        '核对设备信息与镜像适配关系。',
        '整包固件先分块暂存并校验，再进入最终烧写。',
        'GSI 入口用于系统镜像；ADB Proxy 不支持 Fastboot / 烧写。',
        '最终烧写期间不要断电或拔线；异常保留日志与任务编号。',
    ])
    capture(page, 'device-flashing.png')


def capture_reports(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('reports', null)")
    page.wait_for_selector('#reports-table-body [data-report-name]')
    page.evaluate('reports => displayTestReports(reports)', REPORTS)
    annotate(page, '报告管理：确认报告身份，再分析或重试', [
        ('1', '#reports-worker-filter'), ('2', '#filter-user-checkbox'),
        ('3', '#reports-table-body tr:first-child'),
    ], [
        '先按 Worker 筛选，报告筛选不会改变测试执行主机。',
        '按需只显示本人报告，再核对套件版本、报告名称和统计。',
        '按行分析、Retry、下载或打开目录；删除前核对报告身份。',
    ])
    capture(page, 'report-management.png')
    clear_annotations(page)
    page.evaluate("switchPage('report-analysis', null)")
    page.evaluate("""() => displayReportAnalysis({
      report_name: '2026.11.14_09.30.00（演示）',
      details: {test_type:'CTS', suite_version:'16_r2', android_version:'16', soc_platform:'RK3576'},
      summary: {total:150, pass:148, fail:2, pass_rate:'98.67%'},
      failures: [
        {module:'CtsWifiTestCases', name:'android.net.wifi.cts.WifiManagerTest#testWifiState',
         reason:'演示失败：Wi-Fi state did not become ready within timeout.'},
        {module:'CtsNetTestCases', name:'android.net.cts.ConnectivityManagerTest#testNetwork',
         reason:'演示失败：Expected a validated network.'}
      ]
    })""")
    annotate(page, '报告分析：从统计定位到失败用例', [
        ('1', '#report-upload-zone'), ('2', '#report-summary'), ('3', '#report-failure-list'),
    ], [
        '从报告管理打开，或上传 XML / 压缩包 / 结果目录。',
        '先核对 Android、套件版本与通过 / 失败统计。',
        '查看模块、用例与错误堆栈，可回填单测或进入报错诊断。',
    ])
    capture(page, 'report-analysis.png')


def capture_suites(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('test-suites', null)")
    page.wait_for_selector('.suite-suite-item')
    page.evaluate('path => selectTestSuiteForBrowser(path)', SUITE['tools_path'])
    page.wait_for_selector('.suite-file-row')
    annotate(page, '测试套件：原生 results / logs 从这里查找', [
        ('1', '#suite-worker-select'), ('2', '#suite-download-url'),
        ('3', '#suite-browser-list'), ('4', '#suite-file-list'), ('5', '#btn-test-results'),
    ], [
        '选择产生测试结果的 Worker；套件浏览选择与执行上下文独立。',
        '支持下载、解压或添加该 Worker 已有的本地套件。',
        '按套件类型与版本选择对应套件。',
        '原生运行产物在 results / logs；未登记到报告管理也可在这里查看。',
        '测试结果入口查询 Tradefed 结果，按运行时间戳匹配目录。',
    ])
    capture(page, 'test-suites.png')


def capture_ats(page) -> None:
    clear_annotations(page)
    page.evaluate("switchPage('automation', null)")
    frame = page.frame_locator('#automation-frame')
    frame.locator('body[data-automation-ready="true"]').wait_for()
    frame.locator('[data-workflow="create"]').click()
    child = page.locator('#automation-frame').element_handle().content_frame()
    child.evaluate("""() => {
      const demoOption = (id, label, value) => {
        const select = document.getElementById(id);
        select.replaceChildren(new Option(label, value));
        select.disabled = false;
      };
      demoOption('automation-profile', 'Android 16 CTS（演示）', 'demo-profile');
      demoOption('build-server', 'builder@192.0.2.20', 'demo-build');
      demoOption('build-template', 'Android 固件构建模板（演示）', 'demo-template');
      demoOption('build-workspace', '/srv/android16', '/srv/android16');
      demoOption('build-lunch-target', 'rk3576-userdebug', 'rk3576-userdebug');
      document.getElementById('build-lunch-target').disabled = false;
      demoOption('automation-worker', 'Controller（演示）', 'ats-worker-controller');
      demoOption('automation-test-suite', 'CTS 16_r2（演示）', 'cts-16-r2');
    }""")
    annotate(child, 'GMS ATS：发起、预检、追踪与诊断', [
        ('1', '#automation-profile'), ('2', '#build-workspace'),
        ('3', '#build-lunch-target'), ('4', '#automation-worker'),
        ('5', '#automation-test-suite'),
    ], [
        '从“发起”选择 Profile；也可在“平台”配置资源。',
        '使用已有固件，或选择编译服务器、构建模板与源码目录。',
        'Lunch Target 只取当前源码目录扫描结果。',
        '核对 Worker 与可用设备，烧写模式限选 1 台本地 USB / USB-IP 设备。',
        '选测试类型与套件，完成预检后创建运行；在“追踪与诊断”查看进度。',
    ])
    capture(page, 'ats-create-run.png')


def capture_access(page) -> None:
    clear_annotations(page)
    page.set_viewport_size({'width': 1600, 'height': 1100})
    page.evaluate("switchPage('users', null)")
    close_elevation(page)
    page.locator('#sdk-config-tab').click()
    page.locator('#agent-sdk-sources-add').click()
    page.locator('#agent-sdk-add-target').fill('builder@192.0.2.20')
    page.locator('#agent-sdk-add-discover-btn').click()
    page.wait_for_function("document.querySelectorAll('#agent-sdk-add-path-list input').length === 2")
    page.locator('#agent-sdk-add-android-version').fill('Android16')
    page.locator('#agent-sdk-add-revision').fill('HEAD')
    annotate(page, 'SDK 配置：发现源码目录并核对版本', [
        ('1', '#agent-sdk-add-type'), ('2', '#agent-sdk-add-target'),
        ('3', '#agent-sdk-add-path-list'), ('4', '#agent-sdk-add-android-version'),
        ('5', '#agent-sdk-add-revision'), ('6', '#agent-sdk-add-probe-btn'),
    ], [
        '选择本机 Git 或编译服务器 SSH 只读源码。',
        '填写服务器，按回车或点击“发现目录”。',
        '仅列出 Android 源码树；可多选批量添加，也可手动输入路径。',
        '目录可自动识别 Android 版本，识别不准时手动修正。',
        '核对默认 revision；分析时还需绑定匹配问题版本的 commit。',
        '校验路径后添加，返回列表查看版本统计与校验状态。',
    ])
    capture(page, 'sdk-config.png')
    clear_annotations(page)
    page.evaluate("sdkConfigToggleAddSdk(false)")
    page.locator('#agent-access-tab').click()
    page.locator('#agent-access-create-toggle').click()
    page.locator('#agent-access-name').fill('codex-lab-demo')
    annotate(page, 'Agent 接入：配置范围，再创建一次性配对码', [
        ('1', '#agent-access-name'), ('2', '#agent-access-days'),
        ('3', '#agent-access-scope-details'), ('4', '#agent-access-workers'),
        ('5', '#agent-access-devices'), ('6', '#agent-access-create-submit'),
    ], [
        'Agent 名称建议包含客户端与主机，方便后续识别。',
        '凭据有效期与配对码的 5 分钟有效期分别设置。',
        '只授予需要的 scope；可恢复推荐、全选或清空。',
        '按 Worker ID 限制执行目标，多个 ID 用逗号分隔。',
        '按设备序列号限制范围，* 表示全部。',
        '生成时验证管理员身份，配对码单次使用；截图未创建凭据。',
    ])
    capture(page, 'agent-enrollment.png')
    page.evaluate('agentAccessToggleCreate(false)')


def main() -> None:
    global OUTPUT
    gallery = (ROOT / 'web/shell/dialogs/sidebar-visibility.html').read_text(encoding='utf-8')
    expected = set(re.findall(r'/static/images/guide/([^"?]+\.png)', gallery))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='+', choices=sorted(expected), help='Only replace these guide images.')
    args = parser.parse_args()
    requested = set(args.only) if args.only else expected
    destination = OUTPUT
    port = free_port()
    runtime = tempfile.TemporaryDirectory(prefix="gms-guide-")
    OUTPUT = Path(runtime.name) / 'screenshots'
    OUTPUT.mkdir()
    CAPTURED.clear()
    env = os.environ.copy()
    env.update({
        "GMS_PORT": str(port),
        "GMS_DATA_ROOT": runtime.name,
        "GMS_ENV": "development",
        "GMS_SKIP_RUNTIME_ENV": "1",
        "ATS_WORKER_ENABLED": "0",
        "GMS_ASSISTANT_KKAGENT_ENABLED": "0",
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
            context = browser.new_context(viewport=VIEWPORT, bypass_csp=True)
            page = context.new_page()
            status = page.request.get(f"{base_url}/api/auth/status").json()
            endpoint = "setup" if status.get("setup_required") else "login"
            # 一次性口令：仅用于本脚本刚拉起的 127.0.0.1 临时实例，
            # 运行结束即销毁；避免在源码中出现字面量密码。
            guide_password = f"GuideAdmin-{secrets.randbits(32)}!"
            login = page.request.post(
                f"{base_url}/api/auth/{endpoint}",
                data={"username": "guide-admin", "password": guide_password, "display_name": "Guide Admin"},
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
            page.wait_for_timeout(1600)
            page.evaluate("""
                () => {
                  document.querySelectorAll('.modal.show').forEach(modal => ModalManager.close(modal.id));
                  window.requestElevatedAccess = async () => false;
                  applySidebarVisibility(['test','desktop','terminal','users','devices','reports','report-analysis','test-suites','apk-analysis','architecture','automation','cluster','agent']);
                }
            """)
            groups = [
                (capture_apk, {'apk-analysis.png'}),
                (capture_test_workflows, {'test-parameters.png', 'device-screen.png', 'device-flashing.png'}),
                (capture_usbip, {'usbip-local-device.png'}),
                (capture_reports, {'report-management.png', 'report-analysis.png'}),
                (capture_suites, {'test-suites.png'}),
                (capture_ats, {'ats-create-run.png'}),
                (capture_agent, {'agent-routing.png'}),
                (capture_cluster, {'cluster-refresh.png'}),
                (capture_access, {'sdk-config.png', 'agent-enrollment.png'}),
            ]
            for capture_group, filenames in groups:
                if requested & filenames:
                    capture_group(page)
            browser.close()
        # Full regeneration still requires exact gallery coverage. A targeted
        # refresh may capture a group but only publishes the requested files.
        if not requested.issubset(CAPTURED) or not CAPTURED.issubset(expected):
            raise RuntimeError(f'Guide image coverage mismatch: missing={requested - CAPTURED}, extra={CAPTURED - expected}')
        destination.mkdir(parents=True, exist_ok=True)
        for filename in sorted(requested):
            shutil.copy2(OUTPUT / filename, destination / filename)
        print(f'Updated {len(requested)} guide images in {destination}', flush=True)
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
        OUTPUT = destination


if __name__ == "__main__":
    main()
