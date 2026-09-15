# ADR 0008: Daily Brief separates triage from diagnosis

Status: Accepted

## Context

Routine pending-reply issues previously required historical searches, technical
root-cause analysis and a five-section report. This mixed evidence completeness
with business priority and made the aggregate morning report difficult to act on.

## Decision

- Batch runs use `analysis_mode=triage`: current change, actor, blocker and next
  action. They read the issue, journals and relevant attachments. Historical and
  source searches are not prerequisites. Technical root causes stay unknown.
- A user-triggered single-issue reanalysis performs diagnosis. The durable job's
  exact run ID is carried through execution; the worker never substitutes a newer
  run on the same date.
- `daily_brief_result.IssueResult` defines both runtime validation and the JSON
  Schema embedded in prompts. Historical persisted results remain readable by
  the tolerant UI; new model results must pass the current contract.
- Confidence is an explicit numeric model estimate. The runtime does not invent
  a number from a root-cause label. Evidence completion is shown separately and
  derives from the persisted gate, not from the model's assertions.
- Priority is deterministic and bounded to 0–100. Attachments do not affect
  priority; age contributes at most five additional points after seven days.
- The aggregate report omits full diagnostic reports. The UI shows summaries and
  next actions; individual results remain available in the detail modal.

## Consequences

The current data source remains pending-reply and stale issues; this is not a
complete activity digest. Field references and tool completion do not yet prove
that every natural-language claim is supported by the cited evidence. No numeric
evidence-confidence score is presented as a substitute for that verification.

Existing completed runs are not rewritten. A new run or explicit regeneration
uses the new prompt contract. This preserves audit history and frozen snapshots.
