"""图片附件 OCR 派生文本参与证据链路。

从 test_evidence_zip_search.py 的模式拆出：复用假 Redmine fixture，
验证 image 附件在快照下载时被 OCR 成可检索派生文本
（``derived_text_path`` + ``text_available``），以及 OCR 依赖缺失 /
零文本两种失败显性转 partial（不允许静默丢截图——#656295/#655644
复盘：截图不可读是取证未闭环的主要来源）。
"""

from __future__ import annotations

import asyncio
import io
import sys
import unittest
from unittest.mock import patch

from features.redmine.tests.test_evidence_pipeline import (
    EvidencePipelineTests,
    FakeRedmineServer,
)


def _tiny_png_bytes() -> bytes:
    """渲染一行大号 ASCII 文本的 PNG，保证真实 tesseract 可稳定识别。"""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (560, 72), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28
        )
    except OSError:
        font = ImageFont.load_default(size=28)
    draw.text((8, 16), "FATAL EXCEPTION 21:37:01", fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class ImageDerivedTextTests(EvidencePipelineTests):
    def _fetch_with_png(self, owner_id: str = "owner-a", png: bytes | None = None):
        from features.redmine.evidence import EvidenceFetcher
        from features.redmine.evidence_store import owner_evidence_store

        if png is None:
            png = _tiny_png_bytes()
        with FakeRedmineServer() as fake:
            fake.issue_document["issue"]["attachments"].append(
                {
                    "id": 776701,
                    "filename": "clipboard-failure.png",
                    "filesize": len(png),
                    "content_type": "image/png",
                    "content_url": "",
                    "created_on": "2026-10-08T07:40:00Z",
                    "author": {"id": 8, "name": "锐益达"},
                }
            )
            fake.attachment_payloads["776701"] = png
            issue_id = int(fake.issue_document["issue"]["id"])
            with self._patched_config(fake.base_url):
                fetcher = EvidenceFetcher(owner_id)
                result = asyncio.run(
                    fetcher.run(fetcher.create_snapshot(issue_id, download="all"))
                )
        return result, owner_evidence_store(owner_id)

    def test_image_attachment_gets_ocr_derived_text(self):
        result, store = self._fetch_with_png()
        self.assertEqual(result["status"], "ready")
        artifact = store.get_artifact_by_attachment_id("776701")
        self.assertIsNotNone(artifact)
        self.assertEqual(str(artifact.get("kind")), "image")
        self.assertEqual(str(artifact.get("status")), "ready")
        self.assertTrue(artifact.get("text_available"))
        # API dict 不带内部路径（_artifact_from_row 剥离）；与 zip 测试一致，
        # 直接从 DB 取派生路径。
        with store._connect() as conn:
            row = conn.execute(
                "SELECT derived_text_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (str(artifact["artifact_id"]),),
            ).fetchone()
        derived_rel = str(row["derived_text_path"] or "") if row else ""
        self.assertTrue(
            derived_rel,
            f"status={artifact.get('status')} error={artifact.get('error')!r} "
            f"snapshot_errors={result.get('errors')}",
        )
        derived = store.resolve_internal(derived_rel).read_text(encoding="utf-8")
        self.assertIn("21:37:01", derived)

    def test_image_ocr_empty_result_stays_ready_without_text(self):
        """图片本身无文字（示意图/照片）：OCR 正常执行、零产出 → 保持
        ready 且无派生文本，不翻 partial（否则快照噪声化）。"""
        with patch(
            "pytesseract.image_to_string",
            lambda *args, **kwargs: "  \n",
        ):
            result, store = self._fetch_with_png()
        self.assertEqual(result["status"], "ready")
        artifact = store.get_artifact_by_attachment_id("776701")
        self.assertEqual(str(artifact.get("status")), "ready")
        self.assertEqual(str(artifact.get("error") or ""), "")
        self.assertFalse(artifact.get("text_available"))
        self.assertFalse(str(artifact.get("derived_text_path") or ""))

    def test_image_ocr_dependency_missing_is_explicit_not_fatal(self):
        # sys.modules 注入 None 让 import pytesseract / PIL 直接 ImportError，
        # 模拟无 OCR 依赖的部署：PNG 先在 patch 上下文外渲染好，附件本体
        # 保持 ready 之外的显性 partial，原因写明"依赖缺失"，不阻塞其余附件。
        png = _tiny_png_bytes()
        with patch.dict(sys.modules, {"pytesseract": None, "PIL": None}):
            result, store = self._fetch_with_png(png=png)
        self.assertEqual(result["status"], "partial")
        artifact = store.get_artifact_by_attachment_id("776701")
        self.assertEqual(str(artifact.get("status")), "partial")
        self.assertIn("OCR 依赖缺失", str(artifact.get("error") or ""))
        self.assertFalse(artifact.get("text_available"))

    def test_image_ocr_timeout_is_partial_and_keeps_other_attachments(self):
        from features.redmine.evidence_images import OCR_TIMEOUT_SECONDS

        def timeout(_image, *, lang, timeout):
            self.assertEqual(lang, "chi_sim+eng")
            self.assertEqual(timeout, OCR_TIMEOUT_SECONDS)
            raise RuntimeError("Tesseract process timeout")

        with patch("pytesseract.image_to_string", side_effect=timeout):
            result, store = self._fetch_with_png()
        self.assertEqual(result["status"], "partial")
        artifact = store.get_artifact_by_attachment_id("776701")
        self.assertEqual(artifact["status"], "partial")
        self.assertIn("timeout", artifact["error"])
        # The fixture has other text attachments; an OCR error must not
        # prevent their download or the snapshot reaching its terminal state.
        self.assertGreater(result["downloaded_count"], 1)
        self.assertEqual(store.get_artifact_by_attachment_id("776656")["status"], "ready")

    def test_image_pixel_budget_rejects_before_ocr(self):
        with patch("features.redmine.evidence_images.OCR_MAX_IMAGE_PIXELS", 100), patch(
            "pytesseract.image_to_string", return_value="",
        ) as ocr:
            result, store = self._fetch_with_png()
        for call in ocr.call_args_list:
            image = call.args[0]
            self.assertLessEqual(image.width * image.height, 100)
        self.assertEqual(result["status"], "partial")
        artifact = store.get_artifact_by_attachment_id("776701")
        self.assertEqual(artifact["status"], "partial")
        self.assertIn("像素超过上限", artifact["error"])


if __name__ == "__main__":
    unittest.main()
