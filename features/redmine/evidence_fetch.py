"""Evidence fetch engine: ``EvidenceFetcher`` for issue/attachment download.

Split from ``evidence.py`` to keep both modules under the
600-line review limit. All shared limits, errors, credentials helpers and the
owner evidence store stay in ``evidence.py``; this module accesses them via
``evidence.`` attributes where tests rely on runtime patching.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from . import evidence as _evidence
from .evidence import (
    _DERIVED_TEXT_MAX_BYTES,
    ANALYZABLE_KINDS,
    ATTACHMENT_CONNECT_TIMEOUT_SECONDS,
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_READ_TIMEOUT_SECONDS,
    DOWNLOAD_POLICIES,
    MAX_REDIRECTS,
    SNAPSHOT_ISSUE_TIMEOUT_SECONDS,
    SNAPSHOT_MAX_ATTACHMENTS,
    SNAPSHOT_MAX_TOTAL_DOWNLOAD_BYTES,
    EvidenceAuthError,
    EvidenceError,
    EvidenceNotFoundError,
    EvidencePermissionError,
    _attachment_path_ok,
    _classify_attachment,
    _detect_text_encoding,
    _OwnerCredentials,
    _same_origin,
    owner_evidence_store,
)
from .evidence_store import EvidenceStore


logger = logging.getLogger(__name__)


# -------------------------------------------------------------- fetch engine

class EvidenceFetcher:
    """对一个 issue 执行一次完整证据抓取并写入 owner evidence store。"""

    def __init__(self, owner_id: str, store: EvidenceStore | None = None):
        self.owner_id = str(owner_id)
        self.store = store or owner_evidence_store(owner_id)

    # ------------------------------------------------------------- public API

    def create_snapshot(self, issue_id: int, download: str = "none") -> dict[str, Any]:
        if download not in DOWNLOAD_POLICIES:
            raise EvidenceError(f"download 策略必须是 {DOWNLOAD_POLICIES} 之一", status_code=422)
        return self.store.create_snapshot(issue_id=int(issue_id), download_policy=download)

    async def run(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        issue_id = int(snapshot["issue_id"])
        snapshot_id = str(snapshot["snapshot_id"])
        credentials = _evidence.load_owner_credentials(self.owner_id)
        base_url = _evidence.owner_base_url(self.owner_id)
        if not base_url:
            self._fail(
                snapshot_id,
                issue_id,
                "redmine.base_url 未配置；请在 Web UI『设置』页配置 Redmine 地址后再抓取证据",
            )
            raise EvidenceError(
                "Redmine base_url 未配置；请在 Web UI『设置』页配置 Redmine 地址",
                status_code=409,
            )
        if not credentials.headers():
            # 2026-09-11 反馈：错误必须自带修复路径。agent 与 enroll
            # 账号共享 owner 存储，人在 Web UI 为该账号配置即可解除阻断。
            self._fail(
                snapshot_id,
                issue_id,
                "owner 账号未配置 Redmine 凭据；请由 enroll 该 agent 的账号在 Web UI『设置』页"
                "配置用户名/密码或 API Key（agent 与该账号共享 owner 存储），"
                "或由该账号调用 POST /api/redmine-agent/config/credentials；"
                "配置后可用 gms-rt-redmine-credentials-status 验证",
            )
            raise EvidenceAuthError(
                "owner 账号未配置 Redmine 凭据；请由 enroll 该 agent 的账号在 Web UI『设置』页"
                "配置凭据（或调用 POST /api/redmine-agent/config/credentials），"
                "再用 gms-rt-redmine-credentials-status 验证"
            )

        self.store.update_snapshot(snapshot_id, status="fetching")
        errors: list[dict[str, str]] = []
        try:
            raw_bytes, detected_type = await self._fetch_issue_json(
                base_url, issue_id, credentials
            )
        except EvidenceError as exc:
            self._fail(snapshot_id, issue_id, str(exc))
            raise

        snapshot_dir = self.store.snapshot_dir(snapshot)
        issue_rel_path = f"{issue_id}/{snapshot_id}/issue.json"
        try:
            self._atomic_write(self.store.resolve_internal(issue_rel_path), raw_bytes)
        except OSError as exc:
            self._fail(snapshot_id, issue_id, f"写入 issue.json 失败: {exc}")
            raise EvidenceError("保存原始 JSON 失败", status_code=500) from exc

        try:
            document = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._fail(snapshot_id, issue_id, f"响应不是合法 JSON: {exc}")
            raise EvidenceError("Redmine 返回了非 JSON 响应", status_code=502) from exc

        issue = document.get("issue")
        if not isinstance(issue, dict) or int(issue.get("id") or 0) != issue_id:
            self._fail(snapshot_id, issue_id, "响应缺少匹配的 issue.id")
            raise EvidenceError("Redmine 响应缺少匹配的 issue.id", status_code=502)

        manifest = self._build_manifest(issue_id, document, detected_type)
        journals = manifest["journals"]
        attachments = manifest["attachments"]
        if manifest.get("content_type_warning"):
            errors.append({"stage": "issue", "message": manifest["content_type_warning"]})

        self.store.update_snapshot(
            snapshot_id,
            status="downloading",
            source_updated_on=str(issue.get("updated_on") or ""),
            fetched_at=_evidence._now_iso(),
            raw_json_path=issue_rel_path,
            content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            manifest_json=manifest,
            journal_count=len(journals),
            attachment_count=len(attachments),
        )
        for attachment in attachments:
            self.store.create_artifact(
                snapshot_id=snapshot_id,
                attachment_id=str(attachment.get("id") or ""),
                filename=str(attachment.get("filename") or ""),
                original_filename=str(attachment.get("original_filename") or ""),
                content_type=str(attachment.get("content_type") or ""),
                kind=str(attachment.get("kind") or "unknown"),
                declared_size=int(attachment.get("filesize") or 0),
            )

        downloaded = 0
        policy = str(snapshot.get("download_policy") or "none")
        if policy != "none" and attachments:
            if len(attachments) > SNAPSHOT_MAX_ATTACHMENTS:
                errors.append({
                    "stage": "download",
                    "message": f"附件数量 {len(attachments)} 超过上限 {SNAPSHOT_MAX_ATTACHMENTS}，跳过下载",
                })
            else:
                credentials_headers = credentials.headers()
                total_budget = SNAPSHOT_MAX_TOTAL_DOWNLOAD_BYTES
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(
                        total=None,
                        connect=ATTACHMENT_CONNECT_TIMEOUT_SECONDS,
                        sock_read=ATTACHMENT_READ_TIMEOUT_SECONDS,
                    ),
                ) as session:
                    for attachment in attachments:
                        artifact = self._artifact_for(snapshot_id, str(attachment.get("id") or ""))
                        if artifact is None:
                            continue
                        if policy == "analyzable" and attachment.get("kind") not in ANALYZABLE_KINDS:
                            self.store.update_artifact(
                                artifact["artifact_id"], status="rejected",
                                error="download=analyzable 不覆盖该类型",
                            )
                            continue
                        if total_budget <= 0:
                            self.store.update_artifact(
                                artifact["artifact_id"], status="rejected",
                                error="快照总下载量已达上限",
                            )
                            errors.append({
                                "stage": "download",
                                "message": f"attachment {attachment.get('id')} 因总量上限被拒绝",
                            })
                            continue
                        try:
                            spent = await self._download_attachment(
                                session, base_url, attachment,
                                credentials_headers, artifact, snapshot_dir,
                            )
                        except EvidenceError as exc:
                            self.store.update_artifact(
                                artifact["artifact_id"], status="failed", error=str(exc)
                            )
                            errors.append({
                                "stage": "download",
                                "message": f"attachment {attachment.get('id')}: {exc}",
                            })
                            continue
                        except Exception as exc:  # 网络/文件系统异常统一降级
                            self.store.update_artifact(
                                artifact["artifact_id"], status="failed",
                                error=f"下载异常: {type(exc).__name__}",
                            )
                            errors.append({
                                "stage": "download",
                                "message": f"attachment {attachment.get('id')}: {type(exc).__name__}",
                            })
                            continue
                        total_budget -= spent
                        downloaded += 1

        final_status = "ready" if not errors else "partial"
        self.store.update_snapshot(
            snapshot_id,
            status=final_status,
            downloaded_count=downloaded,
            error_json=errors,
        )
        return self.store.get_snapshot(snapshot_id) or {"snapshot_id": snapshot_id}

    # ------------------------------------------------------------ issue fetch

    async def _fetch_issue_json(
        self, base_url: str, issue_id: int, credentials: _OwnerCredentials
    ) -> tuple[bytes, str]:
        url = (
            f"{base_url}/issues/{issue_id}.json"
            "?include=journals,attachments,relations,children"
        )
        headers = credentials.headers()
        headers["Accept"] = "application/json"
        headers.setdefault("User-Agent", "GMS Remote Test/1.0")
        timeout = aiohttp.ClientTimeout(total=SNAPSHOT_ISSUE_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                url, headers=headers, allow_redirects=False
            ) as response:
                return await self._read_issue_response(response)
        except aiohttp.ClientError as exc:
            # 连接失败/超时/断流必须进入 failed 终态（issue 本体
            # 失败必须显式报告），不能让快照永久停在 fetching。
            raise EvidenceError(
                f"Redmine 网络请求失败: {type(exc).__name__}", status_code=502
            ) from exc
        except asyncio.TimeoutError as exc:
            raise EvidenceError(
                f"Redmine 请求超时 ({SNAPSHOT_ISSUE_TIMEOUT_SECONDS}s)", status_code=504
            ) from exc

    async def _read_issue_response(
        self, response: aiohttp.ClientResponse
    ) -> tuple[bytes, str]:
        body = await response.read()
        content_type = str(response.headers.get("Content-Type") or "")
        if response.status in (301, 302, 303, 307, 308):
            raise EvidenceError("Redmine issue 接口发生重定向（登录页？）", status_code=502)
        if response.status in (401,):
            raise EvidenceAuthError("Redmine 拒绝了当前凭据 (401)")
        if response.status == 403:
            raise EvidencePermissionError("当前 Redmine 身份无权读取该 issue (403)")
        if response.status == 404:
            raise EvidenceNotFoundError("Redmine issue 不存在或不可见 (404)")
        if response.status >= 400:
            raise EvidenceError(f"Redmine 返回 HTTP {response.status}", status_code=502)
        if "html" in content_type.lower():
            raise EvidenceAuthError("Redmine 返回了登录 HTML 而不是 JSON")
        if "json" not in content_type.lower() and content_type:
            logger.warning("Redmine issue 响应 Content-Type 异常: %s", content_type)
        return body, content_type

    # --------------------------------------------------------- attachment dl

    async def _download_attachment(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        attachment: dict[str, Any],
        headers: dict[str, str],
        artifact: dict[str, Any],
        snapshot_dir: Path,
    ) -> int:
        artifact_id = str(artifact["artifact_id"])
        evidence_root = self.store.evidence_dir.resolve()
        # snapshot_dir = evidence/<issue>/<snapshot>；转换成 evidence 根相对路径
        snapshot_rel = snapshot_dir.resolve().relative_to(evidence_root).as_posix()
        raw_url = str(attachment.get("content_url") or "")
        if not raw_url:
            raw_url = f"{base_url}/attachments/download/{attachment.get('id')}"
        if raw_url.startswith("/"):
            raw_url = urljoin(base_url + "/", raw_url)
        url = raw_url
        redirects = 0
        while True:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                raise EvidenceError("附件 URL 协议不被允许")
            if not _same_origin(url, base_url):
                raise EvidenceError("附件 URL 跳出了配置的 Redmine origin")
            if not _attachment_path_ok(url):
                raise EvidenceError("附件 URL 路径不是 Redmine attachment/download")
            self.store.update_artifact(artifact_id, status="downloading")
            async with session.get(
                url, headers=headers, allow_redirects=False
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location") or ""
                    if not location:
                        raise EvidenceError("附件重定向缺少 Location")
                    url = urljoin(url, location)
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        raise EvidenceError("附件重定向次数超限")
                    continue
                if response.status in (401,):
                    raise EvidenceAuthError("附件下载被拒 (401)")
                if response.status == 404:
                    raise EvidenceNotFoundError("附件不存在 (404)")
                if response.status >= 400:
                    raise EvidenceError(f"附件下载 HTTP {response.status}")
                declared = int(attachment.get("filesize") or 0)
                if declared and declared > ATTACHMENT_MAX_BYTES:
                    raise EvidenceError(
                        f"声明大小 {declared} 超过单附件上限 {ATTACHMENT_MAX_BYTES}",
                    )
                digest = hashlib.sha256()
                payload = bytearray()
                async for chunk in response.content.iter_chunked(256 * 1024):
                    payload.extend(chunk)
                    digest.update(chunk)
                    if len(payload) > ATTACHMENT_MAX_BYTES:
                        raise EvidenceError("附件超过单附件大小上限")
                data = bytes(payload)
                break

        from .utils import sanitize_attachment_filename

        safe_name = sanitize_attachment_filename(
            str(attachment.get("filename") or f"attachment_{attachment.get('id')}")
        )
        # stored_path 是 evidence 根下的相对路径：issue/<snapshot>/attachments/<artifact>/<name>
        target_rel = f"{snapshot_rel}/attachments/{artifact_id}/{safe_name}"
        absolute = self.store.resolve_internal(target_rel)
        self._atomic_write(absolute, data)

        detected_type = ""
        if data[:3] == b"\xff\xd8\xff":
            detected_type = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            detected_type = "image/png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            detected_type = "image/gif"
        elif data[:2] == b"PK":
            detected_type = "application/zip"
        elif data[:4] == b"%PDF":
            detected_type = "application/pdf"

        derived_rel = ""
        derived_error = ""
        kind = str(artifact.get("kind") or "unknown")
        if kind in {"text", "log"}:
            encoding = _detect_text_encoding(data)
            if encoding and encoding != "utf-8" and len(data) <= _DERIVED_TEXT_MAX_BYTES:
                try:
                    derived = data.decode(encoding).encode("utf-8")
                    derived_rel = f"{snapshot_rel}/derived/{artifact_id}.txt"
                    self._atomic_write(self.store.resolve_internal(derived_rel), derived)
                except (UnicodeDecodeError, OSError) as exc:
                    derived_error = f"派生 UTF-8 文本失败: {type(exc).__name__}"
        elif kind == "archive" and data[:2] == b"PK":
            # 2026-09-11 反馈（反馈 2026-09-11）：zip 内文本成员（logcat /
            # test_result.xml 等）派生成可检索文本，命中可以
            # attachment:<file>.zip!/<member>:L<line> 引用。
            derived_rel, derived_error = self._extract_zip_derived_text(
                artifact_id, snapshot_rel, data
            )

        size_note = ""
        if declared and declared != len(data):
            size_note = f"声明 {declared} 字节，实际 {len(data)} 字节"

        self.store.update_artifact(
            artifact_id,
            status="ready",
            size_bytes=len(data),
            sha256=digest.hexdigest(),
            stored_path=target_rel,
            derived_text_path=derived_rel,
            detected_content_type=detected_type,
            error="; ".join(part for part in (size_note, derived_error) if part),
        )
        if size_note:
            logger.info(
                "Redmine attachment %s 大小不一致: %s", attachment.get("id"), size_note
            )
        return len(data)

    # -------------------------------------------------- zip derived text (2026-09-11 反馈)

    def _extract_zip_derived_text(
        self, artifact_id: str, snapshot_rel: str, data: bytes
    ) -> tuple[str, str]:
        """Extract text members of a zip into one derived text file.

        Returns ``(derived_rel, error_note)``. Only text-suffixed members
        within the size/member budgets are included; the markers let search
        cite ``attachment:<file>.zip!/<member>:L<line>``. Never writes member
        files, so zip-slip and decompression-bomb risk is bounded by the
        per-member and total byte caps checked before reading.
        """

        import io
        import zipfile

        from .evidence import (
            TEXT_KIND_EXTENSIONS,
            ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES,
            ZIP_MEMBER_TEXT_MAX_BYTES,
            ZIP_MEMBER_TEXT_MAX_MEMBERS,
            zip_member_marker,
        )

        chunks: list[str] = []
        total = 0
        included = 0
        skipped_large = 0
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if included >= ZIP_MEMBER_TEXT_MAX_MEMBERS:
                        break
                    suffix = Path(info.filename).suffix.lower()
                    if suffix not in TEXT_KIND_EXTENSIONS:
                        continue
                    if info.file_size > ZIP_MEMBER_TEXT_MAX_BYTES:
                        skipped_large += 1
                        continue
                    if total + info.file_size > ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES:
                        break
                    try:
                        raw = archive.read(info)
                    except (zipfile.BadZipFile, OSError, RuntimeError):
                        continue
                    encoding = _detect_text_encoding(raw) or "utf-8"
                    text = raw.decode(encoding, errors="replace")
                    block = f"{zip_member_marker(info.filename)}\n{text}"
                    chunks.append(block)
                    total += len(block.encode("utf-8"))
                    included += 1
        except (zipfile.BadZipFile, OSError) as exc:
            return "", f"zip 解包失败: {type(exc).__name__}"
        if not included:
            return "", ""
        derived_rel = f"{snapshot_rel}/derived/{artifact_id}.txt"
        payload = ("\n".join(chunks) + "\n").encode("utf-8")
        try:
            self._atomic_write(self.store.resolve_internal(derived_rel), payload)
        except OSError as exc:
            return "", f"派生 zip 文本失败: {type(exc).__name__}"
        note = ""
        if skipped_large:
            note = f"跳过 {skipped_large} 个超阈值 zip 成员"
        return derived_rel, note

    # ------------------------------------------------------------- normalize

    def _build_manifest(
        self, issue_id: int, document: dict[str, Any], content_type: str
    ) -> dict[str, Any]:
        issue = document.get("issue") or {}
        warnings = []
        if "json" not in content_type.lower():
            warnings.append(f"Content-Type 异常: {content_type}")
        journals = []
        for item in issue.get("journals") or []:
            if not isinstance(item, dict):
                continue
            user = item.get("user") or {}
            journals.append({
                "id": item.get("id"),
                "user_id": user.get("id"),
                "user_name": user.get("name") or "",
                "created_on": item.get("created_on") or "",
                "private_notes": bool(item.get("private_notes")),
                "notes": item.get("notes") or "",
                "details": [
                    {
                        "property": detail.get("property") or "",
                        "name": detail.get("name") or "",
                        "old_value": detail.get("old_value"),
                        "new_value": detail.get("new_value"),
                    }
                    for detail in (item.get("details") or [])
                    if isinstance(detail, dict)
                ],
            })
        journals.sort(key=lambda item: int(item.get("id") or 0))

        attachments = []
        for item in issue.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            original_filename = str(item.get("filename") or "")
            from .utils import sanitize_attachment_filename

            attachments.append({
                "id": item.get("id"),
                "filename": sanitize_attachment_filename(
                    original_filename, default=f"attachment_{item.get('id')}"
                ),
                "original_filename": original_filename,
                "filesize": int(item.get("filesize") or 0),
                "content_type": str(item.get("content_type") or ""),
                "content_url": str(item.get("content_url") or ""),
                "created_on": item.get("created_on") or "",
                "author_name": (item.get("author") or {}).get("name") or "",
                "kind": _classify_attachment(original_filename, str(item.get("content_type") or "")),
                "digest": item.get("digest") or "",
            })

        manifest = {
            "issue_id": issue_id,
            "subject": issue.get("subject") or "",
            "description": issue.get("description") or "",
            "status": (issue.get("status") or {}).get("name") if isinstance(issue.get("status"), dict) else "",
            "project": (issue.get("project") or {}).get("name") if isinstance(issue.get("project"), dict) else "",
            "tracker": (issue.get("tracker") or {}).get("name") if isinstance(issue.get("tracker"), dict) else "",
            "priority": (issue.get("priority") or {}).get("name") if isinstance(issue.get("priority"), dict) else "",
            "assigned_to": (issue.get("assigned_to") or {}).get("name") if isinstance(issue.get("assigned_to"), dict) else "",
            "created_on": issue.get("created_on") or "",
            "updated_on": issue.get("updated_on") or "",
            "journals": journals,
            "attachments": attachments,
            "relations": issue.get("relations") or [],
            "children": issue.get("children") or [],
            "fields": {
                key: value for key, value in issue.items()
                if key not in {
                    "journals", "attachments", "relations", "children",
                    "description", "subject",
                }
            },
        }
        if warnings:
            manifest["content_type_warning"] = "; ".join(warnings)
        return manifest

    def _artifact_for(self, snapshot_id: str, attachment_id: str) -> dict[str, Any] | None:
        for artifact in self.store.list_artifacts(snapshot_id):
            if str(artifact.get("attachment_id")) == str(attachment_id):
                return artifact
        return None

    def _fail(self, snapshot_id: str, issue_id: int, message: str) -> None:
        self.store.update_snapshot(
            snapshot_id,
            status="failed",
            error_json=[{"stage": "issue", "message": message}],
        )

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
        temporary.replace(path)
