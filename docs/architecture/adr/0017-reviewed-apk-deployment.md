# ADR-0017: One approval for APK transmission and installation

Status: Accepted.

APK deployment uses a typed request rather than arbitrary device shell.
An Agent uploads one APK with an exact Worker, serial and replacement mode.
The Controller retains an immutable SHA256 and an owner-scoped request with
a five-minute approval deadline. Uploading creates no device side effects.

The human resource owner reviews the filename, SHA256, Worker, serial and
installation mode on the returned approval URL. The human-only POST issues
and consumes an ADR 0006 approval in memory, bound to the complete request,
and atomically transitions the pending request to running. No raw token is
returned to the browser, stored in the request, or copied through the Agent.
Replays and concurrent approvals cannot execute a second installation.

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

DB diagnosis is a separate read-only surface: validated package/database/
table/column identifiers and bounded pagination compile to a SELECT passed
to `sqlite3 -readonly` under `run-as`. For user-0 system apps that are not
debuggable, it may reuse the device shell's existing read permissions on the
fixed package database path. It accepts no raw SQL, shell, or root
escape. Missing sqlite3 and application access failures remain actionable
semantic errors; the interface never enables root or grants new app access.

Automatic package updates remain independent of device authorization. The
per-user systemd timer invokes the verified existing update lifecycle with
one exact profile and runs doctor/selfcheck afterwards. It refreshes all
registered clients together and never terminates running Agent sessions.
