"""烧写恢复完成后的本机库存立即刷新（不等守护进程下一个心跳）。

刷新职责位于 features/cluster/firmware_recovery.refresh_post_burn_inventory：
run_local_firmware_recovery 的两个返回路径（完成/派发失败）都会触发。
本文件锁定两条契约：
- 恢复结束后库存被立即写入（probe → refresh_devices）；
- probe/写库失败只告警，绝不影响恢复结果。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.cluster import firmware_recovery


def _sync_to_thread(fn, *args):
    async def run():
        return fn(*args)
    return run()


class PostBurnInventoryRefreshTests(unittest.TestCase):
    def test_refreshes_inventory_after_recovery(self):
        devices = [{"serial": "D1", "state": "available", "transport": "local_usb"}]
        refreshed = []
        repository = SimpleNamespace(
            refresh_devices=lambda wid, devs: refreshed.append((wid, devs)))

        with patch(
            "features.cluster.local_bridge._probe_devices", return_value=devices
        ), patch("asyncio.to_thread", new=_sync_to_thread):
            firmware_recovery.refresh_post_burn_inventory(repository, "local-worker")

        assert refreshed == [("local-worker", devices)]

    def test_refresh_failure_never_raises(self):
        repository = SimpleNamespace(refresh_devices=lambda wid, devs: None)

        with patch(
            "features.cluster.local_bridge._probe_devices",
            side_effect=RuntimeError("probe boom"),
        ), patch("asyncio.to_thread", new=_sync_to_thread):
            # 不得抛出：刷新是 best-effort。
            firmware_recovery.refresh_post_burn_inventory(repository, "local-worker")


if __name__ == "__main__":
    unittest.main()
