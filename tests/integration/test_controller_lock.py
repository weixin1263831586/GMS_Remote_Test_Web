from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from foundation.controller_lock import controller_process_lock


class ControllerLockTests(unittest.TestCase):
    def test_lock_is_reentrant_in_process_and_rejects_second_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with controller_process_lock(root), controller_process_lock(root):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "from foundation.controller_lock import controller_process_lock; "
                            f"root=Path({str(root)!r}); "
                            "ctx=controller_process_lock(root); "
                            "\ntry:\n ctx.__enter__()\nexcept RuntimeError:\n raise SystemExit(0)"
                            "\nraise SystemExit(1)"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
