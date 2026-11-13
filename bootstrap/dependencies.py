from __future__ import annotations

from dataclasses import dataclass

from features.automation import register_worker_status_port
from features.cluster import register_cluster_port
from features.devices.host_inventory import register_devices_port
from features.devices.locks import devices_display_name_resolver
from features.email import (
    configure_attachment_resolver,
    configure_manager_provider,
)
from features.kkagent import UniversalAIAnalyzer
from features.redmine import (
    configure_agent_factories,
    get_redmine_config_for_request,
)
from features.redmine.agent import RedmineAgent
from features.redmine.repository import RedmineAgentDB
from features.redmine.service import RedmineService
from features.reports import ReportAnalyzer
from foundation.config import ConfigManager, RuntimeSettings, settings
from foundation.device_locks import configure_display_name_resolver


def _universal_analyzer_factory(config=None):
    """统一网关分析器工厂：注入 reports 源码检索 provider（避免依赖环）。"""
    if config is None:
        from foundation.config import config_manager

        config = config_manager.get_ai_config()

    def _codesearch_source_search(class_name: str) -> list[dict]:
        try:
            return ReportAnalyzer().rk_codesearch(class_name, max_results=3) or []
        except Exception:
            return []

    return UniversalAIAnalyzer(config, source_search=_codesearch_source_search)


def _wire_sdk_source_rank_hints() -> None:
    """把 android-internals wiki 命中转成 SDK 子仓排序提示（ADR 0014 闭环）。

    wiki 的 ``source_anchors``（repo/path）与机制章节名是强相关子仓的
    先验信号；经 ``features.system.source_repo_rank`` 注入，只影响
    ssh_git 全库检索的**排序**，不改变任何证据判定语义（wiki 保持
    background 级）。federated_search 自身永不抛异常；这里再兜一层，
    保证排序信号故障不可能阻塞 SDK 检索。
    """
    try:
        from features.knowledge.external import federated_search
        from features.system.source_repo_rank import (
            configure_codesearch_hints,
            configure_knowledge_hints,
        )
    except Exception:  # pragma: no cover - feature 缺失时保持未注入
        return

    def _hints(query: str) -> list[tuple[str, float]]:
        try:
            data = federated_search(query, limit=5)
        except Exception:
            return []
        hints: dict[str, float] = {}
        for hit in (data or {}).get("results") or []:
            anchors = hit.get("source_anchors") or []
            for anchor in anchors:
                repo = str((anchor or {}).get("repo") or "").strip()
                if not repo:
                    continue
                # 锚点 repo 可能是 AOSP 全名（platform/frameworks/base），
                # 取尾两段与相对路径前缀对齐。
                parts = repo.strip("/").split("/")
                rel = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
                hints[rel] = max(hints.get(rel, 0.0), 2.0)
                path = str((anchor or {}).get("path") or "").strip().strip("/")
                if path:
                    hints[path] = max(hints.get(path, 0.0), 1.5)
        return list(hints.items())

    configure_knowledge_hints(_hints)

    def _codesearch_hints(query: str) -> list[tuple[str, float]]:
        """OpenGrok 符号落点 → 仓前缀提示（权重最高：真实代码索引）。

        ``rk_codesearch`` 返回 dict 或 path/line 列表；两种形状都按路径
        前两段归一化成子仓相对前缀。任何异常静默降级（排序失败不得
        阻塞检索）。
        """
        try:
            from features.reports import ReportAnalyzer

            results = ReportAnalyzer().rk_codesearch(str(query or ""), max_results=5) or []
        except Exception:
            return []
        hints: dict[str, float] = {}
        for item in results:
            path = ""
            if isinstance(item, dict):
                path = str(item.get("path") or "").strip()
            elif isinstance(item, (list, tuple)) and item:
                path = str(item[0] or "").strip()
            path = path.strip("/").lower()
            if not path:
                continue
            parts = path.split("/")
            if len(parts) >= 2:
                prefix = "/".join(parts[:2])
                hints[prefix] = max(hints.get(prefix, 0.0), 3.0)
        return list(hints.items())

    configure_codesearch_hints(_codesearch_hints)


@dataclass
class AppServices:
    config: ConfigManager
    settings: RuntimeSettings
    redmine: RedmineService


def _wire_cross_feature_services() -> None:
    """Connect cross-feature seams that features cannot wire themselves.

    - cluster / automation: register the foundation access ports that
      devices / reports / system / test_execution / users consume.
    - email: per-request owner config manager (SMTP credentials live in the
      redmine config tree).
    - redmine: per-owner agent analyzer factories (reports / assistant).
    - device locks: identity-aware owner display-name resolver.
    """
    register_cluster_port()
    register_worker_status_port()
    register_devices_port()
    _wire_sdk_source_rank_hints()
    from features.ops import register_ops_ports

    register_ops_ports()
    # 依赖倒置：磁盘卫生需要"非终态 run 集合"保护暂存固件（fail-closed），
    # automation 经 foundation.automation_port 反向提供，避免 ops→automation
    # 内部模块直连闭合跨 feature 依赖。
    from features.automation.repository import AutomationStore
    from foundation.automation_port import configure_active_run_ids_provider
    from foundation.config import settings

    def _active_automation_run_ids() -> set[str]:
        store = AutomationStore(settings.data_root / "automation/automation.sqlite3")
        # 必须全量查询非终态任务：list_runs 的 limit 上限（500）会把更早
        # 入队/在途的任务漏掉，其暂存固件会被清理语义误判为可回收。
        return set(store.active_run_ids())

    configure_active_run_ids_provider(_active_automation_run_ids)
    configure_manager_provider(get_redmine_config_for_request)
    # 依赖倒置：报告附件解析（owner 校验 + ZIP 打包）由 reports
    # feature 提供，email 通过 provider 使用——避免 email→reports 直连
    # 闭合跨 feature 依赖环。
    from features.reports.files_api import resolve_report_attachments

    configure_attachment_resolver(resolve_report_attachments)
    configure_agent_factories(
        report_analyzer_factory=ReportAnalyzer,
        ai_analyzer_factory=_universal_analyzer_factory,
    )
    configure_display_name_resolver(devices_display_name_resolver)


def build_services(
    *,
    runtime_settings: RuntimeSettings | None = None,
) -> AppServices:
    selected = settings if runtime_settings is None else runtime_settings
    _wire_cross_feature_services()
    redmine_repository = RedmineAgentDB(
        db_path=selected.data_root / "redmine/redmine.sqlite3",
        docs_dir=selected.data_root / "redmine/docs",
    )
    return AppServices(
        config=ConfigManager(project_root=selected.project_root),
        settings=selected,
        redmine=RedmineService(
            repository=redmine_repository,
            agent=RedmineAgent(
                redmine_repository,
                report_analyzer_factory=ReportAnalyzer,
                ai_analyzer_factory=_universal_analyzer_factory,
            ),
        ),
    )
