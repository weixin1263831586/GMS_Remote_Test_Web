# ADR 0031: Model-led conversation over the project tool registry

Status: Accepted

## Context

The conversation endpoint used keyword routing. Short requests such as flashing
firmware reported missing arguments without discovering available resources.
A kkagent bridge existed but was not called by the conversation endpoint.

## Decision

Use kkagent as the default planner for natural-language turns. It receives the
project tool index and a bounded JSON protocol for discovery, read requests,
questions, answers and ATS workflow proposals. Tool schemas and actual query
results drive questions; the browser renders the same field schema for devices,
firmware, reports and other resources.

The model process runs in an empty temporary workspace, with a private temporary
copy of operator model configuration. Native tools are denied, inherited MCP
servers and hooks are removed, and auto-approval is disabled. The host
configuration is never edited. The Controller executes registered tools using
the authenticated request and existing route authorization, ownership and
Worker targeting. Dangerous or confirmation-required tools become pending
plans; other tools retain their existing confirmation semantics.
Flashing, AOA and test workflows continue through ATS and Cluster (ADR 0004).

Calls have bounded output, rounds and time. Cancellation stops further tool
dispatch. Rules apply only when explicitly disabled or the binary is absent.
Runtime failures stay visible as model errors, without replaying the user
instruction through another router. Each inference has a 120-second deadline,
including policy preparation; a planning turn has a 300-second deadline and
at most one automatic inference retry on timeout. Streaming session IDs are
retained even if inference times out, so retries resume the native session.
Failed turns offer an explicit retry with a private, owner/session/workspace-bound
checkpoint, expiring after 15 minutes (at most 128 checkpoints per process).
Resource observations and operation receipts survive retry; identical routine
writes are not dispatched again, including writes whose receipt was interrupted.
Confirmation-required operations retain the existing single-submit plan boundary.
Waiting progress and engine errors are shown in the UI throughout planning.
An elevation-required route guard pauses planning with a human authorization
action. The browser uses the existing administrator verification modal and
resumes the same checkpoint; it never sends administrator credentials to the
planner. Resumption retries the refused tool against the current authenticated
request and the original route dependency before querying any protected data.
Device catalog availability includes protocol/status and stale observations;
an unlocked unresponsive device is not advertised as an idle target.
Model turns run as background tasks so HTTP returns a session immediately;
the browser polls progress and resource questions, and cancellation terminates
the active model process before any further tool dispatch.
The UI distinguishes the actual conversation engine from report-analysis models.
Blocking preflight results keep confirmation visible but disabled, with reasons.

## Configuration and limits

`GMS_ASSISTANT_KKAGENT_CONFIG` selects the operator's kkagent model configuration;
the default is `~/.kkagent/config.toml`. `GMS_ASSISTANT_KKAGENT_ENABLED=0` selects
deterministic routing, also used by isolated tests. The registry defines current
coverage: unsupported file/interactive operations remain explicit and cannot
be made executable by model text. Missing permissions are reported, not bypassed.
