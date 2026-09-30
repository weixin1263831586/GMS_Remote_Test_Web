"""AIWContentPolicy：android-internals-wiki 内容资格与发布策略（ADR 0014）。

ADR 0014 要求 ingestion 不再由 Provider 自行决定 "src/**/*.md 都是知识"，
而是跟随上游 ``knowledge-pack/policy.yaml`` 的正文发布边界：

- ``included_paths``：可消费正文范围（part1~part5 章；fnmatch 语义）；
- ``excluded_paths``：生成物/非正文（如 ``src/graphify-out/**``）；
- ``excluded_tags``：frontmatter 打了排除标签的页；
- ``exported_metadata``：允许进入索引的 frontmatter 字段白名单；
- ``license.expression``：运行期 license 展示值（不再硬编码简化串）。

上游 policy 未覆盖的路径一律排除（fail-closed），保证 GMS 索引与上游
知识发布规则不漂移；policy 文件缺失/损坏时回退为"无正文"而不是全量
索引，由 status/reindex 显式暴露该异常状态。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

POLICY_RELATIVE_PATH = "knowledge-pack/policy.yaml"

#: 上游 policy 尚未声明任何 distribution 时使用的保守回退：仅索引
#: src/partN-*/ch* 章节正文（与上游 policy 的 included_paths 语义一致）。
_FALLBACK_INCLUDED = ["src/part*-*/ch*/**"]

#: GMS 消费的 distribution 名称候选（显式有序）。上游未来新增其他消费方
#: （如 another_consumer）时，"字典里的第一个"不得成为内容信任边界：
#: 只有候选列表内的 distribution 才允许决定 included/excluded 投影。
_DISTRIBUTION_CANDIDATES = ("android_internals", "smartperfetto")

_ALWAYS_EXCLUDED = [
    "src/graphify-out/**",
]

#: 永不进入索引/结果集的工作流字段（policy.audit_metadata.workflow_fields）
_AUDIT_FIELD_PREFIXES = ("task6_", "task9_", "pipeline_stage")


@dataclass(frozen=True)
class AIWContentPolicy:
    """已解析的内容发布策略（不可变；reindex 时重建）。"""

    included_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    excluded_tags: frozenset[str] = frozenset()
    exported_metadata: tuple[str, ...] = ()
    license_expression: str = ""
    schema_version: int = 0
    #: 实际采用的 distribution 名称（"" = 上游未声明任何 distribution，
    #: 使用保守回退）；status 里必须可见，防止投影来源静默漂移。
    policy_distribution: str = ""
    #: 所选 distribution 的 projection_revision（上游内容投影版本）。
    policy_projection_revision: int = 0
    source_revision: str = ""
    content_hash: str = ""
    parse_warning: str = ""
    #: policy 文件缺失或不可解析时为 True：此时不索引任何正文（fail-closed）。
    degraded: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    # -- 资格判定 -------------------------------------------------------

    def is_eligible(self, rel_path: str, tags: list[str] | None = None) -> bool:
        """路径 + 标签双重资格判定；任何一步不满足即排除。"""
        if self.degraded:
            return False
        normalized = rel_path.replace("\\", "/")
        if _matches_any(normalized, _ALWAYS_EXCLUDED):
            return False
        if self.excluded_paths and _matches_any(normalized, self.excluded_paths):
            return False
        if self.excluded_tags and tags:
            lowered = {str(tag).strip().lower() for tag in tags}
            if lowered & set(self.excluded_tags):
                return False
        return _matches_any(normalized, self.included_paths)

    def project_frontmatter(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """导出白名单投影：audit 工作流字段永不导出。"""
        if self.degraded:
            return {}
        allowed = set(self.exported_metadata) or {
            "title", "chapter", "applicable_versions", "last_verified",
            "last_verified_against", "confidence", "tags", "sources",
        }
        out: dict[str, Any] = {}
        for key, value in metadata.items():
            lowered = str(key).lower()
            if lowered.startswith(_AUDIT_FIELD_PREFIXES):
                continue
            if lowered in allowed:
                out[str(key)] = value
        return out


def _matches_any(rel_path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, str(pattern)) for pattern in patterns)


def load_policy(repo_root: Path) -> AIWContentPolicy:
    """读取并解析上游 knowledge-pack/policy.yaml（不可信外部数据）。

    任何解析失败都降级为 ``degraded=True``（不索引正文），绝不抛出到
    reindex 流程——policy 是资格边界，坏了必须显式暴露而不是放宽。
    """
    policy_path = repo_root / POLICY_RELATIVE_PATH
    raw_text = ""
    try:
        raw_text = policy_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("android_internals policy unreadable: %s", exc)
        return AIWContentPolicy(
            degraded=True,
            parse_warning=f"policy 不可读: {exc}",
            source_revision=_head_revision(repo_root),
        )
    raw = _parse_yaml_mapping(raw_text)
    if raw is None:
        return AIWContentPolicy(
            degraded=True,
            parse_warning="policy 不是合法的 YAML mapping",
            source_revision=_head_revision(repo_root),
        )
    schema_version = _as_int(raw.get("schema_version"), 0)
    distributions = raw.get("distribution")
    pack: dict[str, Any] = {}
    policy_distribution = ""
    if isinstance(distributions, dict) and distributions:
        # 显式按候选顺序选择，不用"字典第一个"：上游新增其他消费方的
        # distribution 时投影边界不得静默漂移（ADR 0014）。
        for name in _DISTRIBUTION_CANDIDATES:
            candidate = distributions.get(name)
            if isinstance(candidate, dict):
                pack = candidate
                policy_distribution = name
                break
        else:
            # 上游只声明了未知消费方的 distribution：其投影边界对 GMS
            # 不适用，fail-closed（不索引正文）并由 status 显式暴露。
            return AIWContentPolicy(
                degraded=True,
                parse_warning=(
                    "policy distribution 不含已知消费方 "
                    f"{list(_DISTRIBUTION_CANDIDATES)}: {sorted(distributions)}"
                ),
                schema_version=schema_version,
                source_revision=_head_revision(repo_root),
                content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                raw=raw,
            )
    projection_revision = _as_int(pack.get("projection_revision"), 0)
    included = _as_str_tuple(pack.get("included_paths")) or _FALLBACK_INCLUDED
    excluded = _as_str_tuple(pack.get("excluded_paths"))
    excluded_tags = frozenset(
        str(tag).strip().lower() for tag in _as_str_tuple(pack.get("excluded_tags"))
    )
    exported = _as_str_tuple(raw.get("exported_metadata"))
    license_block = raw.get("license")
    license_expression = ""
    if isinstance(license_block, dict):
        license_expression = str(license_block.get("expression") or "").strip()
    elif isinstance(license_block, str):
        license_expression = license_block.strip()
    return AIWContentPolicy(
        included_paths=included,
        excluded_paths=excluded,
        excluded_tags=excluded_tags,
        exported_metadata=exported,
        license_expression=license_expression,
        schema_version=schema_version,
        policy_distribution=policy_distribution,
        policy_projection_revision=projection_revision,
        source_revision=_head_revision(repo_root),
        content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        raw=raw if isinstance(raw, dict) else {},
    )


def _parse_yaml_mapping(text: str) -> dict[str, Any] | None:
    """使用安全 YAML 解析；依赖缺失或输入损坏时返回 None 以 fail-closed。"""
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML is unavailable; refusing to interpret content policy")
        return None
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _head_revision(repo_root: Path) -> str:
    try:
        git_dir = repo_root / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            return (git_dir / ref).read_text(encoding="utf-8").strip()
        return head
    except OSError:
        return ""


def policy_state_summary(policy: AIWContentPolicy) -> dict[str, Any]:
    """status/reindex 结果里的 policy 概览（不携带原始 policy 全文）。"""
    return {
        "schema_version": policy.schema_version,
        "policy_distribution": policy.policy_distribution,
        "policy_projection_revision": policy.policy_projection_revision,
        "content_hash": policy.content_hash,
        "included_paths": list(policy.included_paths),
        "excluded_paths": list(policy.excluded_paths),
        "excluded_tags": sorted(policy.excluded_tags),
        "exported_metadata": list(policy.exported_metadata),
        "license_expression": policy.license_expression,
        "degraded": policy.degraded,
        "parse_warning": policy.parse_warning,
        "policy_revision": policy.source_revision,
    }


def dumps_policy(policy: AIWContentPolicy) -> str:
    return json.dumps(policy_state_summary(policy), ensure_ascii=False, sort_keys=True)
