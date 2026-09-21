# ADR 0014: External knowledge federation (android-internals-wiki, background only)

Status: Accepted

## Context

Single-issue diagnosis currently ranks evidence produced inside the platform:
device snapshots, test logs, source evidence collected through
`gms-rt-sdk-*`, and confirmed internal cases from the Redmine knowledge
base. Operators and kkagent diagnostics keep asking a different kind of
question — "what is LMKD supposed to do after a kill?", "how does
Choreographer annotate FrameTimeline?" — that is about Android system
mechanisms, not about this product's devices or cases. The public
`Gracker/android-internals-wiki` repository answers exactly that class of
question, but folding it into the evidence pipeline would let textbook
mechanism text masquerade as verified root-cause evidence.

The personal knowledge base (`features/knowledge`, KnowledgeService /
knowledge.sqlite3) is the wrong home for this content: it stores internal,
case-linked notes, while the wiki is third-party, versioned by upstream git,
and licensed CC BY-NC-SA 4.0 (non-commercial, share-alike). Importing wiki
pages into it would blur both ownership and license boundaries, and the
content must never be baked into `plugins/gms-remote-test/`, the agent
package, or customer deliverables.

## Decision

External knowledge is a **read-only federation of background sources**,
separate from the personal knowledge base:

- `features/knowledge/external/` defines the
  `ExternalKnowledgeProvider` protocol, the `KnowledgeHit` result shape and
  `FederatedKnowledgeService`. The first provider is
  `AndroidInternalsProvider`, which reads an admin-cloned checkout of
  `android-internals-wiki` and serves FTS5 search from a companion index
  (`<data_root>/knowledge/external/android_internals.sqlite3`), never from
  the personal knowledge store.
- **Background only.** Every hit carries `evidence_level: "background"`
  and is consumed for mechanism explanation and as input to verification
  plans. It is never a verified root cause, never enters the evidence
  ledger (`features/redmine/kkagent/evidence_gate.py` is unchanged), and
  never substitutes for `gms-rt-sdk-*` source forensics.
- **Evidence priority ladder** (used by diagnosis ranking and prompt
  contracts):
  - P0 — actual device evidence (snapshot, dumpsys, bound-device state);
  - P1 — test logs / CTS·GTS·VTS·STS sources;
  - P2 — AOSP / current product source (via `gms-rt-sdk-*`);
  - P3 — confirmed internal historical cases (mature cases / knowledge base);
  - P4 — Android Internals Wiki (background, this ADR);
  - P5 — AI reasoning (hypothesis, never evidence).
  Wiki hits rank below every internal source and above raw AI hypothesis.
- **Mandatory provenance.** A hit without provenance is not renderable:
  `source`, `source_revision` (`git rev-parse HEAD` of the clone),
  `applicable_versions`, `confidence`, `last_verified`,
  `last_verified_against`, `license` (CC BY-NC-SA 4.0) and
  `evidence_level` travel with every result; Web and assistant rendering
  must show the source line and the fixed footnote "背景知识：解释系统机制，
  尚未通过设备/源码证据验证，不得作为已证实根因".
- **Fail-closed configuration.** Providers are enabled only through
  admin-deployed runtime config
  (`[external_knowledge.providers.<name>]`, `enabled` + `repo_root`).
  Clients cannot pass paths. A missing section, an invalid `repo_root`, or
  a checkout without `.git` disables the provider (status reported with the
  reason) and search returns an empty result for that source — never an
  error for the other sources.
- **Failure isolation.** `FederatedKnowledgeService` fans out per source;
  one provider raising does not affect the others. Callers go through the
  `features.knowledge` public surface only
  (`federated_search` / `federated_status` / `federated_reindex`), never
  into `features.knowledge.external` internals.
- **Access control.** Web-session routes live under
  `/api/knowledge/external/*` behind the login session (reindex is admin
  only); the agent route `/api/knowledge/android-internals/*` requires the
  new `knowledge.read` agent scope (registered beside `sdk.read`).
- **triage never queries the wiki.** The morning-brief batch triage prompt
  and tool surface exclude `gms_rt_knowledge_search`; only the on-demand
  diagnostic deep analysis may use it. This keeps batch triage cheap and
  prevents noise inflation from background hits.
- **License boundary.** Wiki content stays in the admin-side clone and its
  companion index; it is displayed only inside the platform with license
  attribution and is never packaged into `plugins/gms-remote-test/`, the
  agent package, or anything shipped to customers.

## Consequences

Diagnosis gains a fifth recall lane (`system_background_results`, capped at
6) alongside the existing four; a provider failure degrades to an empty
lane and a warning log, so the diagnosis response stays 200. Because the
lane is background, the AI prompt contract changes only by presenting the
lane as explanatory context — verified root-cause statements still require
P0-P3 evidence through the unchanged evidence gate.

Deployment is a local git clone plus one config stanza plus a reindex call;
there is no network dependency at query time and no TUF/packaging pipeline
(explicitly out of scope). Index rebuilds are incremental (content hash)
and process-safe (`BEGIN IMMEDIATE` + `PRAGMA user_version`), so Web,
CLI and Worker processes can trigger them concurrently. Upstream content
updates arrive as ordinary `git pull` plus reindex, and every hit remains
traceable to the exact clone revision it was served from.
