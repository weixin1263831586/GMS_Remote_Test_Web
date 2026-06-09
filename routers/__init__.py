"""GMS Remote Test - API 路由注册"""

from . import (
    agent,
    apk,
    assets,
    audit,
    config,
    desktop,
    devices,
    firmware,
    integrations,
    notifications,
    reports,
    system,
    terminal,
    tests,
    users,
)

ALL_ROUTERS = [
    agent.router,
    notifications.router,
    audit.router,
    assets.router,
    integrations.router,
    users.router,
    config.router,
    desktop.router,
    devices.router,
    tests.router,
    reports.router,
    firmware.router,
    apk.router,
    terminal.router,
    system.router,
]
