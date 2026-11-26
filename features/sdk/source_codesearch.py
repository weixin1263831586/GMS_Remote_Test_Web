"""OpenGrok indexed source access; results are not commit-pinned."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any

from foundation.config import ConfigManager

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


logger = logging.getLogger(__name__)


def codesearch_script_location() -> tuple[str, str]:
    """定位 codesearch 脚本与其工作目录（search 子命令的 cwd）。"""
    web_app_dir = Path(__file__).resolve().parents[2]
    codesearch_dir = web_app_dir / 'plugins' / 'codesearch'
    return str(codesearch_dir / 'scripts' / 'codesearch.py'), str(codesearch_dir)


def run_codesearch_process(
    cmd: list[str], cwd: str, *, timeout: float = 30.0,
) -> subprocess.CompletedProcess | None:
    """Run a bounded CodeSearch subprocess without a shell."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=max(0.1, timeout), cwd=cwd,
        )
        return result if result.returncode == 0 else None
    except subprocess.TimeoutExpired:
        logger.warning("代码搜索超时（%.1f秒预算）", timeout)
        return None
    except Exception as e:
        logger.error(f"代码搜索异常: {e}")
        return None


def get_opengrok_project_for_android_version(android_version: str, opengrok_config: dict) -> str:
    if not android_version or not opengrok_config:
        return (opengrok_config or {}).get('default_project', 'Android16')
    match = re.match(r'(\d+)', android_version)
    if match and match.group(1) in opengrok_config.get('project_mapping', {}):
        return opengrok_config['project_mapping'][match.group(1)]
    return opengrok_config.get('default_project', 'Android16')


def _attach_opengrok_url(item: dict[str, Any], base_url: str, project: str) -> None:
    """Build and attach an OpenGrok xref URL to a search result item, then clean temp keys."""
    path = item.get("path", "")
    line = item.get("line")
    if line:
        item["url"] = f"{base_url}/xref/{project}/{path}#{line}"
    else:
        item["url"] = f"{base_url}/xref/{project}/{path}"
    item.pop("_opengrok_base_url", None)
    item.pop("_opengrok_project", None)


def search_indexed_source(
    class_name: str,
    failure_location: dict | None = None,
    max_results: int = 5,
    android_version: str = '',
) -> list[dict[str, str]]:
    """
    Args:
        class_name: 类名 (如 com.android.cts.permission.PermissionTest)
        failure_location: 从堆栈提取的失败位置 {file_name, file_type, line_number}
        max_results: 最大返回结果数
        android_version: 报告的 Android 版本（如 "15"），用于按版本映射选择 OpenGrok 项目

    Returns:
        List[Dict]: 搜索结果列表，每个包含 {project, path, line, type, file_type}
    """
    codesearch_script, codesearch_dir = codesearch_script_location()

    # 按 Android 版本选择索引项目。
    opengrok_config = {}
    with suppress(Exception):
        opengrok_config = ConfigManager().load_config().get('opengrok', {})
    base_url = opengrok_config.get('base_url', '')
    version_project = get_opengrok_project_for_android_version(
        android_version,
        opengrok_config,
    )
    project_args = ['--project', version_project] if version_project else []

    try:
        # 如果有精确失败位置，优先使用
        if failure_location:
            file_name = failure_location.get('file_name', '')
            file_type = failure_location.get('file_type', '')
            line_number = failure_location.get('line_number', '')

            simple_name = file_name.split('
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
)[0]

            result = run_codesearch_process(
                ['python3', codesearch_script, 'search', '--keywords', simple_name, '--search-field', 'path', '--limit', '10', *project_args],
                str(codesearch_dir),
            )
            if result:
                lines = result.stdout.strip().split('\n')
                target_file = f"{simple_name}.{file_type}"

                for i, line_text in enumerate(lines):
                    line_text = line_text.strip()
                    if not line_text:
                        continue
                    if target_file in line_text or (simple_name in line_text and f".{file_type}" in line_text):
                        project = ''
                        for j in range(i + 1, min(len(lines), i + 3)):
                            if lines[j].strip().startswith('project:'):
                                project = lines[j].strip().split(':', 1)[1].strip()
                                break
                        item = {
                            'type': 'definition',
                            'path': line_text.replace('[definition] ', '').strip() if line_text.startswith('[definition]') else line_text,
                            'line': line_number,
                            'file_type': file_type,
                            'project': project,
                            'is_exact_location': True,
                        }
                        # 链接项目优先取报告版本对应项目，避免搜索默认项目覆盖版本语义
                        selected_project = version_project or project
                        if base_url and selected_project:
                            _attach_opengrok_url(item, base_url, selected_project)
                        return [item][:max_results]
                # Fall through to class name search

        # 没有失败位置时，使用类名搜索定义
        simple_class_name = class_name.split('.')[-1]

        result = run_codesearch_process(
            ['python3', codesearch_script, 'search', '--keywords', simple_class_name, '--search-field', 'def', '--limit', str(max_results), *project_args],
            str(codesearch_dir),
        )
        if not result:
            return []

        selected_project = version_project

        search_results = []
        lines = result.stdout.strip().split('\n')

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or not (line.startswith('[') and ']' in line):
                i += 1
                continue

            bracket_end = line.index(']')
            bracket_content = line[1:bracket_end]
            rest_of_line = line[bracket_end + 1:].strip()

            result_item = {
                'type': bracket_content,
                'path': rest_of_line,
                'file_type': 'kt' if rest_of_line.endswith('.kt') else 'java',
            }

            if base_url and selected_project:
                result_item['_opengrok_base_url'] = base_url
                result_item['_opengrok_project'] = selected_project

            for j in range(i + 1, min(len(lines), i + 3)):
                next_line = lines[j].strip()
                if next_line.startswith('project:'):
                    result_item['project'] = next_line.split(':', 1)[1].strip()
                    break
                elif next_line.startswith('['):
                    break

            for j in range(i + 1, min(len(lines), i + 4)):
                next_line = lines[j].strip()
                if (
                    next_line
                    and not next_line.startswith('project:')
                    and not next_line.startswith('[')
                    and ':' in next_line
                ):
                    line_num_part = next_line.split(':')[0].strip()
                    if line_num_part.isdigit():
                        result_item['line'] = line_num_part
                        break

            if '_opengrok_base_url' in result_item:
                _attach_opengrok_url(result_item, base_url, selected_project)

            search_results.append(result_item)
            i += 1

        seen_paths = set()
        unique_results = []
        for item in search_results:
            if item['path'] not in seen_paths:
                seen_paths.add(item['path'])
                unique_results.append(item)

        return unique_results[:max_results]

    except subprocess.TimeoutExpired:
        logger.warning("代码搜索超时")
        return []
    except Exception as e:
        logger.error(f"代码搜索异常：{e}")
        return []


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
