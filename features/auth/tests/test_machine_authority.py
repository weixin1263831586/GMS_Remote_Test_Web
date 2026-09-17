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
    mint_capability_token,
    verify_capability_token,
)
from features.auth.constants import CurrentUser


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
