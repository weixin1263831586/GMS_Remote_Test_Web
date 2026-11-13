"""Synthetic claim-review receipts for headless analysis tests."""

import json

from features.redmine.kkagent.claim_review import report_digest, report_units
from features.redmine.kkagent.trace import KkAgentTrace


def review_payload(report, *, assessment="inference", category="observation", refs=None):
    return {
        "report_sha256": report_digest(report), "status": "supported", "findings": [],
        "units": [{"unit_id": unit["unit_id"], "category": category, "assessment": assessment,
                   "evidence_ids": refs if refs is not None else ["EV-005"],
                   "causal_link": "correlation", "reason": "区分已观察到的失败与未确认的起因。"}
                  for unit in report_units(report)],
    }


def review_trace(report, session="same", **kwargs):
    return KkAgentTrace(session_id=session, exit_code=0, final_event={
        "type": "result", "subtype": "success", "exit_code": 0, "session_id": session,
        "message": json.dumps(review_payload(report, **kwargs), ensure_ascii=False),
    })

