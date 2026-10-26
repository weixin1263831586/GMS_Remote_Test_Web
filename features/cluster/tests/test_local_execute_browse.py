"""本机 Worker 桥 file_browse 的浏览边界回归。

远端 Worker 的 file_browse 已限制在套件根/数据根内（worker_agent
device_actions.browse_directory 的 allowed_roots）；本机桥此前未传
allowed_roots，等于回退到「任意目录可列」。这里钉住桥与远端同边界。
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException

from features.cluster import api as cluster_api


class LocalFileBrowseRootsTests(unittest.TestCase):
    def test_file_browse_passes_suite_roots_as_allowed_roots(self):
        with TemporaryDirectory() as tmp:
            inside = Path(tmp) / "suites"
            inside.mkdir()
            with patch(
                "features.cluster.local_bridge._suite_roots",
                return_value=[str(inside)],
            ), patch(
                "worker_agent.device_actions.browse_directory"
            ) as browse:
                cluster_api._local_execute("file_browse", {"path": ""})
            self.assertEqual(
                [inside], [Path(str(root)) for root in browse.call_args.kwargs["allowed_roots"]]
            )

    def test_file_browse_rejects_paths_outside_suite_roots(self):
        with TemporaryDirectory() as tmp:
            inside = Path(tmp) / "suites"
            inside.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "listing.txt").write_text("x", encoding="utf-8")
            with patch(
                "features.cluster.local_bridge._suite_roots",
                return_value=[str(inside)],
            ), self.assertRaises(HTTPException):
                cluster_api._local_execute(
                    "file_browse", {"path": str(outside)}
                )

    def test_file_browse_allows_paths_inside_suite_roots(self):
        with TemporaryDirectory() as tmp:
            inside = Path(tmp) / "suites"
            (inside / "android-cts").mkdir(parents=True)
            with patch(
                "features.cluster.local_bridge._suite_roots",
                return_value=[str(inside)],
            ):
                result = cluster_api._local_execute(
                    "file_browse", {"path": str(inside)}
                )
            names = [item["name"] for item in result["files"]]
            self.assertIn("android-cts", names)


if __name__ == "__main__":
    unittest.main()
