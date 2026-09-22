"""AndroidInternalsProvider：android-internals-wiki 只读背景知识源。

ADR 0014：仅消费管理员配置的本地 git clone（``external_knowledge.providers.
android_internals.repo_root``），客户端不能提供路径；license 表达式来自
上游 policy（CC-BY-NC-SA-4.0 OR LicenseRef-AIW-Commercial），平台侧只保
留引用与 provenance，不做再分发。

实现要点：

- 内容资格由 ``content_policy.AIWContentPolicy`` 决定：跟随上游
  ``knowledge-pack/policy.yaml`` 的正文发布边界（included/excluded/excluded_
  tags），policy 缺失或损坏时 fail-closed 不索引（ADR 0014）；
- frontmatter 用安全 YAML（``yaml.safe_load``）完整解析：tags/sources/
  数组与嵌套结构保留，audit 工作流字段按 policy 白名单投影；sources[]
  投影为 ``SourceAnchor``（Wiki→codesearch 验证闭环）；
- 伴生 FTS5 索引独立于个人知识库（``data_root/knowledge/external/``），
  schema 迁移走 ``user_version`` + ``BEGIN IMMEDIATE``（进程安全、幂等）；
- 增量重建按内容 hash 跳过未变页；重建在单事务内完成，读方在提交前
  继续看到旧索引；
- revision 三态（ADR 0014）：available = clone HEAD（pull 后未 reindex
  时领先），approved = 管理员批准并索引的 revision（``approve_revision``），
  命中的 source_revision 恒等于索引内容实际 revision；
- 检索词元经 ``fts_safe_query`` 清洗，杜绝 FTS 语法注入；命中按 BM25 +
  Android 版本兼容 + confidence + last_verified 新鲜度 + anchor 加权重排
  （``rerank_hits``）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from foundation.config import settings
from foundation.processes import run_local_command

from .base import (
    EXTERNAL_KNOWLEDGE_LICENSE,
    ExternalKnowledgeError,
    KnowledgeHit,
    ProviderStatus,
    fts_safe_query,
)
from .content_policy import (
    POLICY_RELATIVE_PATH,
    AIWContentPolicy,
    load_policy,
    policy_state_summary,
)
from .frontmatter import _scalarize, parse_frontmatter
from .ranking import _bm25_score, extract_source_anchors, rerank_hits


logger = logging.getLogger(__name__)

SOURCE_ID = "android_internals"

DEFAULT_MAX_HITS = 5
MAX_HITS_CAP = 10
GIT_TIMEOUT_SECONDS = 15
REINDEX_BUSY_TIMEOUT_MS = 2_000
_SNIPPET_TOKENS = 24

#: schema 版本；表结构变化时递增并在 _SCHEMA_MIGRATIONS 中登记。
SCHEMA_VERSION = 3
_SCHEMA_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS wiki_pages (
        path TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        chapter TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        frontmatter TEXT NOT NULL DEFAULT '{}',
        content_hash TEXT NOT NULL DEFAULT '',
        mtime REAL NOT NULL DEFAULT 0
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
        path UNINDEXED,
        title,
        content,
        tokenize = 'unicode61'
    );
    CREATE TABLE IF NOT EXISTS wiki_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    );
    """,
    # v2: unicode61 把连续 CJK 串当单个 token，查询侧 2-gram 匹配不上。
    # 新增 cjk 列（索引时预切重叠 2-gram）供中文查询命中；FTS5 虚表
    # 不支持 ADD COLUMN，只能重建。迁移后 fts 行数 < pages 行数，由
    # _ensure_schema 检测到缺口并清空 pages，触发下一轮全量重建。
    2: """
    DROP TABLE IF EXISTS wiki_fts;
    CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
        path UNINDEXED,
        title,
        content,
        cjk,
        tokenize = 'unicode61'
    );
    """,
    # v3: frontmatter 完整投影（sources→anchors、tags 等 JSON 字段）。
    # 老库的 frontmatter 列是残缺标量投影，只能通过清空 pages 触发全量
    # 重建；FTS 列结构未变，虚表无需重建。
    3: """
    DELETE FROM wiki_pages;
    """,
}

def index_db_path() -> Path:
    return Path(settings.data_root) / "knowledge/external/android_internals.sqlite3"


_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def _cjk_bigramize(text: str) -> str:
    """索引侧 CJK 预处理：unicode61 把连续 CJK 串当单个 token，查询侧的
    2-gram 词元会匹配不上（golden query 实测金标漏召回）。把每个 CJK 串
    预切为重叠 2-gram 写入独立 cjk 列，snippet 仍取原始 content 列。"""
    def _bigrams(match: re.Match[str]) -> str:
        run = match.group(0)
        return " ".join(run[i : i + 2] for i in range(len(run) - 1))

    return _CJK_RUN_RE.sub(_bigrams, text)



class AndroidInternalsProvider:
    """android-internals-wiki 只读 provider（status + search + 显式 reindex）。"""

    source_id = SOURCE_ID

    def __init__(self, repo_root: str, *, max_hits: int = DEFAULT_MAX_HITS) -> None:
        self.repo_root = Path(os.path.expanduser(str(repo_root or ""))).resolve()
        self.max_hits = max(1, min(int(max_hits or DEFAULT_MAX_HITS), MAX_HITS_CAP))
        self._reindex_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 配置装载（fail-closed：未配置/无效 → disabled，绝不抛出到调用方）
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> AndroidInternalsProvider | None:
        section = ((config or {}).get("external_knowledge") or {}).get("providers") or {}
        item = section.get(SOURCE_ID) or {}
        if not isinstance(item, dict) or not item.get("enabled"):
            return None
        repo_root = str(item.get("repo_root") or "").strip()
        if not repo_root:
            return None
        try:
            max_hits = int(item.get("max_hits") or DEFAULT_MAX_HITS)
        except (TypeError, ValueError):
            max_hits = DEFAULT_MAX_HITS
        return cls(repo_root, max_hits=max_hits)

    def validate(self) -> str:
        """返回空串表示 clone 可用；否则返回 disabled 原因。"""
        if not self.repo_root.is_dir():
            return f"repo_root 不存在: {self.repo_root}"
        if not (self.repo_root / ".git").exists():
            return f"repo_root 不是 git clone: {self.repo_root}"
        if not (self.repo_root / "src").is_dir():
            return f"repo_root 缺少 src/ 目录: {self.repo_root}"
        return ""

    def load_policy(self) -> AIWContentPolicy:
        """读取上游发布策略（每次调用重新读取，管理员改 policy 即生效）。"""
        return load_policy(self.repo_root)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def status(self) -> ProviderStatus:
        reason = self.validate()
        if reason:
            return ProviderStatus(source=SOURCE_ID, enabled=True, status="disabled", detail=reason)
        doc_count = 0
        last_sync_at = ""
        revision = ""
        approved = ""
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()
                doc_count = int(row[0] or 0)
                meta = conn.execute(
                    "SELECT key, value FROM wiki_meta WHERE key IN "
                    "('last_sync_at', 'source_revision', 'approved_revision')"
                ).fetchall()
                meta_map = {str(r["key"]): str(r["value"] or "") for r in meta}
                last_sync_at = meta_map.get("last_sync_at", "")
                revision = meta_map.get("source_revision", "")
                approved = meta_map.get("approved_revision", "")
        except (sqlite3.Error, OSError) as exc:
            return ProviderStatus(
                source=SOURCE_ID, enabled=True, status="error", detail=f"索引不可用: {exc}"
            )
        # revision 三态：available = clone HEAD（pull 后未 reindex 时领先于
        # 索引）；approved = 管理员批准的 revision；source = 已索引 revision。
        available = self._head_revision()
        if not approved:
            approved = revision
        detail = ""
        if not doc_count:
            detail = "索引为空，请管理员执行 reindex"
        elif approved and revision != approved:
            detail = (
                f"revision {approved[:12]} 已批准，等待 reindex"
                f"（indexed {revision[:12] or 'none'}）"
            )
            if available and available != approved:
                detail += f"；clone HEAD 已变更为 {available[:12]}，需重新批准"
        elif available and approved and available != approved:
            detail = (
                f"上游有新 revision（available {available[:12]} ≠ approved "
                f"{approved[:12]}），请管理员确认后 reindex"
            )
        return ProviderStatus(
            source=SOURCE_ID,
            enabled=True,
            status="ready" if doc_count else "empty",
            detail=detail,
            source_revision=revision,
            doc_count=doc_count,
            last_sync_at=last_sync_at,
            available_revision=available,
            approved_revision=approved,
        )

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_MAX_HITS,
        android_api_level: int | None = None,
    ) -> list[KnowledgeHit]:
        if self.validate():
            return []
        match_query = fts_safe_query(query)
        if not match_query:
            return []
        limit = max(1, min(int(limit or self.max_hits), MAX_HITS_CAP))
        # BM25 ORDER BY 取前 3x 候选，加权重排后再截到 limit：避免加权
        # 信号（版本/新鲜度）作用在已被 BM25 截断的窗口外。
        candidate_cap = min(MAX_HITS_CAP, max(limit * 3, limit + 5))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT p.path, p.title, p.chapter, p.status, p.frontmatter,
                           snippet(wiki_fts, -1, '[', ']', '…', :snip_tokens) AS snip,
                           bm25(wiki_fts) AS rank
                    FROM wiki_fts
                    JOIN wiki_pages p ON p.path = wiki_fts.path
                    WHERE wiki_fts MATCH :match
                    ORDER BY rank
                    LIMIT :limit
                    """,
                    {"match": match_query, "snip_tokens": _SNIPPET_TOKENS, "limit": candidate_cap},
                ).fetchall()
                # revision 读索引内置值（reindex 时写入）：与索引内容严格对应，
                # 且检索热路径不起 git 子进程。
                revision = self._stored_revision(conn)
        except sqlite3.Error as exc:
            logger.warning("android_internals search failed: %s", exc)
            return []
        if not rows:
            return []
        best = min((float(row["rank"]) for row in rows), default=0.0)
        hits: list[KnowledgeHit] = []
        for row in rows:
            frontmatter = self._load_frontmatter(row["frontmatter"])
            rank = float(row["rank"] or 0.0)
            score = _bm25_score(rank, best)
            anchors = extract_source_anchors(frontmatter.get("sources"), revision)
            tags = [str(tag) for tag in (frontmatter.get("tags") or []) if str(tag).strip()]
            hits.append(
                KnowledgeHit(
                    source=SOURCE_ID,
                    title=str(frontmatter.get("title") or row["title"] or Path(str(row["path"])).stem),
                    snippet=str(row["snip"] or "").strip(),
                    source_path=str(row["path"]),
                    chapter=str(frontmatter.get("chapter") or row["chapter"] or ""),
                    source_revision=revision,
                    applicable_versions=_scalarize(frontmatter.get("applicable_versions")),
                    confidence=_scalarize(frontmatter.get("confidence")),
                    last_verified=_scalarize(frontmatter.get("last_verified")),
                    last_verified_against=_scalarize(frontmatter.get("last_verified_against")),
                    license=self._license_hint(),
                    score=score,
                    source_anchors=anchors,
                    extra={
                        "status": str(frontmatter.get("status") or row["status"] or ""),
                        "tags": tags,
                    },
                )
            )
        return rerank_hits(hits, query, android_api_level=android_api_level)[:limit]

    @staticmethod
    def _load_frontmatter(raw: Any) -> dict[str, Any]:
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _license_hint(self) -> str:
        """license 展示值：policy.expression 优先，缺失时回退 ADR 0014 常量。"""
        try:
            expression = self.load_policy().license_expression
        except Exception:  # 政策读取失败不阻塞检索
            expression = ""
        return expression or EXTERNAL_KNOWLEDGE_LICENSE

    # ------------------------------------------------------------------
    # 索引重建
    # ------------------------------------------------------------------

    def reindex(self) -> dict[str, Any]:
        """全量增量重建；并发触发时后者以 409 语义失败（ExternalKnowledgeError）。"""
        reason = self.validate()
        if reason:
            raise ExternalKnowledgeError(reason)
        with self._reindex_lock:
            return self._reindex_locked()

    def _reindex_locked(self) -> dict[str, Any]:
        policy = self.load_policy()
        if policy.degraded:
            # fail-closed：发布策略缺失/损坏时不重建，显式暴露而不是放宽
            # 成"src 下全部都是知识"（ADR 0014）。
            raise ExternalKnowledgeError(
                f"上游发布策略不可用（{POLICY_RELATIVE_PATH}: {policy.parse_warning}）；"
                "已拒绝重建索引，请检查 tools/android-internals-wiki 的 knowledge-pack/policy.yaml"
            )
        pages = self._collect_pages(policy)
        revision = self._head_revision()
        db_path = index_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        try:
            conn = sqlite3.connect(db_path, timeout=REINDEX_BUSY_TIMEOUT_MS / 1000)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(f"PRAGMA busy_timeout = {REINDEX_BUSY_TIMEOUT_MS}")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("BEGIN IMMEDIATE")
                self._ensure_schema(conn)
                stored = conn.execute(
                    "SELECT key, value FROM wiki_meta WHERE key IN "
                    "('source_revision', 'approved_revision')"
                ).fetchall()
                stored_meta = {str(row["key"]): str(row["value"] or "") for row in stored}
                indexed_revision = stored_meta.get("source_revision", "")
                approved_revision = (
                    stored_meta.get("approved_revision", "") or indexed_revision
                )
                if indexed_revision and revision != approved_revision:
                    raise ExternalKnowledgeError(
                        f"revision {revision[:12]} 尚未批准；当前批准版本为 "
                        f"{approved_revision[:12] or 'none'}，请先执行 approve-revision"
                    )
                if not approved_revision:
                    # 首次建库没有旧索引可保护，由本次管理员 reindex 完成初始批准。
                    approved_revision = revision
                updated, removed = self._apply_pages(conn, pages)
                meta: dict[str, str] = {
                    "last_sync_at": datetime.now().isoformat(timespec="seconds"),
                    "source_revision": revision,
                    "approved_revision": approved_revision,
                    "policy_state": json.dumps(
                        policy_state_summary(policy), ensure_ascii=False, sort_keys=True
                    ),
                }
                for key, value in meta.items():
                    conn.execute(
                        "INSERT INTO wiki_meta(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value),
                    )
                conn.commit()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise ExternalKnowledgeError("已有索引重建进行中") from exc
                raise ExternalKnowledgeError(f"索引重建失败: {exc}") from exc
            except sqlite3.Error as exc:
                conn.rollback()
                raise ExternalKnowledgeError(f"索引重建失败: {exc}") from exc
            finally:
                conn.close()
        except OSError as exc:
            raise ExternalKnowledgeError(f"索引文件不可写: {exc}") from exc
        logger.info(
            "android_internals reindex done: %s pages (+%s/-%s) in %.1fs",
            len(pages), updated, removed, time.time() - started,
        )
        result: dict[str, Any] = {
            "source": SOURCE_ID,
            "doc_count": len(pages),
            "updated": updated,
            "removed": removed,
            "source_revision": revision,
            "approved_revision": approved_revision,
            "elapsed_seconds": round(time.time() - started, 2),
        }
        result.update(policy_state_summary(policy))
        return result

    def approve_revision(self) -> dict[str, Any]:
        """管理员批准当前 clone HEAD 为索引目标（三态收口动作）。

        实际索引内容仍由 reindex 决定；这里只把 approved_revision 指向
        available HEAD 并落审计痕迹，供 status/UI 显示"已批准待索引"。
        """
        reason = self.validate()
        if reason:
            raise ExternalKnowledgeError(reason)
        revision = self._head_revision()
        if not revision:
            raise ExternalKnowledgeError("无法读取 clone HEAD revision")
        db_path = index_db_path()
        if not db_path.exists():
            raise ExternalKnowledgeError("索引不存在，请先执行一次 reindex")
        try:
            conn = sqlite3.connect(db_path, timeout=REINDEX_BUSY_TIMEOUT_MS / 1000)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(f"PRAGMA busy_timeout = {REINDEX_BUSY_TIMEOUT_MS}")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO wiki_meta(key, value) VALUES('approved_revision', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (revision,),
                )
                conn.execute(
                    "INSERT INTO wiki_meta(key, value) VALUES('approved_at', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (datetime.now().isoformat(timespec="seconds"),),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise ExternalKnowledgeError(f"批准 revision 失败: {exc}") from exc
            finally:
                conn.close()
        except OSError as exc:
            raise ExternalKnowledgeError(f"索引文件不可写: {exc}") from exc
        logger.info("android_internals revision approved: %s", revision)
        return {"source": SOURCE_ID, "approved_revision": revision}

    def _collect_pages(self, policy: AIWContentPolicy | None = None) -> dict[str, dict[str, Any]]:
        active_policy = policy or self.load_policy()
        src_root = self.repo_root / "src"
        pages: dict[str, dict[str, Any]] = {}
        skipped = 0
        for md_path in sorted(src_root.rglob("*.md")):
            rel_path = md_path.relative_to(self.repo_root).as_posix()
            try:
                stat = md_path.stat()
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("android_internals: skip unreadable %s: %s", md_path, exc)
                continue
            if not text.strip():
                continue
            frontmatter, body = parse_frontmatter(text)
            tags = [str(tag) for tag in (frontmatter.get("tags") or []) if str(tag).strip()]
            # 内容资格判定走 policy（路径 + excluded_tags 双重边界）。
            if not active_policy.is_eligible(rel_path, tags):
                skipped += 1
                continue
            projected = active_policy.project_frontmatter(frontmatter)
            pages[rel_path] = {
                "path": rel_path,
                "title": _scalarize(projected.get("title")) or md_path.stem,
                "chapter": _scalarize(projected.get("chapter") or projected.get("section") or ""),
                "status": _scalarize(projected.get("status") or frontmatter.get("status") or ""),
                "frontmatter": projected,
                "content": body,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "mtime": stat.st_mtime,
            }
        if skipped:
            logger.info(
                "android_internals: policy excluded %s non-canonical paths", skipped
            )
        return pages

    def _apply_pages(self, conn: sqlite3.Connection, pages: dict[str, dict[str, Any]]) -> tuple[int, int]:
        existing = {
            str(row["path"]): str(row["content_hash"])
            for row in conn.execute("SELECT path, content_hash FROM wiki_pages")
        }
        updated = 0
        for page in pages.values():
            if existing.get(page["path"]) == page["content_hash"]:
                continue
            conn.execute(
                """
                INSERT INTO wiki_pages(path, title, chapter, status, content, frontmatter, content_hash, mtime)
                VALUES(:path, :title, :chapter, :status, :content, :frontmatter, :content_hash, :mtime)
                ON CONFLICT(path) DO UPDATE SET
                    title = excluded.title, chapter = excluded.chapter, status = excluded.status,
                    content = excluded.content, frontmatter = excluded.frontmatter,
                    content_hash = excluded.content_hash, mtime = excluded.mtime
                """,
                {
                    **{key: page[key] for key in ("path", "title", "chapter", "status", "content", "content_hash", "mtime")},
                    "frontmatter": json.dumps(page["frontmatter"], ensure_ascii=False),
                },
            )
            conn.execute("DELETE FROM wiki_fts WHERE path = ?", (page["path"],))
            conn.execute(
                "INSERT INTO wiki_fts(path, title, content, cjk) VALUES(?, ?, ?, ?)",
                (
                    page["path"],
                    page["title"],
                    page["content"],
                    _cjk_bigramize(f"{page['title']}\n{page['content']}"),
                ),
            )
            updated += 1
        removed = 0
        for stale_path in set(existing) - set(pages):
            conn.execute("DELETE FROM wiki_pages WHERE path = ?", (stale_path,))
            conn.execute("DELETE FROM wiki_fts WHERE path = ?", (stale_path,))
            removed += 1
        return updated, removed

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            # 逐条 execute：executescript 会隐式 COMMIT 挂起事务，破坏
            # BEGIN IMMEDIATE 的迁移+重建原子性。迁移脚本均为无内嵌
            # 分号的 DDL，按 ";" 拆分是安全的。
            for statement in filter(str.strip, _SCHEMA_MIGRATIONS[version].split(";")):
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # FTS 重建型迁移（v2 清空 wiki_fts）后行数缺口：清空 pages 让
        # 本轮 reindex 走全量重建，增量 hash 逻辑不会误跳过已存在页。
        pages = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        fts_rows = conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0]
        if pages != fts_rows:
            conn.execute("DELETE FROM wiki_pages")

    # ------------------------------------------------------------------
    # git / sqlite helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stored_revision(conn: sqlite3.Connection) -> str:
        """索引内置的 source_revision（reindex 时写入）；缺省返回空串。"""
        row = conn.execute(
            "SELECT value FROM wiki_meta WHERE key = 'source_revision'"
        ).fetchone()
        return str(row[0]) if row else ""

    def _head_revision(self) -> str:
        result = run_local_command(
            ["git", "-C", str(self.repo_root), "rev-parse", "HEAD"],
            timeout=GIT_TIMEOUT_SECONDS,
        )
        head = (result.stdout or "").strip()
        return head if result.ok and re.fullmatch(r"[0-9a-f]{40}", head) else ""

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """只读连接（mode=ro）；关闭由本上下文负责，避免长期连接泄漏。"""
        db_path = index_db_path()
        if not db_path.exists():
            raise sqlite3.OperationalError("index database does not exist yet")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
