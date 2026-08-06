import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.cluster.worker_auth import (
    persist_worker_token,
    restore_worker_token,
    revoke_worker_token,
    worker_tokens,
    write_worker_tokens,
)


class WorkerAuthPersistenceTests(unittest.TestCase):
    def test_tokens_are_persisted_in_dedicated_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "worker_tokens.json"
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(token_path)},
            ):
                write_worker_tokens({
                    "worker-2": "token two",
                    "worker-1": "token-one",
                })
                parsed = worker_tokens()

            mode = stat.S_IMODE(token_path.stat().st_mode)
            data = json.loads(token_path.read_text(encoding="utf-8"))

        self.assertEqual(mode, 0o600)
        self.assertEqual(
            data["worker_tokens"],
            {"worker-1": "token-one", "worker-2": "token two"},
        )
        self.assertEqual(parsed, data["worker_tokens"])

    def test_missing_dedicated_file_returns_empty_map(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "missing.json"
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(token_path)},
            ):
                parsed = worker_tokens()
        self.assertEqual(parsed, {})

    def test_cluster_config_tokens_are_not_read_or_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            cluster_path = Path(directory) / "cluster.json"
            token_path = Path(directory) / "worker_tokens.json"
            cluster_data = {
                "enabled": True,
                "worker_tokens": {"worker-legacy": "legacy-token"},
            }
            cluster_path.write_text(
                json.dumps(cluster_data),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "GMS_CLUSTER_CONFIG": str(cluster_path),
                    "GMS_WORKER_TOKENS_FILE": str(token_path),
                },
            ):
                self.assertEqual(worker_tokens(), {})
                write_worker_tokens({"worker-current": "current-token"})

            unchanged_cluster = json.loads(
                cluster_path.read_text(encoding="utf-8")
            )
            token_data = json.loads(token_path.read_text(encoding="utf-8"))

        self.assertEqual(unchanged_cluster, cluster_data)
        self.assertEqual(
            token_data["worker_tokens"],
            {"worker-current": "current-token"},
        )

    def test_direct_map_without_latest_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "worker_tokens.json"
            token_path.write_text(
                json.dumps({"worker-1": "token-one"}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(token_path)},
            ):
                parsed = worker_tokens()
        self.assertEqual(parsed, {})

    def test_single_worker_updates_preserve_peer_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "worker_tokens.json"
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(token_path)},
            ):
                write_worker_tokens({"worker-1": "one", "worker-2": "two"})
                previous = persist_worker_token("worker-1", "replacement")
                revoked = revoke_worker_token("worker-2")

                self.assertEqual(previous, "one")
                self.assertTrue(revoked)
                self.assertEqual(worker_tokens(), {"worker-1": "replacement"})

    def test_deployment_rollback_does_not_overwrite_newer_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "worker_tokens.json"
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(token_path)},
            ):
                write_worker_tokens({"worker-1": "original", "worker-2": "peer"})
                previous = persist_worker_token("worker-1", "deploy-a")
                persist_worker_token("worker-1", "deploy-b")

                restored = restore_worker_token("worker-1", "deploy-a", previous)

                self.assertFalse(restored)
                self.assertEqual(
                    worker_tokens(),
                    {"worker-1": "deploy-b", "worker-2": "peer"},
                )


if __name__ == "__main__":
    unittest.main()
