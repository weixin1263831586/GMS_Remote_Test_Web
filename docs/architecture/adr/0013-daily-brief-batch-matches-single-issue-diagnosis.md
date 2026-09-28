# ADR 0013: Daily Brief batch analysis matches single-issue diagnosis

Status: Accepted

## Context

ADR 0008 split the daily brief into a lightweight batch triage (latest change,
actor, blocker, next action; no history/source/device evidence, no root cause)
and an on-demand single-issue diagnosis. Operators expect the scheduled morning
brief to carry the same analytical depth as the "Redmine 单号分析" workflow:
both surfaces render the same result cards, the same evidence gate display and
the same detail modal. The difference in depth was an artifact of an early
cost-control decision, not a product requirement; the only intended difference
between the two surfaces is the trigger source (scheduled nightly run vs.
user-initiated run).

Batch triage also produced a structurally weaker artifact (IssueResult JSON
with `root_cause_type=unknown`) that the UI had to render beside full
diagnostic results, and the pending-reply issues the brief targets are exactly
the ones where an actionable root cause matters most.

## Decision

- The batch analyze phase (`DailyBriefService._analyze_phase`) no longer forces
  `analysis_mode=triage`. Every issue is analyzed with the same diagnostic
  pipeline as single-issue analysis (`reanalyze_issue` → `_analyze_one` →
  kkagent native summary), including history search, source-evidence gating for
  test failures, and the deterministic Controller evidence preflight (Redmine
  baseline plus device snapshot when a device is bound).
- The triage prompt branch and its IssueResult contract remain in the codebase
  only to render historically persisted results; new analyses never take that
  branch.
- The prompt contract version advances to `redmine_daily_triage_v18`.
- The aggregate morning report keeps its short per-issue summary rendering for
  native (`kkagent_markdown`) results; full reports stay in the per-issue
  detail view.

## Consequences

A nightly run analyzes more deeply per issue: wall time, token usage and the
per-owner `max_parallel_issues` budget now apply to every pending item. An
operator who only wants the short action list can still stop the run from the
UI; per-issue failures stay isolated and the run converges to `partial`/`failed`
as before.

Historically persisted triage results keep rendering through the legacy JSON
path. Existing completed runs are not rewritten; new runs (including retries
that refreeze a snapshot) use the diagnostic contract.

The skill reference is updated to `redmine_daily_triage_v12` to describe the
unified workflow; the packaging test pins that version string.

## Addendum (2026-09-28): prompt contract v19

After the first nightly runs on this ADR, the report contract advanced to
`redmine_daily_triage_v19`:

- The report no longer contains a "处理时间线" (processing timeline) section;
  it duplicated Redmine's own journal stream.
- The problem-overview table splits test-class issues from non-test issues,
  and pins reporter = issue author, issue id format `#<issue_id>`, forbids
  internal evidence/user ids, and normalizes all times to Beijing time
  (UTC+8).

`redmine_daily_triage_v12` remains the independent skill-workflow contract
axis pinned by the packaging test; v19 only advances the prompt/report
contract axis.
