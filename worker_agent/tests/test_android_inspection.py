"""``validate_export_path`` 的注入防线用例。

``adb exec-out cat <path>`` 会把参数拼成一行交给设备端 shell 二次解析，
路径中的 shell 元字符（``;``、``|``、``$()``、反引号、空格、glob）必须被
字符白名单整体拒绝，而不是只拦控制字符。
"""

import pytest

from worker_agent.android_inspection import validate_export_path


def test_export_path_accepts_canonical_apk_path():
    assert validate_export_path("/system/app/Settings/Settings.apk") == (
        "/system/app/Settings/Settings.apk"
    )


@pytest.mark.parametrize("bad", [
    "/system/app/x;reboot/x.apk",
    "/system/app/$(reboot)/x.apk",
    "/system/app/`id`/x.apk",
    "/system/app/a b/x.apk",
    "/system/app/a|b/x.apk",
    "/system/app/a&b/x.apk",
    "/system/app/a>b/x.apk",
    "/system/app/*.apk",
    "/system/app/it's/x.apk",
])
def test_export_path_rejects_shell_metacharacters(bad):
    with pytest.raises(ValueError):
        validate_export_path(bad)
