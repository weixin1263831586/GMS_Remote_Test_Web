"""Worker 命令终态通知（start_test/firmware/GSI）单元测试。"""

import unittest
from unittest import mock

from features.cluster import commands_api


def _command(command_type="start_test", status="completed", **overrides):
    base = {
        "id": "cmd-test-1",
        "command_type": command_type,
        "status": status,
        "worker_id": "worker-B",
        "job_id": "job-1",
        "attempt_id": "attempt-1",
        "error": "",
        "result": {},
        "payload": {"owner_id": "owner-1", "devices": ["worker-B:DEV1"]},
    }
    base.update(overrides)
    return base


def _job(owner="owner-1"):
    return {
        "owner_id": owner,
        "assigned_worker_id": "worker-B",
        "request": {"suite_key": "CTS:17_r2", "devices": ["worker-B:DEV1"]},
        "leases": [],
    }


class StartTestNotificationTests(unittest.TestCase):
    def test_completed_start_test_notifies_owner(self):
        with mock.patch.object(commands_api, "service") as service, \
                mock.patch("features.system.queue_notification") as queue, \
                mock.patch("features.cluster.report_index.update_cluster_report_status"):
            service().repository.claim_terminal_notification.return_value = True
            service().repository.get_job.return_value = _job()
            commands_api.synchronize_command(_command())
            queue.assert_called_once()
            args = queue.call_args
            # owner / 标题 / 内容 / 级别 / 分类 / 元数据
            self.assertEqual(args.args[0], "owner-1")
            self.assertIn("测试完成", args.args[1])
            self.assertIn("worker-B", args.args[2])
            self.assertEqual(args.args[3], "success")
            meta = args.args[5] if len(args.args) > 5 else args.kwargs.get("extra")
            self.assertEqual(meta.get("job_id"), "job-1")
            self.assertEqual(meta.get("worker_id"), "worker-B")
            self.assertEqual(meta.get("status"), "completed")

    def test_no_owner_skips_notification(self):
        with mock.patch.object(commands_api, "service") as service, \
                mock.patch("features.system.queue_notification") as queue, \
                mock.patch("features.cluster.report_index.update_cluster_report_status"):
            service().repository.claim_terminal_notification.return_value = True
            service().repository.get_job.return_value = _job(owner="")
            commands_api.synchronize_command(_command())
            queue.assert_not_called()

    def test_duplicate_ack_claimed_once(self):
        """心跳对账会重复 ACK；claim_terminal_notification 返回 False 时不重复通知。"""
        with mock.patch.object(commands_api, "service") as service, \
                mock.patch("features.system.queue_notification") as queue, \
                mock.patch("features.cluster.report_index.update_cluster_report_status"):
            service().repository.claim_terminal_notification.return_value = False
            service().repository.get_job.return_value = _job()
            commands_api.synchronize_command(_command())
            queue.assert_not_called()

    def test_failed_start_test_uses_error_level(self):
        with mock.patch.object(commands_api, "service") as service, \
                mock.patch("features.system.queue_notification") as queue, \
                mock.patch("features.cluster.report_index.update_cluster_report_status"):
            service().repository.claim_terminal_notification.return_value = True
            service().repository.get_job.return_value = _job()
            commands_api.synchronize_command(
                _command(status="failed", error="module not found in suite: X")
            )
            args = queue.call_args
            self.assertIn("测试失败", args.args[1])
            self.assertEqual(args.args[3], "error")
            self.assertIn("module not found in suite: X", args.args[2])

    def test_running_status_does_not_notify(self):
        with mock.patch.object(commands_api, "service") as service, \
                mock.patch("features.system.queue_notification") as queue:
            service().repository.claim_terminal_notification.return_value = True
            commands_api.synchronize_command(_command(status="running"))
            queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
