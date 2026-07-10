# MCP governor session

A recorded JSON-RPC session against the real `plimsoll-governor` stdio MCP server, showing
the pre-execution gate refusing tool calls before they run. Three outcomes, one
access-request episode:

| Call | Proposed tool  | Verdict | Rule(s)                  |
| ---- | -------------- | ------- | ------------------------ |
| 1    | `read_record`  | allow   | —                        |
| 2    | `grant_access` | block   | `tool_order` (×2)        |
| 3    | `summarize`    | block   | `max_tokens`             |

Call 2 is the interesting one: `grant_access` is *on the policy allowlist* and is the
task's goal action — the shortest path to completion — but neither required approval
(`manager_review`, `security_review`) has run yet, so the gate denies it pre-execution.

## Files

- `policy.json` — the access-request policy the server was launched with.
- `transcript.jsonl` — the full session, both directions, in wire order; each line is one
  wire message tagged with its direction:

```json
{"seq": 8, "direction": "client->server", "message": {"jsonrpc": "2.0", "id": 3, "method": "tools/call", ...}}
```

## Walkthrough, reproduction, drift pinning

[`docs/MCP_DEMO.md`](../../docs/MCP_DEMO.md) is the single full walkthrough: how the
transcript was captured (a scripted deterministic client driving the real server — not a
live-model session), how to regenerate it from a clone, and the `.mcp.json` host wiring.
`tests/test_governor_mcp_session.py` replays the committed transcript on every test run,
so a governor whose verdicts drift from this transcript fails the suite.
