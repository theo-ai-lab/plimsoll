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

## Recommended Use

- Keep real production traces out of the repository unless they are sanitized.
- Prefer synthetic fixtures for demos.
- Add domain-specific `pii_patterns` and `secret_patterns` to the policy.
- Review `report.json`, `report.html`, JUnit XML, SARIF JSON, and the Markdown summary before sending them to anyone else.
- Review inferred policies before using them as pass/fail gates.
