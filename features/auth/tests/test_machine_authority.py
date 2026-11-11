"""ADR 0012 machine capability token regressions.

The first implementation minted ``expires.nonce.principal.owner`` and
rebuilt the principal at verify time WITHOUT the capability snapshot, so
every plan capability (build.execute, devices.lease, tests.execute,
firmware.stage) was silently dropped after a Bearer round-trip: creation
passed, the workflow then 403'd mid-run. These tests pin the contract:
the token carries and restores the FULL snapshot, fails closed on tamper
and expiry, and survives ids containing the ``.`` framing separator.
"""

import time

import pytest

from features.auth.access import is_elevated
from features.auth.authority import (
    CAPABILITY_TOKEN_PREFIX,
    automation_authority,
    automation_granted_capabilities,
    automation_required_capabilities,
    mint_capability_token,
    verify_capability_token,
)
from features.auth.constants import AGENT_ROLE, CurrentUser


def human_principal(
    permissions=("tests.execute", "devices.lease", "firmware.stage"),
) -> CurrentUser:
    return CurrentUser(
        id="user-hcq",
        username="hcq",
        role="device_operator",
        extra_permissions=frozenset(permissions),
    )


def machine_principal() -> CurrentUser:
    granted = automation_granted_capabilities(
        human_principal(), {"test_type": "CTS"}
    )
    return automation_authority("ats_run.1", "user-hcq", granted)


def test_round_trip_preserves_capability_snapshot():
    principal = machine_principal()
    token = mint_capability_token(principal)

    restored = verify_capability_token(token)

    assert restored is not None
    assert restored.id == "automation:ats_run.1"
    assert restored.resource_owner_id == "user-hcq"
    assert restored.extra_permissions == principal.extra_permissions
    # plan capabilities survive the Bearer round-trip — this is THE bug:
    assert "tests.execute" in restored.extra_permissions
    assert "devices.lease" in restored.extra_permissions
    assert "firmware.stage" in restored.extra_permissions
    # plan without a build section compiles no build capabilities:
    assert "build.execute" not in restored.extra_permissions


def test_dotted_owner_and_run_ids_survive_the_framing():
    principal = automation_authority(
        "ats.run.123", "owner.name@example.com", ["tests.execute"]
    )
    token = mint_capability_token(principal)

    restored = verify_capability_token(token)

    assert restored is not None
    assert restored.id == "automation:ats.run.123"
    assert restored.resource_owner_id == "owner.name@example.com"
    assert restored.has_permission("tests.execute")


def test_tampered_payload_fails_closed():
    token = mint_capability_token(machine_principal())
    body = token[len(CAPABILITY_TOKEN_PREFIX):]
    parts = body.split(".")
    # The capability snapshot is field #5 (Base64); flipping a character
    # must invalidate the HMAC over the payload.
    tampered = [
        *parts[:4],
        ("A" if parts[4][0] != "A" else "B") + parts[4][1:],
        *parts[5:],
    ]
    forged = CAPABILITY_TOKEN_PREFIX + ".".join(tampered)

    assert verify_capability_token(forged) is None


def test_expired_token_fails_closed():
    token = mint_capability_token(machine_principal(), ttl_seconds=60)
    body = token[len(CAPABILITY_TOKEN_PREFIX):]
    parts = body.split(".")
    # Re-sign with an expired timestamp (test signs with the same derived
    # key through mint's helpers is not exposed; instead mint a fresh
    # token and rewind time via monkeypatched clock on verify).
    from features.auth import authority

    expires_b64 = parts[0]
    import base64

    expires = int(base64.urlsafe_b64decode(expires_b64 + "=" * (-len(expires_b64) % 4)))
    assert expires > int(time.time())

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(authority.time, "time", lambda: expires + 1)
        assert verify_capability_token(token) is None
    assert verify_capability_token(token) is not None  # still valid in real time


def test_machine_principal_cannot_mint_or_verify_widen_capabilities():
    # Human principals cannot mint machine tokens at all.
    with pytest.raises(ValueError):
        mint_capability_token(human_principal())
    # Verification never widens: unknown capability names in the snapshot
    # are dropped by automation_authority's allow-list.
    rogue = automation_authority("run-1", "user-hcq", ["admin", "*"])
    token = mint_capability_token(
        CurrentUser(
            id="automation:run-1",
            username="automation:run-1",
            role="agent_service",
            extra_permissions=frozenset({"admin", "*"}),
            resource_owner_id="user-hcq",
        )
    )
    restored = verify_capability_token(token)
    assert restored is not None
    # rogue names dropped; only the fixed MACHINE_PERMISSIONS floor remains:
    from features.auth.authority import MACHINE_PERMISSIONS

    assert restored.extra_permissions == MACHINE_PERMISSIONS
    assert rogue.has_permission("admin") is False


def test_missing_flash_plan_requires_firmware_stage_like_the_executor():
    # Executor semantics: only flash.mode == "skip" skips flashing.
    assert "firmware.stage" in automation_granted_capabilities(
        human_principal(), {"test_type": "CTS"}
    )
    assert "firmware.stage" in automation_granted_capabilities(
        human_principal(),
        {"test_type": "CTS", "flash": {"mode": "firmware"}},
    )
    assert "firmware.stage" not in automation_granted_capabilities(
        human_principal(),
        {"test_type": "CTS", "flash": {"mode": "skip"}},
    )


def test_unheld_capability_is_rejected_not_dropped():
    with pytest.raises(ValueError):
        automation_granted_capabilities(
            human_principal(permissions=["tests.execute"]),
            {"test_type": "CTS", "build": {"provider": "ssh"}},
        )


def test_elevated_human_session_consents_to_full_union():
    # 提权即同意：普通 user 角色经管理员重认证后可创建带烧写/设备预留的
    # 计划（与固件工作台对人类要求的门槛一致，ADR 0012 提权即同意）。
    plain_user = CurrentUser(id="u1", username="u1", role="user")
    with pytest.raises(ValueError):
        automation_granted_capabilities(plain_user, {"test_type": "CTS"})
    granted = automation_granted_capabilities(plain_user, {"test_type": "CTS"}, elevated=True)
    assert "firmware.stage" in granted
    assert "devices.lease" in granted
    # 快照仍然不超出计划所需：纯测试计划不带 build 能力。
    assert "build.execute" not in granted


def test_elevation_never_amplifies_bearer_identities():
    # 提权只帮助人类 cookie 会话：agent token 与机器 principal 是 Bearer
    # 身份，能力由 scope/快照固定，elevated 标志必须被忽略。
    agent = CurrentUser(id="agent:1", username="agent:1", role=AGENT_ROLE)
    with pytest.raises(ValueError):
        automation_granted_capabilities(agent, {"test_type": "CTS"}, elevated=True)
    machine = CurrentUser(
        id="automation:run-1", username="automation:run-1", role="agent_service",
        extra_permissions=frozenset({"jobs.read", "reports.read"}),
        resource_owner_id="u1",
    )
    with pytest.raises(ValueError):
        automation_granted_capabilities(machine, {"test_type": "CTS"}, elevated=True)
    worker = CurrentUser(id="w1", username="w1", role="worker_service")
    with pytest.raises(ValueError):
        automation_granted_capabilities(worker, {"test_type": "CTS"}, elevated=True)


def test_required_capabilities_is_principal_independent():
    # 需求编译与身份解耦：聊天预检用它解释缺失能力，create/preflight 用
    # automation_granted_capabilities 做持有校验，两者必须同源。
    required = automation_required_capabilities({"test_type": "CTS"})
    assert sorted(required) == sorted([
        "firmware.stage", "devices.lease", "devices.read",
        "devices.use_leased", "tests.execute", "tests.cancel",
    ])
    assert "build.execute" not in automation_required_capabilities(
        {"test_type": "CTS", "flash": {"mode": "skip"}}
    )


def test_bearer_principal_never_inherits_browser_elevation():
    # A machine capability (or agent token) riding a request whose browser
    # cookie session is still elevated must NOT be treated as elevated:
    # elevation is a human-session property (ADR 0010/0012).
    class FakeState:
        auth_method = "machine_authority"
        is_elevated = None

    class FakeRequest:
        state = FakeState()
        cookies = {"gms_auth": "leftover-elevated-session"}

    assert is_elevated(FakeRequest()) is False

    FakeState.auth_method = "invalid_capability_token"
    assert is_elevated(FakeRequest()) is False

    FakeState.auth_method = "agent_token"
    assert is_elevated(FakeRequest()) is False
