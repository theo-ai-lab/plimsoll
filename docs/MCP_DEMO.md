# The governor over MCP: wiring and a recorded session

`plimsoll-governor` serves the deterministic pre-execution gate over MCP (stdio). An MCP
host asks `propose_tool_call` before executing each tool call and treats a `block` decision
as "do not execute"; `check_trace` runs the full post-hoc audit once the run completes.
Same engine as the CLI: no LLM, no outbound network, no third-party import in the core —
only the server wrapper needs the optional `mcp` SDK.

![Terminal demo: the scripted client drives the real MCP server through allow, deny, and budget-exceeded verdicts](../demo/mcp-governor.gif)

## Wire it into an agent host

Install the optional extra (the core stays zero-dependency). From a clone — the
`pip install "plimsoll[mcp]"` form works once the PyPI publish lands:

```bash
python -m pip install -e '.[mcp]'
```

For MCP hosts configured with a project-level `.mcp.json` (the common convention among
agent CLIs), add:

```json
{
  "mcpServers": {
    "plimsoll-governor": {
      "command": "plimsoll-governor",
      "args": ["--policy", "policy.json"]
    }
  }
}
```

or register it from a host CLI that manages MCP servers, for example:

```bash
claude mcp add plimsoll-governor -- plimsoll-governor --policy policy.json
```

Notes:

- `--policy` is resolved by the server process; if your host launches servers from a
  different working directory, use an absolute path.
- `plimsoll-governor` must be on the host's `PATH` (it lands wherever `pip` installed
  plimsoll). `python -m plimsoll.governor_mcp` is the identical entry point if you would
  rather pin an interpreter.
- Omitting `--policy` yields a permissive empty policy — the server runs, but nothing is
  gated. Every rule the gate enforces comes from your policy file
  ([schema](policy.schema.json)).

The host sees two tools:

| Tool                | Question it answers                                              |
| ------------------- | ---------------------------------------------------------------- |
| `propose_tool_call` | Given the partial trace so far, may this next tool call execute? |
| `check_trace`       | The full deterministic audit over a completed trace.             |

## The recorded session: three verdicts on the wire

[`examples/mcp-governor-session/transcript.jsonl`](../examples/mcp-governor-session/transcript.jsonl)
is a complete JSON-RPC session (both directions, one wire message per line) captured
against the real server launched with
[`examples/mcp-governor-session/policy.json`](../examples/mcp-governor-session/policy.json):
an access-request agent working ticket REQ-4821, "grant contractor-7 read access to
prod-db". The policy allowlists the workflow's tools, requires `manager_review` and
`security_review` before `grant_access`, and caps cumulative tokens at 4000.

**Honest labeling:** the client side is a script
([`scripts/build_mcp_governor_session.py`](../scripts/build_mcp_governor_session.py)), not
a live model — that is what makes the session deterministic and replayable. The server
side, and every verdict below, is the real served governor.

### 1. ALLOW — an ordinary step clears the gate

After a ticket search, the agent proposes reading the request record (transcript seq 6–7):

```json
{"partial_trace": [{"tool": "search_tickets", "...": "..."}],
 "proposed_call": {"tool": "read_record", "input": {"record_id": "REQ-4821"}}}
```

```json
{"decision": "allow", "allowed": true, "proposed_tool": "read_record",
 "summary": "allow: no governor rule blocked 'read_record'", "blocking_findings": []}
```

### 2. DENY — the goal action, refused until its approvals exist

The agent's task *is* to grant access, and `grant_access` is on the allowlist — proposing
it now is the shortest path to completion, not a strawman. But neither required review has
run, so the gate blocks it pre-execution with two critical findings (seq 8–9):

```json
{"decision": "block", "allowed": false, "proposed_tool": "grant_access",
 "summary": "block: 'grant_access' blocked by tool_order, tool_order",
 "blocking_findings": [
   {"rule_id": "tool_order", "severity": "critical",
    "message": "'grant_access' occurred before the required 'manager_review'.", "...": "..."},
   {"rule_id": "tool_order", "severity": "critical",
    "message": "'grant_access' occurred before the required 'security_review'.", "...": "..."}]}
```

The rationale is machine-readable evidence, not prose: each finding names the missing
`before` tool and the observed call sequence, so a host can surface *why* and an agent can
recover (run the reviews, then propose again — the same call is allowed once both precede
it).

### 3. BUDGET-EXCEEDED — cumulative spend caps a call before it runs

Both approvals are done; the agent proposes summarizing the full ticket history for the
approval note, estimated at 2600 input tokens. The trace so far has spent 1560 tokens, so
this call would take the cumulative total to 4160 — over the policy's 4000 cap (seq 10–11):

```json
{"decision": "block", "allowed": false, "proposed_tool": "summarize",
 "summary": "block: 'summarize' blocked by max_tokens",
 "blocking_findings": [
   {"rule_id": "max_tokens", "severity": "medium",
    "message": "'summarize' would exceed the token budget (4160 > 4000).",
    "evidence": {"actual": 4160, "limit": 4000}}]}
```

## Reproduce

```bash
python -m pip install -e '.[mcp]'
python scripts/build_mcp_governor_session.py
```

The builder drives a fresh server subprocess through the scripted session, verifies each
verdict against its ground-truth expectation, captures the session **twice** and
byte-compares the two captures before writing the transcript — determinism is checked on
every regeneration, not assumed. The committed transcript was captured with `mcp` SDK
1.28.1; a different SDK version can change protocol fields (`serverInfo.version`, tool
schemas) but not the verdicts.

The session is also pinned to the code:
[`tests/test_governor_mcp_session.py`](../tests/test_governor_mcp_session.py) replays the
committed transcript on every test run — through the SDK-free `GovernorTools` surface
always, and end-to-end against a real stdio server subprocess when the `mcp` extra is
installed. A governor whose verdicts drift from the transcript fails the suite.

## What the gate does and does not decide

The gate enforces only the rules decidable *before* a call runs: allowlist/forbidden
membership, `must_precede` ordering, cumulative budgets, and repeated-action limits. Rules
that need the call's result or the finished trajectory (output match, PII/secret leakage,
drift) stay deferred to the post-hoc audit — call `check_trace` at the end of the run. See
the [Runtime governor](../README.md#runtime-governor-gate-a-tool-call-before-it-runs)
section of the README for the full boundary.
