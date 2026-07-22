import json
import stat
import tempfile
import unittest
from pathlib import Path

from features.cluster.worker_auth import worker_tokens, write_worker_tokens


class WorkerAuthPersistenceTests(unittest.TestCase):
    def test_worker_tokens_persisted_into_cluster_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            write_worker_tokens({"worker-2": "token two", "worker-1": "token-one"}, path)
            parsed = worker_tokens(path)

            mode = stat.S_IMODE(path.stat().st_mode)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(mode, 0o600)
        # Tokens land under the worker_tokens key, sorted on write.
        self.assertEqual(
            data["worker_tokens"],
            {"worker-1": "token-one", "worker-2": "token two"},
        )
        self.assertEqual(parsed, {"worker-1": "token-one", "worker-2": "token two"})

    def test_existing_cluster_config_keys_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            path.write_text(
                json.dumps({"enabled": True, "local_worker_id": "worker-local"}),
                encoding="utf-8",
            )
            write_worker_tokens({"worker-1": "token-one"}, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            parsed = worker_tokens(path)

        self.assertEqual(data["enabled"], True)
        self.assertEqual(data["local_worker_id"], "worker-local")
        self.assertEqual(data["worker_tokens"], {"worker-1": "token-one"})
        self.assertEqual(parsed, {"worker-1": "token-one"})

    def test_missing_file_returns_empty_map(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = worker_tokens(Path(directory) / "missing.json")
        self.assertEqual(parsed, {})


if __name__ == "__main__":
    unittest.main()
