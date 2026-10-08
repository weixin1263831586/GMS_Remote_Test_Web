# ADR 0016: Account-scoped Cluster job submission receipts

Status: Accepted

## Context

A browser can lose the response after a task and its device leases have been
created. Repeating the request must not schedule a second task. Concurrent Web
processes share the Cluster database and the physical device claim registry.

## Decision

`POST /api/cluster/jobs` accepts an optional `Idempotency-Key` header (1–128
ASCII letters, digits, or `._:-`). The key belongs to the authenticated resource
owner, as defined in ADR 0010. A canonical request digest excludes the ignored
client-provided owner field. Reusing a key with different input returns
`STATE_CONFLICT`.

A file lock identified by the digest of the owner and key serializes creation
across processes. Contention returns `STATE_CONFLICT` with an instruction to
retry the same key. No SQLite write lock is held across separate device claim
and command transactions. Exiting a process releases its lock automatically.

The job and its submission receipt commit in the same SQLite transaction. A
receipt survives deleting its job, so an old retry cannot recreate deleted work.
Replays check the current Agent Worker and device ACLs before returning the
original job. They bypass scheduling and admission because those checks govern
new work, and the existing task may now occupy its Worker.

If a process exits between committing the job and queuing its command, replay
resumes dispatch only while the initial attempt is still `assigned` and its
physical device claims and lease generations remain valid. Losing a claim
fails the job and returns `STATE_CONFLICT`. Command
delivery deduplicates by attempt ID. Terminal jobs are never restarted by replay.

The browser blocks concurrent submission and retains its key for an unchanged
draft until it receives a successful creation response. Workspace Worker and
device selection are taken from that response.

## Consequences

Clients without a key retain their existing behavior. Receipts and lock files
remain under the Cluster database directory; maintenance must retain receipts
for as long as old requests may be retried. Locks use the existing POSIX server
deployment model. Real device claims and fencing remain governed by ADR 0011.
