"""启动时按 configs/config.json 的 static_routes 配置自动添加本机路由。

配置格式（static_routes 可整体缺省，缺省时不做任何事）::

    "static_routes": {
        "enabled": true,
        "routes": [
            {"destination": "10.10.10.0/24", "gateway": "172.16.14.1"},
            {"destination": "10.10.10.29/32", "gateway": "172.16.14.1"}
        ]
    }

行为约定：
- 幂等：已存在的等价路由（同目标同网关）跳过，不重复添加；
- 使用 ``ip route replace`` 语义，目标已存在但网关不同时以配置为准修正；
- 修改内核路由表需要 root。程序以普通用户运行时自动改走 ``sudo -n``；
  若未配置免密 sudo 规则会失败并记录告警（不影响服务启动），见模块底部的
  sudoers 配置说明。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import subprocess
import threading

from foundation.config import config_manager


logger = logging.getLogger(__name__)


def _parse_route(entry: dict) -> tuple[ipaddress._BaseNetwork, ipaddress._BaseAddress] | None:
    """解析并校验一条路由配置；非法条目返回 None 并告警。"""
    destination = str(entry.get('destination') or '').strip()
    gateway = str(entry.get('gateway') or '').strip()
    if not destination or not gateway:
        logger.warning('[StaticRoutes] 路由条目缺少 destination 或 gateway: %r', entry)
        return None
    try:
        network = ipaddress.ip_network(destination, strict=False)
    except ValueError:
        logger.warning('[StaticRoutes] 无效的 destination: %r', destination)
        return None
    try:
        gateway_ip = ipaddress.ip_address(gateway)
    except ValueError:
        logger.warning('[StaticRoutes] 无效的 gateway: %r', gateway)
        return None
    return network, gateway_ip


def _route_matches(destination: str, gateway: str) -> bool:
    """已存在的该目标路由是否与配置的网关一致。"""
    try:
        result = subprocess.run(
            ['ip', 'route', 'show', destination],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f'via {gateway}' in (result.stdout or '')


def _replace_route(destination: str, gateway: str) -> tuple[bool, str]:
    """添加/修正路由。root 直接执行；普通用户改走 sudo -n（免密）。"""
    command = ['ip', 'route', 'replace', destination, 'via', gateway]
    if os.geteuid() != 0 and shutil.which('sudo'):
        command = ['sudo', '-n', *command]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or '').strip()
        return False, stderr or f'exit code {result.returncode}'
    return True, ''


def apply_static_routes(config: dict | None = None) -> list[dict]:
    """按配置逐条确保路由存在，返回每条的处理结果（供日志与测试断言）。"""
    if config is None:
        config = config_manager.load_config()
    routes_config = config.get('static_routes') or {}
    if not routes_config.get('enabled', False):
        return []
    entries = routes_config.get('routes') or []
    if not isinstance(entries, list):
        logger.warning('[StaticRoutes] static_routes.routes 不是列表，忽略')
        return []

    results: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning('[StaticRoutes] 忽略非法路由条目: %r', entry)
            continue
        parsed = _parse_route(entry)
        if parsed is None:
            continue
        _network, gateway_ip = parsed
        destination = str(entry['destination']).strip()
        gateway = str(gateway_ip)
        # 网络对象带前缀长度；ip route 命令使用配置中的原始写法。
        if _route_matches(destination, gateway):
            results.append({
                'destination': destination, 'gateway': gateway,
                'status': 'exists',
            })
            continue
        ok, error = _replace_route(destination, gateway)
        results.append({
            'destination': destination, 'gateway': gateway,
            'status': 'ok' if ok else 'failed',
            'error': error or None,
        })
        if ok:
            logger.info('[StaticRoutes] 已添加路由: %s via %s', destination, gateway)
        else:
            logger.warning(
                '[StaticRoutes] 添加路由失败: %s via %s: %s'
                '（需要 root；可配置 sudoers 免密执行 /usr/sbin/ip route replace）',
                destination, gateway, error,
            )
    return results


def apply_static_routes_async() -> threading.Thread:
    """启动期后台线程执行，不阻塞服务监听端口。"""
    def _run():
        try:
            apply_static_routes()
        except Exception:
            logger.exception('[StaticRoutes] 启动时应用静态路由失败')

    thread = threading.Thread(target=_run, name='StaticRoutes-Startup', daemon=True)
    thread.start()
    return thread
