"""Manual burn routes continue AOA before releasing their device lock."""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.firmware import firmware_api, runtime

from .test_burn_error_semantics import BurnErrorSemanticsTests as _BurnFixture


class FirmwareAoaRouteTests(unittest.TestCase):
    setUp = _BurnFixture.setUp
    tearDown = _BurnFixture.tearDown

    def burn(self, *, flash_ok=True, recovery_ok=True, preflight_error=None, declared_sha=""):
        calls = []
        prepared = {"device_id": "local:D1", "snapshot": {
            "firmware_compatibility": {"sha256": declared_sha} if declared_sha else None}}

        async def batch(**kwargs):
            calls.append("flash")
            return [{"device": "D1", "success": flash_ok}], None if flash_ok else "flash failed"

        async def recover(*args):
            calls.append("aoa")
            return {"success": recovery_ok, "error": "ADB unauthorized"}

        async def release(*args):
            calls.append("release")

        with ExitStack() as stack:
            stack.enter_context(patch.object(firmware_api, "prepare_recovery", return_value=prepared,
                                            side_effect=preflight_error))
            stack.enter_context(patch.object(firmware_api, "validate_local_firmware_recovery"))
            stack.enter_context(patch.object(firmware_api, "release_local_firmware_recovery"))
            stack.enter_context(patch.object(firmware_api, "_scp_put_with_deadline", new=AsyncMock()))
            stack.enter_context(patch.object(firmware_api, "_remote_sha256", new=AsyncMock(return_value="b" * 64)))
            stack.enter_context(patch.object(firmware_api, "_prepare_usbip_firmware_routes",
                                            new=AsyncMock(return_value=([], ""))))
            stack.enter_context(patch.object(firmware_api, "_device_flash_protocols", return_value={"D1": "rockusb-loader"}))
            stack.enter_context(patch.object(firmware_api, "_run_local_firmware_batch", new=batch))
            stack.enter_context(patch.object(firmware_api, "recover_after_burn", new=recover))
            stack.enter_context(patch.object(firmware_api, "validate_remote_update_image", return_value=SimpleNamespace(valid=True)))
            stack.enter_context(patch.object(runtime, "release_firmware_devices", new=release))
            stack.enter_context(patch.object(runtime, "store_notification"))
            response = self.client.post("/api/burn/firmware?devices=D1", data={
                "firmware_path": "/tmp/update.img", "aoa_profile_id": "wizard"})
        return response, calls

    def test_success_verifies_aoa_before_unlocking(self):
        response, calls = self.burn()
        assert response.status_code == 200, response.text
        assert calls == ["flash", "aoa", "release"]

    def test_flash_failure_never_runs_aoa(self):
        response, calls = self.burn(flash_ok=False)
        assert response.status_code == 502, response.text
        assert calls == ["flash", "release"]

    def test_aoa_failure_returns_flash_success_without_retrying_flash(self):
        response, calls = self.burn(recovery_ok=False)
        assert response.status_code == 502, response.text
        assert response.json()["details"]["flash_success"] is True
        assert calls == ["flash", "aoa", "release"]

    def test_preflight_conflict_is_409_and_never_flashes(self):
        from foundation.error_model import ApiError

        response, calls = self.burn(preflight_error=ApiError.conflict("USB port changed"))
        assert response.status_code == 409, response.text
        assert calls == ["release"]

    def test_actual_remote_image_must_match_declared_firmware_before_flash(self):
        response, calls = self.burn(declared_sha="a" * 64)
        assert response.status_code == 409, response.text
        assert "SHA-256" in response.json()["error"]
        assert calls == ["release"]

    def test_matching_remote_image_can_continue_recovery(self):
        response, calls = self.burn(declared_sha="b" * 64)
        assert response.status_code == 200, response.text
        assert calls == ["flash", "aoa", "release"]
