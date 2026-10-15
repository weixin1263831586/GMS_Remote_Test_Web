# ADR-0018: Finite, app-scoped Agent debugging sessions

- Status: Accepted
- Date: 2026-10-14
- Related: ADR 0004, ADR 0006, ADR 0010, ADR 0011, ADR 0017

## Problem

Repeated build/install/launch/UI/evidence loops previously required another
single-use shell or APK approval each time. Extending a single-use token's TTL
does not authorize another operation, and unrestricted shell authorization
would broaden the exact-operation boundary in ADR 0006.

## Decision

Add a separate durable debug grant. An authenticated Agent requests it with
an explicit Worker, serial, package, baseline APK, finite duration and typed
operation set. Controller Build-Tools read the real package using aapt2 and
verify signing certificates using apksigner. A human session of the same
resource owner must approve within five minutes. Approval activates a
15–120 minute session (default 60), reserves or borrows that owner's explicit
device reservation and starts the baseline installation if install is granted.

The grant binds the exact Agent actor id, owner, Worker, device, package,
signer certificate set and allowed operations. It carries no Bearer or SSH
secret. Every operation rechecks the grant, enabled/unrevoked Agent identity,
current scopes/ACLs, Worker inventory, claim lease id, generation, source id
and source type. A reservation transferred to a job no longer authorizes
debug operations, even when its lease id/generation and owner stay the same.
Older grants lacking source bindings require a fresh human-approved grant. Grant
possession alone cannot authenticate a caller or allow another Agent under
the same account to execute.

Only typed install/launch/stop/tap/swipe/text/keyevent/database_read operations are supported.
Every new APK is independently verified against the granted package and
signers and its hash is retained in the audit. Android's replace installation
also enforces installed-app signature compatibility. Sessions without install
must match the installed user-0 APK signer set before activation. Each action
and database read verifies the installed base APK using apksigner over the
audited SSH/SFTP path. A baseline install remains busy until post-install
verification succeeds; any failed debug installation revokes the grant.
Interrupted baseline execution also blocks and revokes subsequent actions.
database_read is opt-in, additionally requires devices.database.read, and is
limited to the grant's package and lease, with result-free access auditing.
UI input requires the
approved package's foreground window in user 0; system dialogs fail closed.
App start/stop are user-0 and package-bound. Shell, raw SQL, firmware, app-data
clearing, permission grants, root and system settings receive no new authority.
Existing read-only evidence tools retain their own scopes and ACLs.

One active operation slot per session is acquired atomically using
BEGIN IMMEDIATE. Approval has an atomic pending→approving→active transition,
and deployment start remains single-use. Operations use the audited SSH
boundary; Controller local APK inspection uses argv-based audited process
helpers. Grant and lease checks repeat before subsequent device mutations.
Grant checks do not renew the expiry. Revocation stops subsequent steps, does
not undo an already-dispatched command, and delays claim release until the
running operation finishes. Expiring claims cannot authorize new operations.
A process crash leaves the operation slot occupied rather than replaying an
uncertain mutation; operators can revoke the grant, verify the device and
request a fresh grant after the claim expires.

The human approval page issues no mutation on load or refresh, renders all
evidence using textContent, and exposes approval and revocation buttons.
The operation history keeps the latest 20 records in status responses; input
text is hashed in the audit. Normal APK approvals and ADR 0006 single-use
tokens retain their existing contracts. Debug-grant authority is checked in
the execution service, never inferred from MCP annotations or a client flag.

## Acceptance

Tests cover cross-Agent/owner rejection, revoked credentials, expiry,
revocation during execution, claim fencing, package/signer mismatch, bounded
input compilation, process-safe serialization, repeated tokenless installs,
browser navigation and explicit approval/revocation. Agent deployment uses
gms-agent doctor --json and gms-rt-system-selfcheck --json.

These automated tests use isolated Worker/ADB doubles. Real-device acceptance
must additionally record the Worker/serial, APK hashes and signer digests,
grant/deployment ids and timestamps for same-name/different-signer rejection,
baseline install failure, lease transfer during install, Controller restart,
and temporary review-page network loss. Run these on authorized test devices
on two Workers; record observed terminal states and confirm no duplicate
installation. This document does not claim a completed hardware run.
