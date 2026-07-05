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
(`manager_review`, `security_review`) has run yet, so the gate denies it with two critical
`tool_order` findings. Call 3 shows the cumulative token budget blocking an oversized
`summarize` even after both approvals.

## How this was produced

`transcript.jsonl` was captured by a **scripted, deterministic client**
([`scripts/build_mcp_governor_session.py`](../../scripts/build_mcp_governor_session.py))
driving the **real server** (`python -m plimsoll.governor_mcp --policy policy.json`, the
same entry point the `plimsoll-governor` console script runs) over newline-delimited
JSON-RPC 2.0 on stdio. It is **not a live-model session** — no LLM chose these calls — but
every verdict in it was produced by the served governor. The builder captures the session
twice and byte-compares before writing, so the determinism claim is verified on each run.

Each transcript line is one wire message, tagged with its direction:

```json
{"seq": 8, "direction": "client->server", "message": {"jsonrpc": "2.0", "id": 3, "method": "tools/call", ...}}
```

The recorded `serverInfo.version` is the version of the optional `mcp` SDK the session was
captured with (1.28.1); re-capturing with a different SDK version can change protocol
fields but not the verdicts.

## Reproduce

```bash
pip install "plimsoll[mcp]"
python scripts/build_mcp_governor_session.py
```

`tests/test_governor_mcp_session.py` replays the committed transcript on every test run —
SDK-free through the same `GovernorTools` surface the server wraps, and end-to-end against
a fresh real server subprocess when the `mcp` extra is installed — so a governor whose
verdicts drift from this transcript fails the suite.

See [`docs/MCP_DEMO.md`](../../docs/MCP_DEMO.md) for the walkthrough and the host wiring
(`.mcp.json`) to run the same gate inside an MCP-capable agent host.

## Files

- `policy.json` — the access-request policy the server was launched with.
- `transcript.jsonl` — the full session, both directions, in wire order.
