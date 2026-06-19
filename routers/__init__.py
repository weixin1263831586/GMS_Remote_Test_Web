"""GMS Remote Test - API 路由注册"""

from features.automation import api as automation
from features.assistant import api as assistant
from features.devices import api as devices
from features.devices import integrations_api as device_integrations
from features.firmware import api as firmware
from features.gerrit import api as gerrit_dashboard
from features.redmine import api as redmine
from features.reports import api as reports
from features.system import api as system
from features.system import assets, audit, desktop, integrations
from features.system.mainline_issues import api as mainline_known_issues
from features.system import notifications_api as notifications
from features.system import terminal_api as terminal
from features.system.update_monitor import api as gms_update_monitor
from features.test_execution import api as tests
from features.users import api as users


ALL_ROUTERS = [
    assistant.router,
    assets.router,
    audit.router,
    automation.router,
    automation.page_router,
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
