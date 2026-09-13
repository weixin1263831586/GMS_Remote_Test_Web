"""usbip_assignments 存储层特征测试。

锁定从 integrations_api.py 抽出时的既有行为(characterization),防止
后续状态机演进时无意改变 assignment 一致性规则:

- load/save 走 runtime_config["usbip_cluster_assignments"];
- prune 只清 stale unknown 且 BUSID 已消失的条目;
- reconcile 只回填 serial/source_os,local worker 的 serial 透传持久化;
- detach-unknown 标记受 (worker_id, generation) CAS 保护,旧代不得覆盖;
- generation 单调(>= 现有最大值+1,且 >= 当前毫秒)。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from features.devices import usbip_assignments as ua


def _config_manager(runtime_config: dict, *, update_error: bool = False):
    manager = MagicMock()
    # 返回实时 dict(setUp 之后对 runtime_config 的写入必须可见)。
    manager.get_runtime_config.side_effect = lambda: runtime_config

    def updater(patch: dict) -> bool:
        if update_error:
            return False
        runtime_config.update(patch)
        return True

    manager.update_runtime_config.side_effect = updater
    return manager


class LoadSaveAssignmentsTests(unittest.TestCase):
    def setUp(self):
        self.runtime_config: dict = {}

    def _patch_runtime(self):
        return patch.object(
            ua.runtime, "config_manager", _config_manager(self.runtime_config)
        )

    def test_load_returns_empty_when_key_missing(self):
        with self._patch_runtime():
            self.assertEqual(ua.load_usbip_assignments(), {})

    def test_load_returns_top_level_copy(self):
        original = {"a|1": {"busid": "1"}}
        self.runtime_config["usbip_cluster_assignments"] = original
        with self._patch_runtime():
            loaded = ua.load_usbip_assignments()
        # 浅拷贝:顶层增删不影响存储,但嵌套 dict 与存储共享(既有语义)。
        loaded["a|1"]["busid"] = "mutated"
        self.assertEqual(original["a|1"]["busid"], "mutated")
        loaded["a|9"] = {}
        self.assertNotIn("a|9", original)

    def test_load_rejects_non_dict_value(self):
        self.runtime_config["usbip_cluster_assignments"] = ["bad"]
        with self._patch_runtime():
            self.assertEqual(ua.load_usbip_assignments(), {})

    def test_save_persists_via_updater(self):
        with self._patch_runtime():
            self.assertTrue(ua.save_usbip_assignments({"a|1": {"busid": "1"}}))
        self.assertEqual(
            self.runtime_config["usbip_cluster_assignments"], {"a|1": {"busid": "1"}}
        )

    def test_save_raises_when_persistence_fails(self):
        with patch.object(
            ua.runtime, "config_manager", _config_manager(self.runtime_config, update_error=True)
        ), self.assertRaises(RuntimeError):
            ua.save_usbip_assignments({})

    def test_assignment_key_format(self):
        self.assertEqual(ua.usbip_assignment_key("host", "2-1"), "host|2-1")

    def test_generation_is_monotonic(self):
        assignments = {
            "a|1": {"generation": 1000},
            "a|2": {"generation": 5000},
        }
        generation = ua.next_transport_generation(assignments)
        self.assertGreater(generation, 5000)
        # 空 assignment 也返回一个毫秒级值。
        self.assertGreater(ua.next_transport_generation({}), int(1e12))


class PruneStaleUnknownTests(unittest.TestCase):
    def setUp(self):
        self.runtime_config: dict = {}
        self.patches = [
            patch.object(
                ua.runtime, "config_manager", _config_manager(self.runtime_config)
            ),
            patch.object(ua, "usbip_assignment_lock", MagicMock()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_prunes_unknown_assignments_with_gone_busids(self):
        self.runtime_config["usbip_cluster_assignments"] = {
            "host|1": {"device_host": "host", "busid": "1", "status": "unknown"},
            "host|2": {"device_host": "host", "busid": "2", "status": "unknown"},
            "host|3": {"device_host": "host", "busid": "3", "status": "attached"},
            "host|4": {"device_host": "other", "busid": "4", "status": "unknown"},
        }
        stale = ua.prune_stale_unknown_usbip_assignments("host", {"2"})
        self.assertEqual(stale, ["host|1"])
        kept = self.runtime_config["usbip_cluster_assignments"]
        self.assertNotIn("host|1", kept)
        for key in ("host|2", "host|3", "host|4"):
            self.assertIn(key, kept)

    def test_no_changes_leaves_storage_untouched(self):
        self.runtime_config["usbip_cluster_assignments"] = {
            "host|1": {"device_host": "host", "busid": "1", "status": "attached"},
        }
        stale = ua.prune_stale_unknown_usbip_assignments("host", {"1"})
        self.assertEqual(stale, [])


class ReconcileSerialsTests(unittest.TestCase):
    def setUp(self):
        self.runtime_config: dict = {}
        self.persisted: list = []
        self.patches = [
            patch.object(
                ua.runtime, "config_manager", _config_manager(self.runtime_config)
            ),
            patch.object(ua, "usbip_assignment_lock", MagicMock()),
            patch.object(
                ua, "persist_local_usbip_sources",
                side_effect=lambda host, serials, source_os="": self.persisted.append(
                    (host, list(serials), source_os)
                ),
            ),
            patch.object(ua, "get_local_worker_id", return_value="local-w1"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_backfills_serial_and_source_os(self):
        self.runtime_config["usbip_cluster_assignments"] = {
            "host|1": {
                "device_host": "host", "busid": "1", "worker_id": "remote",
            },
        }
        changed = ua.reconcile_usbip_assignment_serials(
            "host",
            [{"busid": "1", "serial": "SER1"}],
            source_os="windows",
        )
        self.assertTrue(changed)
        entry = self.runtime_config["usbip_cluster_assignments"]["host|1"]
        self.assertEqual(entry["device_serials"], ["SER1"])
        self.assertEqual(entry["source_os"], "windows")
        self.assertEqual(self.persisted, [("host", [], "windows")])  # remote worker: persist 收到空 serials(既有语义)

    def test_local_worker_serials_are_forwarded(self):
        self.runtime_config["usbip_cluster_assignments"] = {
            "host|1": {
                "device_host": "host", "busid": "1", "worker_id": "local-w1",
            },
        }
        ua.reconcile_usbip_assignment_serials(
            "host", [{"busid": "1", "serial": "SER1"}], source_os="windows"
        )
        self.assertEqual(self.persisted, [("host", ["SER1"], "windows")])

    def test_no_serial_sources_is_noop(self):
        self.assertFalse(ua.reconcile_usbip_assignment_serials("host", [], "windows"))


class MarkDetachUnknownTests(unittest.TestCase):
    def setUp(self):
        self.runtime_config: dict = {}
        self.patches = [
            patch.object(
                ua.runtime, "config_manager", _config_manager(self.runtime_config)
            ),
            patch.object(ua, "usbip_assignment_lock", MagicMock()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_marks_matching_worker_and_generation_only(self):
        self.runtime_config["usbip_cluster_assignments"] = {
            "host|1": {"device_host": "host", "busid": "1",
                       "worker_id": "w1", "generation": 7},
            "host|2": {"device_host": "host", "busid": "2",
                       "worker_id": "w1", "generation": 9},
        }
        ua.mark_usbip_detach_unknown("host", ["1", "2"], "w1", 7)
        entries = self.runtime_config["usbip_cluster_assignments"]
        self.assertEqual(entries["host|1"]["status"], "unknown")
        # generation 不匹配(CAS)的条目不受影响。
        self.assertNotIn("status", entries["host|2"])

    def test_unknown_worker_entries_are_untouched(self):
        self.runtime_config["usbip_cluster_assignments"] = {
            "host|1": {"device_host": "host", "busid": "1",
                       "worker_id": "w2", "generation": 7},
        }
        ua.mark_usbip_detach_unknown("host", ["1"], "w1", 7)
        entries = self.runtime_config["usbip_cluster_assignments"]
        self.assertNotIn("status", entries["host|1"])


if __name__ == "__main__":
    unittest.main()
