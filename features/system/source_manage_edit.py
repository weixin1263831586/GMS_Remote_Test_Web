"""SDK 源码源配置编辑与连接目标解析（拆分管理 API 行数上限）。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from .source_user_store import UserSourceStoreError, validate_repo_root


@dataclass(frozen=True)
class SourceEditDependencies:
    """Injected at the HTTP boundary to avoid importing the router from service code.

    Test doubles and the production route use the same dependency path.
    """
    store_factory: Callable[[], Any]
    error_response: Callable[[Exception], Any]
    password_resolver: Callable[..., str]
    remote_probe: Callable[..., dict[str, Any]]
    local_probe: Callable[..., dict[str, Any]]
    public_record: Callable[[dict[str, Any]], dict[str, Any]]
    reload_runtime: Callable[[], Any]
    logger: Any


def _parse_target(target: str) -> dict[str, Any]:
    """解析手动输入的连接目标：``user@10.0.0.9`` / ``user@host:2222``。

    返回 {host, port, username}；用户名不设缺省（root 连接太危险），
    留空直接 422。
    """
    text = str(target or "").strip()
    if not text:
        raise UserSourceStoreError("请输入编译服务器地址（如 user@10.0.0.9）")
    # 去掉可能粘贴进来的 scheme 与空白
    text = text.replace("ssh://", "").strip()
    if any(ch.isspace() for ch in text) or "@" in text.replace("@", "", 1):
        raise UserSourceStoreError("服务器地址格式非法（应为 user@host[:port]）")
    username, sep, hostport = text.rpartition("@")
    if not sep:
        raise UserSourceStoreError("请使用 user@host 格式（如 user@10.0.0.9）")
    if "@" in hostport:
        raise UserSourceStoreError("服务器地址格式非法（多个 @）")
    host, port_str = hostport, ""
    if ":" in hostport:
        host, port_str = hostport.rsplit(":", 1)
    host = host.strip()
    username = username.strip()
    if not host or not username:
        raise UserSourceStoreError("服务器地址缺少主机名或用户名")
    if any(ch.isspace() for ch in host) or any(ch.isspace() for ch in username):
        raise UserSourceStoreError("主机名/用户名不能包含空白")
    port = 22
    if port_str:
        try:
            port = int(port_str)
        except ValueError as exc:
            raise UserSourceStoreError("端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise UserSourceStoreError("端口必须在 1-65535 之间")
    return {"host": host, "port": port, "username": username}


def reload_persisted_source_runtime(
    reload_runtime: Callable[[], Any], logger: Any, operation: str,
) -> tuple[str, bool]:
    """Report a committed SDK store write independently of runtime reload."""
    for attempt in range(2):
        try:
            return reload_runtime(), True
        except Exception:
            if attempt == 0:
                logger.warning(
                    "SDK 源 %s 已写入，但首次热重载失败，重试一次", operation,
                    exc_info=True,
                )
            else:
                logger.exception(
                    "SDK 源 %s 已持久化，第二次热重载仍失败", operation,
                )
    return "RELOAD_FAILED", False


def source_runtime_reload_warning() -> str:
    return (
        "SDK 配置变更已保存，但运行时热重载失败。"
        "请刷新列表确认配置并检查服务日志，不要重复提交。"
    )


def persisted_sdk_write_response(
    reload_runtime: Callable[[], Any], logger: Any, operation: str,
    data: dict[str, Any], *, changed: bool = True,
) -> dict[str, Any]:
    """Central response contract for all committed SDK create/delete/batch writes."""
    state, ok = (
        reload_persisted_source_runtime(reload_runtime, logger, operation)
        if changed else ("UNCHANGED", True)
    )
    if isinstance(data.get("source"), dict):
        data["source"]["registry_state"] = state
    for record in data.get("created", []):
        record["registry_state"] = state
    data["registry_state"] = state
    data["runtime_reload_ok"] = ok
    result = {"success": True, "data": data}
    if not ok:
        result["warning"] = source_runtime_reload_warning()
    return result


def update_source_config(
    request: Request, source_id: str, req: dict[str, Any],
    *, services: SourceEditDependencies,
):
    """编辑用户注册 SDK 源：元数据更新不重新连接，迁移路径/服务器须探测。

    source_id/provider 代表稳定的检索身份，禁止通过此接口更名/改类型；
    SSH 凭据仅替换时才写入，永远不会随响应返回。
    """
    # The route owns its live dependencies; the service has no reverse import.
    allowed = {
        "repo_root", "target", "password", "default_revision",
        "android_version", "label", "discover_repos", "discover_ttl_hours",
        "expected_config_fingerprint",
    }
    if not req or set(req) - allowed:
        return services.error_response(UserSourceStoreError(
            "SDK 源更新字段为空或包含禁止修改的字段", status_code=422,
        ))
    fingerprint = str(req.get("expected_config_fingerprint") or "").strip()
    if not fingerprint:
        return services.error_response(UserSourceStoreError(
            "缺少配置版本指纹，请刷新 SDK 列表后重试", status_code=422,
        ))
    try:
        store = services.store_factory()
        current = store.get(source_id, expected_fingerprint=fingerprint)
        provider = current.get("provider")
        fields: dict[str, Any] = {
            key: req[key] for key in (
                "repo_root", "default_revision", "android_version",
                "label", "discover_repos", "discover_ttl_hours",
            ) if key in req
        }
        root = validate_repo_root(fields.get("repo_root", current["repo_root"]))
        fields["repo_root"] = root
        target_changed = False
        if provider == "ssh_git":
            old_target = (
                str(current["ssh_username"]), str(current["ssh_host"]),
                int(current.get("ssh_port") or 22),
            )
            if "target" in req:
                connection = _parse_target(req["target"])
                new_target = (
                    connection["username"], connection["host"], connection["port"],
                )
                target_changed = new_target != old_target
                fields.update({
                    "ssh_username": connection["username"],
                    "ssh_host": connection["host"],
                    "ssh_port": connection["port"],
                })
            else:
                new_target = old_target
            supplied_password = str(req.get("password") or "")
            if len(supplied_password) > 128:
                raise UserSourceStoreError("SSH 口令过长", status_code=422)
            # 切换连接目标不可把旧服务器的口令带到新服务器。
            if target_changed:
                password = services.password_resolver(
                    new_target[1], new_target[0], new_target[2], supplied_password,
                )
                fields["ssh_password"] = password  # 无凭据则使用 SSH key
            elif supplied_password:
                password = supplied_password
                fields["ssh_password"] = password
            else:
                password = store.password_from_record(current)
            if target_changed or root != current["repo_root"] or supplied_password:
                target = f"{new_target[0]}@{new_target[1]}:{new_target[2]}"
                probe = services.remote_probe(target, root, password)
            else:
                probe = None
        elif provider == "local_git":
            if "target" in req or "password" in req:
                raise UserSourceStoreError("本机 SDK 源不支持 SSH 连接参数", status_code=422)
            probe = services.local_probe(root) if root != current["repo_root"] else None
        else:
            raise UserSourceStoreError(
                "此源由部署配置管理，不支持在此编辑", status_code=422,
            )
        if probe is not None:
            if not probe.get("path_exists"):
                raise UserSourceStoreError(f"路径不存在：{root}", status_code=422)
            if not (
                probe.get("git_repo_count") or probe.get("is_git_repo")
                or probe.get("is_repo_workspace") or probe.get("is_android_tree")
            ):
                raise UserSourceStoreError(
                    f"目录不是有效的 SDK/源码树：{root}", status_code=422,
                )
        # update 在同一跨进程文件锁内进行版本比较与写入。
        record = store.update(
            source_id, fields=fields,
            expected_fingerprint=fingerprint,
            updated_by=getattr(request.state, "username", "") or "",
        )
    except UserSourceStoreError as exc:
        return services.error_response(exc)
    except (RuntimeError, OSError) as exc:
        services.logger.exception("SDK 源编辑保存失败")
        return services.error_response(exc)

    # The durable write already committed. A reload failure must not be
    # reported as a failed save: clients would retry with a stale fingerprint.
    state, reload_ok = reload_persisted_source_runtime(
        services.reload_runtime, services.logger, f"编辑 {source_id}",
    )
    # If both attempts fail, the existing source_registry() read path will
    # also retry rebuilding when the persisted store fingerprint is stale.
    # The edit response must never imply that the durable write failed.
    record["registry_state"] = state
    response = {
        "success": True,
        "data": {
            "source": services.public_record(record),
            "runtime_reload_ok": reload_ok,
        },
    }
    if not reload_ok:
        response["warning"] = source_runtime_reload_warning()
    return response
