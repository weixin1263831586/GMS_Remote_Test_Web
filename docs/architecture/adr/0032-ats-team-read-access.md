# ADR 0032: Team visibility of ATS run records

Status: Accepted

## Context

Using the same ownership check for reading and modifying ATS runs hides device
occupation and completed workflows created by colleagues. A user can encounter
a reservation without being able to find the run that created it.

## Decision

Authenticated human Web users can read all ATS run records, filtered history,
dashboard counts, details, events, timeline, trace and progress. Shared read
responses redact credentials, including credentials nested in stored JSON.
History retains its existing cursor pagination and user-selected filters.

Cancellation and retry continue to require a human creator account or an
administrator, using resource-owner account identity (ADR 0010). A visible
foreign run returns `FORBIDDEN` when another ordinary user attempts to modify
it. The API returns a per-run `permissions.can_manage` flag; the UI shows the
creator and a read-only label instead of mutation controls for other users.
Client flags are display hints; server authorization remains mandatory.

Agent Service Tokens, machine capability principals and Worker accounts keep
their existing account-scoped read access and cannot mutate the orchestration
surface (ADR 0012). This decision covers ATS run records; plan configuration,
report/artifact APIs and other feature permissions keep their own policies.
