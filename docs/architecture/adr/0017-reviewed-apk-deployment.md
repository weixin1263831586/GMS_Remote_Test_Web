# ADR-0017: One approval for APK transmission and installation

Status: Accepted.

APK deployment uses a typed request rather than arbitrary device shell.
An Agent uploads one APK with an exact Worker, serial and replacement mode.
The Controller retains an immutable SHA256 and an owner-scoped request with
a five-minute approval deadline. Uploading creates no device side effects.

The Controller verifies package, version and signer certificates using aapt2
and apksigner and reads the installed user-0 base APK over audited SSH for
signature comparison. The human resource owner reviews this identity,
filename, SHA256, Worker, serial and installation mode on the approval URL. The human-only POST issues
and consumes an ADR 0006 approval in memory, bound to the complete request,
and atomically transitions the pending request to queued. No raw token is
returned to the browser, stored in the request, or copied through the Agent.
Replays and concurrent approvals cannot execute a second installation.

Review URLs serve a public, metadata-free HTML login shell so a signed-out
browser can reach the approval flow. The JSON status and approval endpoints
retain authentication, scopes and owner checks. An anonymous, unauthorized
or wrong-account response opens the shared ModalManager login dialog with
the gms administrator account prefilled. Human-entered credentials go only
to the existing same-origin /api/auth/login endpoint; login success resumes
status reads at the original URL. Approval still requires a separate click.

Reading an owned deployment or debug-grant JSON status uses `devices.use_leased`
for authenticated human sessions, matching their approval permission. Agent
readers still require `devices.read`. This does not add global inventory
permissions to human roles or relax owner, Worker or device checks.

The Controller validates device inventory and scopes/ACLs, takes an exclusive
claim (borrowing only the owner's reservation), and executes SFTP and ADB
through the audited SSH boundary in ADR 0004. Host and device copies must
match the reviewed SHA256 before `pm install`. The claim covers transmission,
verification, installation and temporary-file cleanup. A running test's claim
cannot be borrowed. Failed or expired requests require a new reviewed request.
Execution also binds each claim's source id and type: reservation transfers
to a test job preserve lease id/generation, but must block subsequent APK
transmission or installation. Borrowed reservations are never released by
deployment cleanup.

An atomic queued→running transition selects one executor. A heartbeat renews
the persisted execution lease every 20 seconds with a 120-second deadline;
status reads cannot expire work whose lease is still live. Lost queued or
running work becomes interrupted. On the next authorized status read the
Controller checks the installed package, signer set and base APK SHA256.
Only an exact artifact match can resolve interruption to completed. All other
outcomes remain interrupted and require explicit review and a new request.
BackgroundTasks dispatch is not a replay queue: restarts never repeat an
uncertain pm install. SSH credentials remain in memory throughout.

DB diagnosis is a separate read-only surface: validated package/database/
table/column identifiers and bounded pagination compile to a SELECT passed
to `sqlite3 -readonly` under `run-as`. For user-0 system apps that are not
debuggable, it may reuse the device shell's existing read permissions on the
fixed package database path. It accepts no raw SQL, shell, or root
escape. Missing sqlite3 and application access failures remain actionable
semantic errors; the interface never enables root or grants new app access.
Database reads require the independent devices.database.read scope plus a
human-approved ADR 0018 grant explicitly allowing database_read. The Agent,
Worker, device, package and installed signer must match and the bound claim
must still be valid. Access history records actor, time, package, database
and table; query results and device output are excluded from this audit.

Automatic package updates remain independent of device authorization. The
per-user systemd timer invokes the verified existing update lifecycle with
one exact profile and runs doctor/selfcheck afterwards. It refreshes all
registered clients together and never terminates running Agent sessions.
