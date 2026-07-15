import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.cluster.worker_auth import worker_tokens, write_worker_tokens


class WorkerAuthPersistenceTests(unittest.TestCase):
    def test_worker_tokens_are_shell_safe_and_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.production"
            with patch.dict(os.environ, {}, clear=True):
                write_worker_tokens({"worker-2": "token two", "worker-1": "token-one"}, path)
                parsed = worker_tokens()

            mode = stat.S_IMODE(path.stat().st_mode)
            content = path.read_text(encoding="utf-8")

        self.assertEqual(mode, 0o600)
        self.assertIn("GMS_CLUSTER_WORKER_TOKENS=", content)
        self.assertEqual(parsed, {"worker-1": "token-one", "worker-2": "token two"})


if __name__ == "__main__":
    unittest.main()
