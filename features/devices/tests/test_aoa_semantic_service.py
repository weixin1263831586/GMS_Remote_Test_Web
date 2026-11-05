"""Owned class issuance, revocation and reviewed semantic profile persistence."""
import copy
from types import SimpleNamespace

import pytest

from features.devices.aoa_certifications import AoaCertificationStore
from features.devices.aoa_semantic_service import authorize_semantic_options, semantic_options
from features.devices.aoa_service import AoaProfileStore
from foundation.error_model import ApiError
from tests.aoa_semantic_fixtures import reference, semantic_template


def receipts(owner="owner-1"):
    commands = {}
    for index, serial in enumerate(("REF-A", "REF-B")):
        commands[str(index)] = {"status": "completed", "command_type": "device_action", "worker_id": "w",
            "payload": {"owner_id": owner, "devices": [f"w:{serial}"], "usb_port_path": "1-2",
                        "lease_tokens": {serial: "fenced"}, "action": "aoa_record_input",
                        "aoa_record": {"action": "verify_direct"}},
            "result": {"success": True, "direct_reference_evidence": reference(serial)}}
    return commands


def issue(store, owner="owner-1"):
    commands = receipts(owner)
    return store.issue(owner, "Synthetic class", ["0", "1"], SimpleNamespace(get_command=commands.get))


def test_issue_is_owned_and_revocation_prevents_new_resolution(tmp_path):
    store = AoaCertificationStore(tmp_path)
    proof = issue(store)
    assert store.get(proof["id"], "owner-1") == proof
    with pytest.raises(ApiError):
        store.get(proof["id"], "other-owner")
    assert store.list("other-owner") == []
    store.revoke(proof["id"], "owner-1")
    with pytest.raises(ApiError):
        store.get(proof["id"], "owner-1")
    assert store.list("owner-1")[0]["revoked_at"]


@pytest.mark.parametrize("mutation", ["owner", "unfenced", "failed", "different-port", "different-device", "arbitrary-action"])
def test_client_receipts_cannot_impersonate_reference_evidence(tmp_path, mutation):
    store, commands = AoaCertificationStore(tmp_path), receipts()
    command = commands["0"]
    if mutation == "owner":
        command["payload"]["owner_id"] = "other"
    elif mutation == "unfenced":
        command["payload"]["lease_tokens"] = {}
    elif mutation == "failed":
        command["status"] = "failed"
    elif mutation == "different-port":
        command["payload"]["usb_port_path"] = "1-3"
    elif mutation == "different-device":
        command["payload"]["devices"] = ["another"]
    else:
        command["payload"]["aoa_record"]["action"] = "calibrate"
    with pytest.raises(ApiError):
        store.issue("owner-1", "Synthetic class", ["0", "1"], SimpleNamespace(get_command=commands.get))
    assert store.list("owner-1") == []


def test_resolution_and_dispatch_recheck_active_owned_proof(tmp_path, monkeypatch):
    store = AoaCertificationStore(tmp_path)
    proof = issue(store)
    monkeypatch.setattr("features.devices.aoa_semantic_service.certification_store", lambda: store)
    metadata = {"semantic_template": semantic_template(), "class_certificate_ids": [proof["id"]]}
    context = reference()["context"]
    opts = semantic_options(metadata, context, "owner-1", ["click 1 1"])
    assert authorize_semantic_options(opts, "owner-1", context, None, "w") == opts
    with pytest.raises(ApiError):
        authorize_semantic_options(opts, "other", context, None, "w")
    with pytest.raises(ApiError):
        authorize_semantic_options(opts, "owner-1", {**context, "locale": "zh-CN"}, None, "w")
    store.revoke(proof["id"], "owner-1")
    with pytest.raises(ApiError):
        authorize_semantic_options(opts, "owner-1", context, None, "w")


def test_automation_semantics_requires_successful_matching_firmware_receipt(tmp_path, monkeypatch):
    store = AoaCertificationStore(tmp_path)
    proof = issue(store)
    monkeypatch.setattr("features.devices.aoa_semantic_service.certification_store", lambda: store)
    opts = semantic_options({"semantic_template": semantic_template(), "class_certificate_ids": [proof["id"]]},
                            reference()["context"], "owner-1", ["click 1 1"])
    opts["aoa_firmware_sha256"] = "a" * 64
    command = {"status": "completed", "result": {"success": True}, "payload": {"sha256": "a" * 64}}
    repo = SimpleNamespace(find_correlated_command=lambda *args: command)
    assert authorize_semantic_options(opts, "owner-1", reference()["context"], repo, "w", "run-1") == opts
    command["payload"]["sha256"] = "b" * 64
    with pytest.raises(ApiError):
        authorize_semantic_options(opts, "owner-1", reference()["context"], repo, "w", "run-1")


def test_semantic_profile_roundtrip_and_incompatible_command_edit_rejected(tmp_path):
    store = AoaProfileStore(tmp_path)
    metadata = {"hid_touch_mode": "direct", "semantic_template": semantic_template(), "class_certificate_ids": ["test-cert"]}
    profile = store.create_profile("owner-1", "Semantic", "", ["click 1 1"], metadata=metadata)
    assert store.get_profile(profile["id"], "owner-1")["metadata"] == metadata
    with pytest.raises(ApiError):
        store.update_profile(profile["id"], "owner-1", commands=["click 1 1", "click 2 2"])
    assert store.get_profile(profile["id"], "owner-1")["commands"] == ["click 1 1"]
    bad = copy.deepcopy(metadata)
    bad["semantic_template"]["steps"][0].pop("after")
    with pytest.raises(ApiError):
        store.update_profile(profile["id"], "owner-1", metadata=bad)


def test_multiple_displays_require_explicit_target_and_keep_their_own_density(monkeypatch):
    from foundation.aoa_certification import build_certificate

    refs = [reference("REF-A"), reference("REF-B"), reference("REF-A", display_id=1), reference("REF-B", display_id=1)]
    for item in refs[2:]:
        item["context"]["density"] = 160
    proof = build_certificate(refs, certificate_id="two-displays", owner_id="owner-1", label="Synthetic displays")
    context = {**refs[0]["context"], "displays": [refs[2]["context"]]}
    template = semantic_template()
    other = semantic_template(refs[2]["context"])["layouts"][0]
    other["id"] = "external-layout"
    template["layouts"].append(other)
    metadata = {"semantic_template": template, "class_certificate_ids": ["two-displays"]}
    monkeypatch.setattr("features.devices.aoa_semantic_service.certification_store",
                        lambda: SimpleNamespace(get=lambda *args: proof))
    with pytest.raises(ValueError, match="unique"):
        semantic_options(metadata, context, "owner-1", ["click 1 1"])
    plan = semantic_options({**metadata, "target_display_id": 1}, context, "owner-1", ["click 1 1"])["aoa_semantic_plan"]
    assert plan["layout_id"] == "external-layout"
    assert plan["context"]["display_id"] == 1
    assert plan["context"]["density"] == 160
