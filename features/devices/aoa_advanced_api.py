"""Human-owned class certification and deterministic semantic template lookup."""
from fastapi import APIRouter, Request

from features.auth import principal_owner_id, require_human_principal
from foundation import cluster_port
from foundation.error_model import ApiError

from .aoa_certifications import certification_store
from .aoa_mapping_service import trusted_target_context
from .aoa_semantic_service import semantic_options
from .aoa_service import profile_store


router = APIRouter()


@router.post("/certifications/verify")
async def verify_reference(request: Request, body: dict):
    require_human_principal(request)
    if not body.get("worker_id") or not body.get("device"):
        raise ApiError.malformed_request("worker_id and device are required")
    return await cluster_port.submit_device_action(request, {
        "worker_id": str(body["worker_id"]), "devices": [str(body["device"])],
        "action": "aoa_record_input", "aoa_record": {"action": "verify_direct", "hid_touch_mode": "direct",
                                                       "display_id": body.get("display_id", 0)}})


@router.post("/certifications")
def issue_certificate(request: Request, body: dict):
    require_human_principal(request)
    try:
        repository = cluster_port.get_cluster_service().repository
    except RuntimeError as exc:
        raise ApiError.dependency_unavailable("类别认证回执服务不可用") from exc
    certificate = certification_store().issue(principal_owner_id(request), body.get("label"),
        body.get("command_ids"), repository)
    return {"success": True, "certificate": certificate}


@router.get("/certifications")
def list_certificates(request: Request):
    require_human_principal(request)
    return {"success": True, "certificates": certification_store().list(principal_owner_id(request))}


@router.delete("/certifications/{certificate_id}")
def revoke_certificate(request: Request, certificate_id: str):
    require_human_principal(request)
    certification_store().revoke(certificate_id, principal_owner_id(request))
    return {"success": True}


@router.post("/templates/resolve")
def resolve_template(request: Request, body: dict):
    require_human_principal(request)
    if body.get("display_id") is not None and (type(body["display_id"]) is not int or not 0 <= body["display_id"] <= 255):
        raise ApiError.invalid_semantics("目标显示器 ID 必须是 0..255 的整数")
    worker, device = str(body.get("worker_id") or ""), str(body.get("device") or "")
    if not worker or not device:
        raise ApiError.malformed_request("worker_id and device are required")
    owner = principal_owner_id(request)
    context = trusted_target_context(worker, device)
    profiles = profile_store().list_profiles(owner)
    matches = []
    for profile in profiles:
        if body.get("profile_id") and profile["id"] != body["profile_id"]:
            continue
        metadata = profile.get("metadata") or {}
        if body.get("display_id") is not None:
            metadata = {**metadata, "target_display_id": body["display_id"]}
        if not metadata.get("semantic_template"):
            continue
        try:
            options = semantic_options(metadata, context, owner, profile["commands"])
        except (ValueError, ApiError):
            continue
        matches.append({"profile_id": profile["id"], "profile_name": profile["name"],
                        "layout_id": options["aoa_semantic_plan"]["layout_id"],
                        "certificate_id": options["aoa_semantic_plan"]["certificate_id"],
                        "display_id": options["aoa_semantic_plan"]["context"]["display_id"],
                        "commands": options["aoa_semantic_plan"]["commands"]})
    if len(matches) != 1:
        raise ApiError.conflict("没有唯一匹配的已认证语义模板，请核对固件、语言和显示配置")
    return {"success": True, "match": matches[0]}
