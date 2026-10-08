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

A stable pool of 256 file locks, selected by the digest of the owner and key,
serializes creation across processes. Contention (including stripe collisions)
returns `STATE_CONFLICT` with an instruction to retry the same key. No SQLite
write lock is held across separate device claim and command transactions.
Exiting a process releases its lock automatically. Lock files are never unlinked
while processes can use them, so two processes cannot acquire different inodes
for the same stripe.

The job and its submission receipt commit in the same SQLite transaction. A
receipt survives deleting its job, so an old retry cannot recreate deleted work.
Replays check the current Agent Worker and device ACLs before returning the
original job. They bypass scheduling and admission because those checks govern
new work, and the existing task may now occupy its Worker.

If a process exits between committing the job and queuing its command, replay
resumes dispatch only while the initial attempt is still `assigned` and its
physical device claims and lease generations remain valid. Losing a claim
fails the job and returns `STATE_CONFLICT`. Command insertion and the Job/Attempt
transition to `dispatching` commit together under a SQLite write lock. Replays
repair legacy commands committed without this transition after checking fencing;
they never rewind a running or terminal Job. Command delivery deduplicates by
attempt ID. Terminal jobs are never restarted by replay.

Every Cluster Job binds at least one device. Empty device lists mean automatic
selection of `device_count` devices, even with an explicit Worker; explicit
device lists determine the count. Worker admission, suite identity/path, explicit
device identity and transport requirements are checked together. The selected
devices are persisted with the original Job. Device-free queries or preparation
use Worker commands rather than Cluster Jobs.

The browser blocks concurrent submission and saves the unconfirmed body and key
in account-scoped `sessionStorage` before posting. Refresh restores the draft
without submitting it. Explicit retries reuse the key even when the Worker is
now occupied: the server checks the receipt before scheduling. Only a confirmed
creation/replay response clears that account's pending draft. Workspace Worker
and device selection are taken from that response.

## Consequences

Clients without a key retain their existing behavior. Receipts remain under the
Cluster database directory and are retained indefinitely, including tombstones
for deleted Jobs, because clients may retry old keys. New submissions create at
most 256 lock files per database. Locks use the existing POSIX server deployment
model. Real device claims and fencing remain governed by ADR 0011.

Upgrading from per-key lock files requires stopping all Controller/Web processes
before restarting them with the striped implementation; mixing lock schemes
would lose submission mutual exclusion. Legacy digest-named files can be removed
offline with all these processes stopped. Receipt records must be preserved;
neither live lock deletion nor receipt TTL eviction is part of normal cleanup.
