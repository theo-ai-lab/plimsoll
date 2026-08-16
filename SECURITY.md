# Security and Privacy

Plimsoll is local-only by default.

## What It Does

- Reads trace and policy files from paths you pass to the CLI.
- Writes JSON, HTML, and optional JUnit/SARIF/Markdown reports to the output directory you pass to the CLI.
- When run inside GitHub Actions, also appends a Markdown summary to the local file named by `$GITHUB_STEP_SUMMARY`; it writes nowhere else and uploads nothing.
- Scans trace text for configured PII and conservative secret-like/high-entropy token patterns.

## What It Does Not Do

- Does not call external APIs.
- Does not start a network service.
- Does not upload traces or reports.
- Does not collect telemetry.
- Does not require API keys.
- Does not read files outside the paths supplied by the user.

## Sensitive Data Notes

Reports may contain excerpts from trace evidence. The built-in sensitive-data findings redact matched examples, but the secret-like/high-entropy token detector is conservative and may produce false positives. Other non-matching trace fields can still appear in metrics or finding evidence. Treat reports as derived from the input traces and review them before sharing.

OpenTelemetry-style traces often carry rich span attributes. Those attributes may include prompts, tool arguments, retrieved context, user data, or provider metadata depending on how the original app was instrumented. Plimsoll does not upload that data, but generated reports can still reflect what was present in the source trace.

Framework-shaped fixture adapters preserve attributes needed for local evidence. If those traces came from real systems, treat adapter outputs, inferred policies, trajectory diffs, JUnit XML, SARIF JSON, Markdown summaries, and any GitHub Step Summary as derived sensitive artifacts.

## Threat Model

| Boundary | Risk | Mitigation |
| --- | --- | --- |
| Input traces | May contain prompts, tool arguments, retrieved context, user data, or provider metadata. | Plimsoll reads only local paths supplied by the user and documents derived artifact risk. |
| Reports | May preserve evidence from the source trace. | Sensitive-data findings redact matched examples, but reports must still be reviewed before sharing. |
| Policy init | May infer a permissive policy from a bad run. | Generated policies are starter files and must be reviewed before use as gates. |
| CI artifacts | JUnit/SARIF can be uploaded by a CI system if configured by the user. | Plimsoll itself does not upload; the example workflow uploads only within the user's CI artifact store. |
| Adapters | Framework-shaped traces can include unexpected attributes. | Adapters normalize a documented subset and preserve attributes locally for evidence. |
| Runtime gate history | The agent being gated could widen its own permissions by claiming prerequisite calls already ran, or by dropping spent calls to reclaim a budget. | The governor owns the record: a `GovernorSession` appends only calls it allowed, a caller-supplied history is cross-checked and never trusted (`session_history_mismatch`), and a proposal with no live session is refused (`session_unknown`). Both fail closed. |
| Runtime gate scope | The record proves what the governor *authorized*, not what the host executed; an authorized call the agent skips is invisible to the gate. | Run the post-hoc `check_trace` audit over the real trace — it sees what actually happened. The gate is the cheap tier, not the only one. |
| Session handles | Handles are sequential process-local names (`session-1`), not bearer secrets, so a co-located caller could name another session. | The stdio transport the `plimsoll-governor` console script serves is one process per client. Deploy one server process per agent; do not multiplex mutually distrusting agents onto one governor. |

## Recommended Use

- Keep real production traces out of the repository unless they are sanitized.
- Prefer synthetic fixtures for demos.
- Add domain-specific `pii_patterns` and `secret_patterns` to the policy.
- Review `report.json`, `report.html`, JUnit XML, SARIF JSON, and the Markdown summary before sending them to anyone else.
- Review inferred policies before using them as pass/fail gates.
