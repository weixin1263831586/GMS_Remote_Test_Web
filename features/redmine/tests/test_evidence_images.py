"""图片附件 OCR 派生文本参与证据链路。

从 test_evidence_zip_search.py 的模式拆出：复用假 Redmine fixture，
验证 image 附件在快照下载时被 OCR 成可检索派生文本
（``derived_text_path`` + ``text_available`` + ``derived_status``）。
OCR 依赖缺失 / 超时 / 像素超限只把派生状态记为 missing 并写入快照
derive 阶段错误，原件保持 ready、附件原始可用状态不被派生失败覆盖；
OCR 正常但零文本记 empty（示意图/照片是
常态，不是证据缺口——#656295/#655644 复盘只要求"未识别"显性可见）。
"""

from __future__ import annotations

import asyncio
import io
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine.tests.test_evidence_pipeline import (
    EvidencePipelineFixture,
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


class ImageDerivedTextTests(EvidencePipelineFixture, unittest.TestCase):
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
        self.assertEqual(artifact.get("derived_status"), "ready")
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
        self.assertEqual(artifact.get("derived_status"), "empty")
        self.assertFalse(artifact.get("text_available"))
        self.assertFalse(str(artifact.get("derived_text_path") or ""))

    def test_image_ocr_dependency_missing_is_explicit_not_fatal(self):
        # 在模块依赖加载入口注入失败：extract_image_ocr_text 经
        # _load_ocr_dependencies() 取依赖，这里让它直接抛
        # ModuleNotFoundError，模拟无 OCR 依赖的部署。不操纵
        # sys.modules / builtins.__import__（进程级全局状态，OCR 又在
        # 工作线程执行，组合运行下会互相污染——外部复审 P2）。
        # PNG 原件保持 ready（字节完整落盘），派生状态记 missing、
        # 原因写明"依赖缺失"并进入快照 derive 阶段错误，不阻塞其余
        # 附件，也不把快照整体拉成 partial。
        from features.redmine import evidence_images

        def _no_ocr():
            raise ModuleNotFoundError("No module named 'pytesseract' (test)")

        with patch.object(evidence_images, "_load_ocr_dependencies", _no_ocr):
            result, store = self._fetch_with_png(png=_tiny_png_bytes())
        self.assertEqual(result["status"], "ready")
        self.assertTrue(
            any(item.get("stage") == "derive" for item in result["errors"])
        )
        artifact = store.get_artifact_by_attachment_id("776701")
        self.assertEqual(str(artifact.get("status")), "ready")
        self.assertEqual(artifact.get("derived_status"), "missing")
        self.assertIn("OCR 依赖缺失", str(artifact.get("error") or ""))
        self.assertFalse(artifact.get("text_available"))

    def test_image_ocr_timeout_records_missing_and_keeps_other_attachments(self):
        from features.redmine.evidence import snapshot_completeness
        from features.redmine.evidence_images import OCR_TIMEOUT_SECONDS

        def timeout(_image, *, lang, timeout):
            self.assertEqual(lang, "chi_sim+eng")
            self.assertEqual(timeout, OCR_TIMEOUT_SECONDS)
            raise RuntimeError("Tesseract process timeout")

        with patch("pytesseract.image_to_string", side_effect=timeout):
            result, store = self._fetch_with_png()
        # 快照终态 ready（下载全部成功）；派生缺口由 completeness 的
        # derived_text 维度如实反映，而不是伪造"证据不完整"。
        self.assertEqual(result["status"], "ready")
        self.assertFalse(snapshot_completeness(result)["derived_text"])
        artifact = store.get_artifact_by_attachment_id("776701")
        self.assertEqual(artifact["status"], "ready")
        self.assertEqual(artifact["derived_status"], "missing")
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
        self.assertEqual(result["status"], "ready")
        artifact = store.get_artifact_by_attachment_id("776701")
        self.assertEqual(artifact["status"], "ready")
        self.assertEqual(artifact["derived_status"], "missing")
        self.assertIn("像素超过上限", artifact["error"])

    def test_derived_text_truncation_stays_on_utf8_boundary(self):
        """中文 OCR 长文本按字节截断后必须仍是合法 UTF-8。"""
        from pathlib import Path

        from features.redmine.evidence_images import extract_image_ocr_text
        from features.redmine.evidence_store import EvidenceStore

        long_text = "崩溃日志一二三四五六七八九十" * 500
        with patch(
            "pytesseract.image_to_string", lambda *args, **kwargs: long_text,
        ), patch(
            "features.redmine.evidence_images._DERIVED_TEXT_MAX_BYTES", 997,
        ), TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory))
            png_path = Path(directory) / "shot.png"
            png_path.write_bytes(_tiny_png_bytes())

            def atomic_write(path: Path, data: bytes) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

            derived_rel, error = extract_image_ocr_text(
                store, atomic_write, "art_trunc", "issue/snap", png_path,
            )
            self.assertEqual(error, "")
            payload = store.resolve_internal(derived_rel).read_bytes()
        self.assertLessEqual(len(payload), 997)
        # 严格解码：截断点落在汉字中间会在这里抛 UnicodeDecodeError。
        decoded = payload.decode("utf-8")
        self.assertEqual(decoded, long_text[: len(decoded)])


if __name__ == "__main__":
    unittest.main()
