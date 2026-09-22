"""OpenGrok indexed source access; results are not commit-pinned."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .source_provider_contract import (
    CODESEARCH_MAX_HITS_PER_FILE,
    CODESEARCH_TIMEOUT_SECONDS,
    MAX_BLOB_BYTES,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    ProviderConfig,
    SourceProviderError,
    _make_signed_id,
    _opengrok_line_number,
    _safe_repo_path,
    _strip_html_tags,
    _strip_opengrok_path,
    decode_signed_id,
)


class CodesearchProvider:
    """OpenGrok /api/v1 REST provider（与 codesearch 插件同一服务契约）。

    与 local_git 的语义差异：

    - 索引是动态的：``revision`` 仅作为请求标签记录在 result_id 里，
      ``commit`` 恒为空串；不保证跨索引更新的引用可复现性；
    - 所有返回显式携带 ``reproducible: false`` 与
      ``requested_revision``——这类证据不允许单独把 root_cause 推到
      confirmed（由 daily brief prompt/evidence 消费方执行）；
    - result_id 载荷为 ``source_id/revision/path``（无 commit）；
    - ``search`` 走 ``GET /api/v1/search``（full + projects [+ path]），
      ``read`` 走 ``GET /api/v1/file/content?path=/<project>/<path>``。
    """

    kind = "codesearch"
    # 动态索引：证据不可按 commit 复现（local_git provider 为 True）。
    reproducible = False

    def __init__(self, config: ProviderConfig, secret: bytes):
        self.config = config
        self._secret = secret

    # -------------------------------------------------------------- helpers

    def _request(self, path: str, params: dict[str, Any]) -> bytes:
        url = f"{self.config.base_url.rstrip('/')}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"Bearer {self.config.token}")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=CODESEARCH_TIMEOUT_SECONDS
            ) as response:
                raw = response.read(MAX_BLOB_BYTES + 1)
                if len(raw) > MAX_BLOB_BYTES:
                    raise SourceProviderError("OpenGrok 响应超过读取上限", status_code=502)
                return raw
        except urllib.error.HTTPError as exc:
            # 错误模型：上游 5xx/凭据失效 = 依赖故障 → 502（凭据问题
            # 管理员可修，不能让 agent 误判为“文件不存在”而放弃）；
            # 400/422 = 查询非法 → 422；404 保留 404。
            if exc.code in (400, 422):
                status = 422
            elif exc.code == 404:
                status = 404
            else:
                status = 502
            raise SourceProviderError(
                f"OpenGrok HTTP {exc.code}: {path}", status_code=status
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # 超时按错误模型映射 504（agents 靠状态码决定 retry vs fix）。
            timed_out = isinstance(exc, TimeoutError) or (
                isinstance(exc, urllib.error.URLError)
                and isinstance(getattr(exc, "reason", None), TimeoutError)
            )
            raise SourceProviderError(
                f"OpenGrok 请求失败: {type(exc).__name__}",
                status_code=504 if timed_out else 502,
            ) from exc

    def _request_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            data = json.loads(
                self._request(path, params).decode("utf-8", errors="replace")
            )
            if not isinstance(data, dict):
                raise SourceProviderError("OpenGrok 返回的不是 JSON 对象", status_code=502)
            return data
        except json.JSONDecodeError as exc:
            raise SourceProviderError(
                "OpenGrok 返回的不是合法 JSON", status_code=502
            ) from exc

    def _make_result_id(self, path: str, revision: str) -> str:
        return _make_signed_id(
            {
                "source_id": self.config.source_id,
                "revision": revision,
                "path": path,
            },
            self._secret,
        )

    def _decode_own_id(self, result_id: str) -> dict[str, str]:
        payload = decode_signed_id(result_id, self._secret)
        if str(payload.get("source_id") or "") != self.config.source_id:
            raise SourceProviderError(
                f"result_id 不属于 source '{self.config.source_id}'", status_code=422
            )
        return {
            "revision": str(payload.get("revision") or ""),
            "path": str(payload.get("path") or ""),
        }

    # ----------------------------------------------------------- revision

    def resolve_revision(self, revision: str) -> str:
        resolved = str(revision or "").strip() or self.config.default_revision
        if not resolved:
            raise SourceProviderError(
                "revision 不能为空", status_code=422
            )
        return resolved

    def revision_metadata(self, revision: str = "") -> dict[str, Any]:
        return {
            "source_id": self.config.source_id,
            "provider": self.kind,
            "requested_revision": self.resolve_revision(revision),
            "revision": self.resolve_revision(revision),
            "commit": "",
            "commit_subject": "OpenGrok 动态索引（无固定 commit）",
            "commit_date": "",
            "reproducible": False,
        }

    # -------------------------------------------------------------- search

    def search(
        self,
        revision: str,
        query: str,
        *,
        path_filter: str = "",
        limit: int = SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise SourceProviderError("query 不能为空", status_code=422)
        limit = max(1, min(int(limit or SEARCH_DEFAULT_LIMIT), SEARCH_MAX_LIMIT))
        resolved = self.resolve_revision(revision)
        params: dict[str, Any] = {
            "full": query,
            "projects": self.config.project,
            "maxresults": min(limit * 2, 500),
            "maxHitsPerFile": CODESEARCH_MAX_HITS_PER_FILE,
        }
        filter_value = str(path_filter or "").strip()
        if filter_value:
            params["path"] = filter_value
        data = self._request_json("/api/v1/search", params)
        results = data.get("results")
        matches: list[dict[str, Any]] = []
        # 达到 limit 截断时必须如实上报（与 local_git provider 契约一致）。
        limited = False
        if isinstance(results, dict):
            for raw_path, raw_hits in results.items():
                repo_path = _strip_opengrok_path(str(raw_path), self.config.project)
                # 畸形上游（代理错误页/索引损坏）可能返回非 list 的
                # hits 值；按依赖数据损坏映射 502，而不是 TypeError→500。
                if not isinstance(raw_hits, list):
                    raise SourceProviderError(
                        "OpenGrok 返回了无法解析的 results 结构（hits 非"
                        " 列表）；请检查上游索引/代理",
                        status_code=502,
                    )
                for hit in raw_hits[:CODESEARCH_MAX_HITS_PER_FILE]:
                    if not isinstance(hit, dict):
                        continue
                    snippet = _strip_html_tags(str(hit.get("line") or ""))[:400]
                    matches.append({
                        "source_id": self.config.source_id,
                        "revision": resolved,
                        "commit": "",
                        "reproducible": False,
                        "path": repo_path,
                        "line": _opengrok_line_number(hit.get("lineNumber")),
                        "snippet": snippet,
                        "result_id": self._make_result_id(repo_path, resolved),
                    })
                    if len(matches) >= limit:
                        limited = True
                        break
                if len(matches) >= limit:
                    limited = True
                    break
        return {
            "source_id": self.config.source_id,
            "revision": resolved,
            "commit": "",
            "reproducible": False,
            "total": _opengrok_line_number(data.get("resultCount")) or len(matches),
            "scanned_files": len(results) if isinstance(results, dict) else 0,
            "limited": limited,
            "matches": matches,
        }

    # ---------------------------------------------------------------- read

    def read_signed(
        self, result_id: str, *, offset: int = 0, limit: int = 400
    ) -> dict[str, Any]:
        """解码自包含 result_id 后按索引文件内容分段读取（唯一 read 入口）。"""
        payload = self._decode_own_id(result_id)
        safe_path = _safe_repo_path(payload["path"])
        raw = self._request(
            "/api/v1/file/content",
            {"path": f"/{self.config.project}/{safe_path}"},
        )
        text = raw[:MAX_BLOB_BYTES].decode("utf-8", errors="replace")
        lines = text.splitlines()
        window = lines[offset:offset + limit]
        return {
            "source_id": self.config.source_id,
            "revision": payload["revision"],
            "commit": "",
            "reproducible": False,
            "path": safe_path,
            "blob_sha256": hashlib.sha256(raw).hexdigest(),
            "total_lines": len(lines),
            "offset": offset,
            "returned_lines": len(window),
            "next_offset": offset + limit if offset + limit < len(lines) else None,
            "truncated": offset + limit < len(lines),
            "lines": [
                {"line": offset + index + 1, "text": line}
                for index, line in enumerate(window)
            ],
        }
