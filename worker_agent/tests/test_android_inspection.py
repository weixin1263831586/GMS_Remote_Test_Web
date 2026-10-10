"""``validate_export_path`` 的注入防线用例。

``adb exec-out cat <path>`` 会把参数拼成一行交给设备端 shell 二次解析，
路径中的 shell 元字符（``;``、``|``、``$()``、反引号、空格、glob）必须被
字符白名单整体拒绝，而不是只拦控制字符。
"""

import pytest

from worker_agent.android_inspection import _product_mount_is_rw, validate_export_path


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


_MOUNT_OUTPUT_RW = "\n".join([
    "/dev/block/by-name/system on /system type ext4 (ro,seclabel,relatime)",
    "tmpfs on /product type tmpfs (rw,seclabel,relatime,size=...)",
    "/dev/block/by-name/metadata on /metadata type ext4 (rw,seclabel,nosuid,nodev)",
])


def test_product_mount_is_rw_strips_parenthesized_options():
    # 旧实现对 "(rw" 整串做成员判断恒为 False；必须剥括号后精确匹配。
    assert _product_mount_is_rw(_MOUNT_OUTPUT_RW) is True


def test_product_mount_is_rw_false_for_ro_product():
    output = ("/dev/block/by-name/product on /product type ext4 "
              "(ro,seclabel,relatime)")
    assert _product_mount_is_rw(output) is False


def test_product_mount_is_rw_no_product_mount():
    output = "/dev/block/by-name/system on /system type ext4 (rw,seclabel)"
    assert _product_mount_is_rw(output) is False
