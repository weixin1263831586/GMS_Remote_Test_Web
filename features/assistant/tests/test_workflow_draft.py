"""WorkflowDraft 回归：真实失败对话的原始整句指令 + 阻塞/重规划场景。

固定回归用例来自一次真实失败对话（复盘记录见仓库根目录 scratch 笔记）：
``设备66df75a34ebde4a4 烧写固件https://.../download，同时执行AOA配置
RK3588_evb1_A17_dpi320_wizard-to-usb-debug，然后支持CTS测试``
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from features.assistant import workflow_draft as wd
from features.assistant.intent import _extract_device_ids


ORIGINAL_MESSAGE = (
    "设备66df75a34ebde4a4 烧写固件"
    "https://172.16.14.233:5001/api/firmware-shares/"
    "16ec2353bc314483b5a8c92e582a540d/download，"
    "同时执行AOA配置RK3588_evb1_A17_dpi320_wizard-to-usb-debug，"
    "然后支持CTS测试"
)


# ==================== P0: 设备序列号解析 ====================

class TestDeviceSerialExtraction:
    def test_original_message_extracts_exact_serial(self):
        # 复盘核心缺陷：CJK 与 ASCII 相邻不构成 \b，序列号被漏掉。
        assert _extract_device_ids(ORIGINAL_MESSAGE) == ["66df75a34ebde4a4"]

    def test_url_and_aoa_tokens_are_not_device_ids(self):
        text = (
            "固件https://172.16.14.233:5001/api/firmware-shares/"
            "16ec2353bc314483b5a8c92e582a540d/download "
            "AOA配置RK3588_evb1_A17_dpi320_wizard-to-usb-debug"
        )
        assert _extract_device_ids(text) == []

    def test_plain_serials_still_recognized(self):
        assert _extract_device_ids("序列号 RK3562GMS7") == ["RK3562GMS7"]
        assert _extract_device_ids("设备 0123456789ABCDEF") == ["0123456789ABCDEF"]


# ==================== 复合意图与实体 ====================

class TestCompositeIntent:
    def test_original_message_is_composite(self):
        assert wd.detect_composite_intent(ORIGINAL_MESSAGE) is True

    def test_plain_test_request_is_not_composite(self):
        assert wd.detect_composite_intent("跑 CtsWifiTestCases，失败 retry 2 次") is False

    def test_entities_extracted_without_pollution(self):
        entities = wd.extract_workflow_entities(ORIGINAL_MESSAGE)
        assert entities["device_serials"] == ["66df75a34ebde4a4"]
        assert entities["firmware_share_id"] == "16ec2353bc314483b5a8c92e582a540d"
        assert entities["aoa_profile"] == "RK3588_evb1_A17_dpi320_wizard-to-usb-debug"
        assert entities["test_type"] == "CTS"


# ==================== 草案构建 / 更新 / 阻塞 ====================

class TestDraftLifecycle:
    def _draft(self):
        entities = wd.extract_workflow_entities(ORIGINAL_MESSAGE)
        return wd.build_workflow_draft(entities, {"worker_id": "", "scope_mode": "single"})

    def test_draft_shape(self):
        draft = self._draft()
        assert draft["intent"] == "flash_aoa_test"
        assert draft["device"]["serial"] == "66df75a34ebde4a4"
        assert draft["device"]["worker_id"] is None  # 待全局库存解析
        assert draft["firmware"]["source"] == "firmware_share"
        assert draft["aoa"]["mode"] == "user_build_recovery"
        assert draft["test"]["type"] == "CTS"
        assert draft["status"] == "draft"
        assert draft["plan_version"] == 1

    def test_replan_preserves_entities_and_bumps_version(self):
        draft = self._draft()
        updated, applied = wd.update_draft(draft, "改成 GTS，换 worker w9")
        assert applied, "修改意见应命中更新规则"
        assert updated["plan_version"] == 2
        assert updated["device"]["serial"] == "66df75a34ebde4a4"
        assert updated["firmware"]["share_id"] == "16ec2353bc314483b5a8c92e582a540d"
        assert updated["test"]["type"] == "GTS"
        assert updated["device"]["worker_id"] == "w9"

    def test_missing_serial_blocks(self):
        draft = self._draft()
        draft["device"]["serial"] = ""
        blocks = wd.draft_blocks(draft)
        assert any(block["stage"] == "device" for block in blocks)

    def test_expired_share_is_preflight_block(self):
        draft = self._draft()
        result = wd.preflight_draft(
            draft,
            share_lookup=lambda _sid: {"ok": False, "reason": "固件分享已过期"},
            device_lookup=lambda _s, _w: {"ok": True, "worker_id": "w1"},
            aoa_lookup=lambda _p, _o: {"ok": True},
            suite_lookup=lambda _t, _w: {"ok": True, "detail": "android-cts"},
        )
        assert result["ready"] is False
        assert any(block["stage"] == "firmware" for block in result["blocks"])

    def test_busy_device_blocks_without_replacement(self):
        draft = self._draft()
        result = wd.preflight_draft(
            draft,
            share_lookup=lambda _sid: {"ok": True},
            device_lookup=lambda _s, _w: {"ok": False, "reason": "设备被占用，不会自动换设备"},
            aoa_lookup=lambda _p, _o: {"ok": True},
            suite_lookup=lambda _t, _w: {"ok": True},
        )
        assert result["ready"] is False
        assert any("被占用" in block.get("reason", "") for block in result["blocks"])

    def test_ready_preflight_resolves_worker(self):
        draft = self._draft()
        result = wd.preflight_draft(
            draft,
            share_lookup=lambda _sid: {"ok": True},
            device_lookup=lambda _s, _w: {"ok": True, "worker_id": "w7", "detail": "在线"},
            aoa_lookup=lambda _p, _o: {"ok": True},
            suite_lookup=lambda _t, _w: {"ok": True},
        )
        assert result["ready"] is True
        assert draft["device"]["worker_id"] == "w7"

    def test_flash_only_skips_test_preflight(self):
        draft = self._draft()
        draft["test"]["scope"] = "skip"
        result = wd.preflight_draft(
            draft,
            share_lookup=lambda _sid: {"ok": True},
            device_lookup=lambda _s, _w: {"ok": True, "worker_id": "w1"},
            aoa_lookup=lambda _p, _o: {"ok": True},
            suite_lookup=lambda _t, _w: {"ok": False, "reason": "没有套件"},
        )
        assert result["ready"] is True

    def test_plan_actions(self):
        assert wd.detect_plan_action("重新规划") == "replan"
        assert wd.detect_plan_action("取消计划") == "cancel"
        assert wd.detect_plan_action("计划状态") == "status"
        assert wd.detect_plan_action("跑 CtsWifiTestCases") == ""


# ==================== 自动化请求体转换 ====================

class TestRunBodyConversion:
    def test_body_from_draft(self):
        from features.assistant.workflow_execution import run_body_from_draft

        draft = wd.build_workflow_draft(
            wd.extract_workflow_entities(ORIGINAL_MESSAGE), {}
        )
        draft["device"]["worker_id"] = "w1"
        body = run_body_from_draft(draft, {})
        assert body["source_type"] == "assistant_chat"
        assert body["devices"] == ["66df75a34ebde4a4"]
        plan = body["test_plan"]
        assert plan["flash"]["firmware_share_id"] == "16ec2353bc314483b5a8c92e582a540d"
        assert plan["worker_id"] == "w1"
        assert plan["aoa"]["mode"] == "user_build_recovery"
        assert plan["aoa"]["profile"] == "RK3588_evb1_A17_dpi320_wizard-to-usb-debug"
        assert plan["test_type"] == "CTS"
        assert "test_mode" not in plan

    def test_flash_only_body_uses_test_mode_skip(self):
        """纯烧写草案：test_mode=skip，不带空 test_type（否则契约拒绝）。"""
        from features.assistant.workflow_execution import run_body_from_draft

        draft = wd.build_workflow_draft(
            wd.extract_workflow_entities(ORIGINAL_MESSAGE), {}
        )
        draft["test"]["scope"] = "skip"
        draft["device"]["worker_id"] = "w1"
        plan = run_body_from_draft(draft, {})["test_plan"]
        assert plan["test_mode"] == "skip"
        assert "test_type" not in plan
        assert plan["flash"]["firmware_share_id"] == "16ec2353bc314483b5a8c92e582a540d"


# ==================== 固件分享来源 ====================

class TestFirmwareShareSource:
    def test_snapshot_missing_share(self):
        from features.firmware import firmware_share_record_snapshot, shares_api

        with patch.object(shares_api, "_find_record", return_value=([], None)):
            assert firmware_share_record_snapshot("deadbeef") is None

    def test_snapshot_expired_flag(self):
        import time

        from features.firmware import firmware_share_record_snapshot, shares_api

        record = {"id": "cafe", "expires_at": int(time.time()) - 10}
        with patch.object(shares_api, "_find_record", return_value=([], record)):
            snapshot = firmware_share_record_snapshot("cafe")
        assert snapshot["expired"] is True
        assert "password" not in snapshot

    def _cluster_stub(self):
        return SimpleNamespace(
            effective_enabled=True,
            config=SimpleNamespace(local_worker_id="w1"),
            repository=SimpleNamespace(
                list_suites=lambda worker_id="": [],
                list_devices=lambda worker_id="": [
                    {"id": "w1:serial-a", "state": "available", "transport": "usb"},
                ],
            ),
            list_workers=lambda: [{"id": "w1"}],
            has_command_agent=lambda _w: True,
        )

    def _service(self):
        from features.automation import AutomationService

        service = AutomationService.__new__(AutomationService)
        service._cluster_provider = lambda: self._cluster_stub()
        return service

    _SHARE_OK = {"id": "cafebabe", "expired": False}

    def test_flash_only_preflight_passes_without_test_type(self):
        """test_mode=skip（纯烧写）：不要求 test_type 与测试套件。"""
        body = {
            "source_type": "assistant_chat",
            "devices": ["serial-a"],
            "test_plan": {
                "test_mode": "skip",
                "worker_id": "w1",
                "flash": {"mode": "firmware", "firmware_share_id": "cafebabe"},
            },
        }
        with patch(
            "features.firmware.firmware_share_record_snapshot",
            return_value=dict(self._SHARE_OK),
        ):
            result = self._service().preflight(body)
        assert result["ready"] is True

    def test_missing_test_type_without_skip_is_rejected(self):
        """不带 test_mode=skip 时漏填 test_type 仍拒绝，不静默降级纯烧写。"""
        body = {
            "devices": ["serial-a"],
            "test_plan": {
                "worker_id": "w1",
                "flash": {"mode": "skip"},
            },
        }
        with pytest.raises(ValueError, match="test_type is required"):
            self._service().preflight(body)

    def test_automation_preflight_requires_valid_share(self):
        """分享缺失/过期必须在计划校验期拒绝（不等到烧写阶段）。"""
        from features.automation import AutomationService

        body = {
            "source_type": "assistant_chat",
            "devices": ["serial-a"],
            "test_plan": {
                "test_type": "CTS",
                "worker_id": "w1",
                "flash": {"mode": "firmware", "firmware_share_id": "cafebabe"},
            },
        }
        cluster = SimpleNamespace(
            effective_enabled=True,
            config=SimpleNamespace(local_worker_id="w1"),
            repository=SimpleNamespace(list_suites=lambda worker_id="": []),
            list_workers=lambda: [{"id": "w1"}],
            has_command_agent=lambda _w: True,
        )
        service = AutomationService.__new__(AutomationService)
        service._cluster_provider = lambda: cluster
        with patch(
            "features.firmware.firmware_share_record_snapshot",
            return_value=None,
        ), pytest.raises(ValueError, match="Firmware share not found"):
            service._prepare_cluster_plan(body, body["test_plan"])
        expired = {"id": "cafebabe", "expired": True}
        with patch(
            "features.firmware.firmware_share_record_snapshot",
            return_value=expired,
        ), pytest.raises(ValueError, match="expired"):
            service._prepare_cluster_plan(body, body["test_plan"])


# ==================== P0: 冻结约束在规范化草案上校验 ====================

class TestConstraintConflictsOnNormalizedDraft:
    """回归（深度审核 P0）：模型省略冻结字段同样构成冲突。

    用户原始约束必须在 build_workflow_draft 规范化后的草案上成立：
    省略 = 缺失，不允许靠模型漏报把「CTS 工作流」静默降级成 flash-only。
    """

    CONSTRAINTS = {
        "device_serial": "66df75a34ebde4a4",
        "firmware_share_id": "abc12345",
        "aoa_requested": True,
        "aoa_profile": "RK3588_wizard",
        "test_type": "CTS",
        "connect_wifi": True,
        "test_module": "CtsCamera",
        "test_case": "android.hardware.camera2",
        "version": 1,
    }

    def test_omitted_test_type_conflicts(self):
        # 用例 1：用户要求 CTS，模型遗漏 test_type → 拒绝，不能变 flash-only
        entities = {
            "device_serials": ["66df75a34ebde4a4"],
            "firmware_share_id": "abc12345",
        }
        conflicts = wd.constraint_conflicts(entities, self.CONSTRAINTS)
        assert any("测试类型 CTS" in item for item in conflicts)

    def test_omitted_firmware_share_conflicts(self):
        # 用例 2：用户指定 firmware_share_id，模型不返回该字段 → 冲突
        entities = {
            "device_serials": ["66df75a34ebde4a4"],
            "test_type": "cts",
        }
        conflicts = wd.constraint_conflicts(entities, self.CONSTRAINTS)
        assert any("固件分享 abc12345" in item for item in conflicts)

    def test_omitted_aoa_and_test_conflicts(self):
        # 用例 3：用户要求 AOA + CTS，模型仅返回固件和设备 → 明确缺阶段
        entities = {
            "device_serials": ["66df75a34ebde4a4"],
            "firmware_share_id": "abc12345",
        }
        conflicts = wd.constraint_conflicts(entities, self.CONSTRAINTS)
        assert any("AOA 恢复" in item for item in conflicts)
        assert any("测试类型 CTS" in item for item in conflicts)

    def test_omitted_wifi_module_case_conflict(self):
        entities = {
            "device_serials": ["66df75a34ebde4a4"],
            "firmware_share_id": "abc12345",
            "aoa_profile": "RK3588_wizard",
            "aoa_requested": True,
            "test_type": "CTS",
        }
        conflicts = wd.constraint_conflicts(entities, self.CONSTRAINTS)
        assert any("Wi-Fi" in item for item in conflicts)
        assert any("测试模块 CtsCamera" in item for item in conflicts)
        assert any("测试用例 android.hardware.camera2" in item for item in conflicts)

    def test_full_echo_has_no_conflict(self):
        entities = {
            "device_serials": ["66df75a34ebde4a4"],
            "firmware_share_id": "abc12345",
            "aoa_profile": "RK3588_wizard",
            "aoa_requested": True,
            "test_type": "cts",
            "connect_wifi": True,
            "test_module": "CtsCamera",
            "test_case": "android.hardware.camera2",
        }
        assert wd.constraint_conflicts(entities, self.CONSTRAINTS) == []

    def test_substitution_still_conflicts(self):
        entities = {
            "device_serials": ["other-device"],
            "firmware_share_id": "deadbeef",
            "test_type": "GTS",
        }
        conflicts = wd.constraint_conflicts(entities, self.CONSTRAINTS)
        assert any("设备 66df75a34ebde4a4" in item for item in conflicts)
        assert any("固件分享 abc12345" in item for item in conflicts)
        assert any("测试类型 CTS" in item for item in conflicts)

    def test_stated_test_scope_freeze_and_downgrade_conflict(self):
        # 用户明确"完整跑 CTS"后模型降级成 flash-only → 范围冲突
        assert wd._stated_test_scope({"flash_only": True}) == "skip"
        assert wd._stated_test_scope({"test_module": "CtsCamera"}) == "module"
        assert wd._stated_test_scope({"test_scope_full": True}) == "full"
        assert wd._stated_test_scope({}) == ""
        constraints = wd.freeze_user_constraints(
            {"test_type": "cts", "test_scope_full": True}
        )
        assert constraints["test_scope"] == "full"
        conflicts = wd.constraint_conflicts(
            {"test_type": "cts", "flash_only": True}, constraints
        )
        assert conflicts == ["测试范围 full（不得更换或遗漏）"]

    def test_unstated_fields_stay_open(self):
        # 未冻结的字段仍允许模型补全（如模型补出测试模块）
        constraints = wd.freeze_user_constraints({"test_type": "CTS"})
        entities = {"test_type": "CTS", "test_module": "CtsCamera"}
        assert wd.constraint_conflicts(entities, constraints) == []


class TestDropAoaResetsFrozenConstraints:
    """审核 P1：显式删除必须产生约束变更事件，不能只合并非空值。

    「去掉 AOA」后冻结的 aoa_requested/aoa_profile 必须被清除，否则
    后续模型重新规划会误认为用户仍要求 AOA；版本号必须递增保留审计。
    """

    def _draft_with_frozen_aoa(self):
        entities = wd.extract_workflow_entities(
            "设备66df75a34ebde4a4 烧写固件"
            "https://172.16.14.233:5001/api/firmware-shares/"
            "16ec2353bc314483b5a8c92e582a540d/download，"
            "同时执行AOA配置RK3588_wizard，然后支持CTS测试"
        )
        draft = wd.build_workflow_draft(entities, {})
        constraints = wd.freeze_user_constraints(
            wd.extract_workflow_entities(
                "设备66df75a34ebde4a4 烧写固件"
                "https://172.16.14.233:5001/api/firmware-shares/"
                "16ec2353bc314483b5a8c92e582a540d/download，"
                "同时执行AOA配置RK3588_wizard，然后支持CTS测试"
            )
        )
        draft["user_constraints"] = constraints
        return draft

    def test_drop_aoa_clears_frozen_aoa_keys(self):
        draft = self._draft_with_frozen_aoa()
        assert draft["user_constraints"]["aoa_requested"] is True
        assert draft["user_constraints"]["aoa_profile"]

        updated, applied = wd.update_draft(draft, "去掉 AOA")
        assert "去掉 AOA" in applied
        constraints = updated["user_constraints"]
        assert "aoa_requested" not in constraints
        assert "aoa_profile" not in constraints
        # 版本历史保留：撤回也是一次约束变更事件
        assert constraints["version"] >= 2
        assert updated["aoa"]["mode"] == "off"

    def test_replan_after_drop_does_not_restore_aoa(self):
        draft = self._draft_with_frozen_aoa()
        updated, _applied = wd.update_draft(draft, "去掉 AOA")
        # 模型重新规划时省略 AOA：不再构成冲突（用户已撤回要求）
        entities = {
            "device_serials": ["66df75a34ebde4a4"],
            "firmware_share_id": "16ec2353bc314483b5a8c92e582a540d",
            "test_type": "CTS",
        }
        assert wd.constraint_conflicts(entities, updated["user_constraints"]) == []

    def test_unrelated_edit_keeps_aoa_constraints(self):
        draft = self._draft_with_frozen_aoa()
        updated, applied = wd.update_draft(draft, "改成 GTS")
        assert "改成 GTS/VTS/STS/CTS" in applied
        constraints = updated["user_constraints"]
        # 未撤回的要求不得被无关修改误清
        assert constraints["aoa_requested"] is True
        assert constraints["aoa_profile"]
        assert constraints["test_type"] == "GTS"
