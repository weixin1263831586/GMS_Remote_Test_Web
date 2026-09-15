# ADR 0009: Preserve kkagent diagnostic summaries

Status: Accepted

## Decision

Explicit single-issue diagnosis returns the agent's final Chinese Markdown
answer directly. It does not require JSON fields or fixed report chapters.
Routine batch triage retains the schema defined in ADR 0008.

Only a successful final result envelope is accepted. Intermediate messages,
reasoning and tool output are never promoted to a final answer. Analysis and
schema/evidence repair have no step, elapsed-time or token budget. Legacy budget
configuration is normalized to zero and does not truncate investigation. Evidence
gathering continues until the agent returns an answer, fails, or the user stops it.
Cancellation still terminates the process tree. Live durable jobs are never
classified as interrupted based on their age; lease renewal remains independent
of analysis duration. Failures to finish remain failures.

The runtime stores the unchanged answer in `detailed_report`, marks the result
format as `kkagent_markdown`, and preserves execution/evidence audit separately.
Unverified evidence completeness remains marked for human review and does not
block displaying the answer. CLI calls recorded as Bash are not automatically
treated as successful MCP evidence. No confidence or root-cause label is invented.

The UI displays the report as one Markdown document, with runtime records
collapsed. Native summaries do not offer structured case saving until a separate
explicit extraction contract exists. Existing structured results remain readable.
