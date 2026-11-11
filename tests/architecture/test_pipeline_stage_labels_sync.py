"""Guard: pipeline stage tables mirrored across ATS UI, Agent page and backend.

The 18-stage pipeline model exists in three hand-maintained copies that an
iframe boundary keeps from sharing one constant:

- ``features/automation/ui/state.js``          (ATS 页阶段条 + 标签)
- ``web/static/js/pages/conversational-agent.js`` (Agent 步骤面板实时进展)
- ``features/assistant/workflow_execution.py`` (后端 RUN_STAGE_LABELS_ZH)

A stage added to one copy but not the others renders as a raw status code in
the mirrored views. These tests parse the JS constants the same way other
architecture guards parse client sources and pin all three copies plus the
failure-status sets to ``features.automation.models``.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from features.assistant.workflow_execution import RUN_STAGE_LABELS_ZH
from features.automation import AUTOMATION_TERMINAL_STATUSES


ROOT = Path(__file__).resolve().parents[2]

ATS_STATE_JS = ROOT / "features/automation/ui/state.js"
AGENT_PAGE_JS = ROOT / "web/static/js/pages/conversational-agent.js"

_ARRAY_RE = re.compile(r"const\s+(?:AGENT_)?PIPELINE_STAGES\s*=\s*\[(.*?)\];", re.DOTALL)
_LABELS_RE = re.compile(r"const\s+(?:AGENT_)?STAGE_LABELS_ZH\s*=\s*\{(.*?)\};", re.DOTALL)
_RUN_LABELS_EXTRA_RE = re.compile(
    r"const\s+AGENT_RUN_STATUS_LABELS_ZH\s*=\s*\{\s*\.\.\.AGENT_STAGE_LABELS_ZH,(?P<body>.*?)\};",
    re.DOTALL,
)
_FAILURE_SET_RE = re.compile(r"const\s+(?:AGENT_)?FAILURE_STATUSES\s*=\s*new Set\(\[(.*?)\]\);", re.DOTALL)
_TERMINAL_SET_RE = re.compile(r"const\s+TERMINAL_STATUSES\s*=\s*new Set\(\[(.*?)\]\);", re.DOTALL)
_FAILURE_INDEX_RE = re.compile(r"([a-z_]+):\s*(?:AGENT_)?PIPELINE_STAGES\.indexOf\('([a-z_]+)'\)")


def _parse(source: str, regex: re.Pattern[str], kind: str) -> str:
    match = regex.search(source)
    if not match:
        raise AssertionError(f"mirrored {kind} constant not found; did the declaration move?")
    return match.group(1)


def _stage_list(body: str) -> list[str]:
    return re.findall(r"'([a-z_]+)'", body)


def _label_map(body: str) -> dict[str, str]:
    return dict(re.findall(r"([a-z_]+):\s*'([^']*)'", body))


def _status_set(body: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", body))


def _failure_index_map(source: str) -> dict[str, str]:
    return dict(_FAILURE_INDEX_RE.findall(source))


class PipelineStageMirrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ats_state = ATS_STATE_JS.read_text(encoding="utf-8")
        self.agent_page = AGENT_PAGE_JS.read_text(encoding="utf-8")

    def test_stage_order_matches_between_ats_and_agent(self):
        ats = _stage_list(_parse(self.ats_state, _ARRAY_RE, "PIPELINE_STAGES"))
        agent = _stage_list(_parse(self.agent_page, _ARRAY_RE, "AGENT_PIPELINE_STAGES"))
        self.assertEqual(ats, agent)

    def test_stage_labels_match_between_ats_and_agent(self):
        ats = _label_map(_parse(self.ats_state, _LABELS_RE, "STAGE_LABELS_ZH"))
        agent = _label_map(_parse(self.agent_page, _LABELS_RE, "AGENT_STAGE_LABELS_ZH"))
        self.assertEqual(ats, agent)

    def test_backend_labels_cover_every_stage_with_same_text(self):
        ats = _label_map(_parse(self.ats_state, _LABELS_RE, "STAGE_LABELS_ZH"))
        missing = {stage for stage in ats if RUN_STAGE_LABELS_ZH.get(stage) != ats[stage]}
        self.assertEqual(set(), missing, "backend RUN_STAGE_LABELS_ZH drifted from the JS mirror")

    def test_backend_extra_labels_match_agent_run_status_extension(self):
        body = _parse(self.agent_page, _RUN_LABELS_EXTRA_RE, "AGENT_RUN_STATUS_LABELS_ZH")
        extension = _label_map(body)
        expected = {
            status: label for status, label in RUN_STAGE_LABELS_ZH.items()
            if status not in _label_map(_parse(self.ats_state, _LABELS_RE, "STAGE_LABELS_ZH"))
        }
        self.assertEqual(expected, extension)

    def test_failure_status_sets_match_across_all_copies(self):
        ats = _status_set(_parse(self.ats_state, _FAILURE_SET_RE, "FAILURE_STATUSES"))
        agent = _status_set(_parse(self.agent_page, _FAILURE_SET_RE, "AGENT_FAILURE_STATUSES"))
        backend = set(AUTOMATION_TERMINAL_STATUSES) - {"completed", "cancelled"}
        self.assertEqual(ats, agent)
        self.assertEqual(backend, ats)

    def test_terminal_status_sets_match_between_ats_ui_and_models(self):
        ats = _status_set(_parse(self.ats_state, _TERMINAL_SET_RE, "TERMINAL_STATUSES"))
        self.assertEqual(set(AUTOMATION_TERMINAL_STATUSES), ats)

    def test_failure_stage_index_mapping_matches_between_ats_and_agent(self):
        ats = _failure_index_map(self.ats_state)
        agent = _failure_index_map(self.agent_page)
        self.assertEqual(ats, agent)


if __name__ == "__main__":
    unittest.main()
