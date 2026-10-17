"""client_hosts 读改写并发回归。

detect_username/set-username 都是"读旧表 → 加一条 → 整表写回"的老模式，
两个并发探测会互相覆盖丢条目。现在单条 host 写入必须走
ConfigManager.save_client_host 的锁内临界区（与
delete_client_ssh_credential 同一约定），并发探测一个都不能丢。
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from foundation.config import ConfigManager


def _concurrent_save(manager: ConfigManager, threads: int, per_thread: int) -> None:
    barrier = threading.Barrier(threads)

    def worker(idx: int) -> None:
        barrier.wait()
        for n in range(per_thread):
            manager.save_client_host(f"192.0.2.{idx}-{n}", f"user{idx}-{n}")

    spawned = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in spawned:
        t.start()
    for t in spawned:
        t.join()


class ClientHostConcurrencyTests(unittest.TestCase):
    def test_save_client_host_keeps_every_concurrent_entry(self):
        with TemporaryDirectory() as tmp:
            manager = ConfigManager(project_root=Path(tmp))
            _concurrent_save(manager, threads=4, per_thread=3)
            hosts = manager.get_runtime_config().get("client_hosts") or {}
            for idx in range(4):
                for n in range(3):
                    self.assertEqual(hosts.get(f"192.0.2.{idx}-{n}"), f"user{idx}-{n}")

    def test_concurrent_detect_username_never_drops_hosts(self):
        from features.users.sessions import ClientManager

        with TemporaryDirectory() as tmp:
            manager = ConfigManager(project_root=Path(tmp))
            client_manager = ClientManager()
            client_manager.config_manager = manager

            def fake_whoami(client_ip, username, password):
                return f"detected-{client_ip}"

            barrier = threading.Barrier(3)

            def worker(idx: int) -> None:
                barrier.wait()
                success, name, error = client_manager.detect_username(
                    f"192.0.2.{idx}", f"probe{idx}", "pw"
                )
                assert success and name == f"detected-192.0.2.{idx}", error

            with patch.object(
                ClientManager, "_ssh_whoami", side_effect=fake_whoami
            ):
                spawned = [
                    threading.Thread(target=worker, args=(i,)) for i in range(3)
                ]
                for t in spawned:
                    t.start()
                for t in spawned:
                    t.join()

            hosts = manager.get_runtime_config().get("client_hosts") or {}
            for idx in range(3):
                self.assertEqual(hosts.get(f"192.0.2.{idx}"), f"detected-192.0.2.{idx}")


if __name__ == "__main__":
    unittest.main()
