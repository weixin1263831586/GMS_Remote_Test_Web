"""终端分片上传的 chunk 范围校验回归。

审核项：/api/terminal/push 的 chunk_index/total_chunks 来自表单且未校验，
负值/倒置/天文数字会触发 range(total_chunks) 全量轮询与畸形分片文件名。
"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from features.system import terminal_api


class TerminalChunkRangeTests(unittest.IsolatedAsyncioTestCase):
    async def _upload(self, chunk_index, total_chunks):
        return await terminal_api._upload_file_chunk(
            file=None,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            upload_id="upload-1",
            file_name="firmware.img",
            file_size=None,
            resume=None,
        )

    async def test_rejects_out_of_range_chunk_shapes(self):
        cases = [
            (-1, 4),      # 负索引
            (4, 4),       # 索引 == 总数
            (5, 4),       # 索引越界
            (0, 0),       # 总数为零
            (0, -1),      # 总数为负
            (0, terminal_api._MAX_UPLOAD_CHUNKS + 1),  # 总数超上限
        ]
        for chunk_index, total_chunks in cases:
            with self.subTest(chunk_index=chunk_index, total_chunks=total_chunks):
                response = await self._upload(chunk_index, total_chunks)
                self.assertEqual(response.status_code, 400)

    async def test_boundary_values_pass_range_check(self):
        # 范围合法（0 起始、索引 < 总数）继续走上传流程；file=None 时
        # 保存阶段报错（经 record_internal_error 映射 500），证明未被
        # 范围校验拦截。upload 临时根切到临时目录，不碰运行时数据。
        with tempfile.TemporaryDirectory() as tmp, patch.object(terminal_api, "upload_temp_root", return_value=str(tmp)):
            response = await self._upload(0, 2)
        self.assertNotEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
