"""GMS Remote Test - API 路由注册"""

from features.automation import api as automation
from features.devices import api as devices
from features.devices import integrations_api as device_integrations
from features.firmware import api as firmware
from features.gerrit import api as gerrit_dashboard
from features.redmine import api as redmine
from features.reports import api as reports
from features.system.mainline_issues import api as mainline_known_issues
from features.system.update_monitor import api as gms_update_monitor
from features.test_execution import api as tests

from . import (
    agent,
    assets,
    audit,
    config,
    desktop,
    integrations,
    notifications,
    system,
    terminal,
    users,
)


ALL_ROUTERS = [
    agent.router,
    assets.router,
    audit.router,
    automation.router,
    automation.page_router,
    config.router,
    desktop.router,
    devices.router,
    device_integrations.router,
    firmware.router,
    gerrit_dashboard.router,
    gerrit_dashboard.page_router,
    gms_update_monitor.router,
    gms_update_monitor.page_router,
    integrations.router,
    mainline_known_issues.router,
    notifications.router,
    redmine.router,
    redmine.page_router,
    reports.router,
    system.router,
    terminal.router,
    tests.router,
    users.router,
]
