# The governor over MCP: wiring and a recorded session

`plimsoll-governor` serves the deterministic pre-execution gate over MCP (stdio). A host
opens a gate session, asks `propose_tool_call` before executing each tool call, and treats
a `block` decision as "do not execute"; `check_trace` runs the full post-hoc audit once the
run completes. Same engine as the CLI: no LLM, no outbound network, no third-party import
in the core — only the server wrapper needs the optional `mcp` SDK.

The server keeps the record of what it allowed. That is the point of the session: the
history every ordering and budget verdict is computed from must not be written by the agent
the gate is constraining.

![Terminal demo: the scripted client drives the real MCP server through allow, deny, forged-history, and budget-exceeded verdicts](../demo/mcp-governor.gif)

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

or, for a host CLI that manages MCP servers with an `mcp add` command, register it as:

```bash
<host-cli> mcp add plimsoll-governor -- plimsoll-governor --policy policy.json
```

Notes:

- `--policy` is resolved by the server process; if your host launches servers from a
  different working directory, use an absolute path.
- `plimsoll-governor` must be on the host's `PATH` (it lands wherever `pip` installed
  plimsoll). `python -m plimsoll.governor_mcp` is the identical entry point if you would
  rather pin an interpreter.
- Omitting `--policy` yields the empty policy. That is not the same as gating nothing:
  `max_repeated_action_count` defaults to `1`, so the second identical tool call is still
  blocked by `repeated_action`. Every other rule the gate enforces comes from your policy
  file ([schema](policy.schema.json)).

The host sees three tools:

| Tool                | Question it answers                                                          |
| ------------------- | ---------------------------------------------------------------------------- |
| `open_session`      | Start a gate session; returns the handle and the served policy's SHA-256.     |
| `propose_tool_call` | Given what this session has allowed so far, may this next tool call execute?  |
| `check_trace`       | The full deterministic audit over a completed trace.                          |

`propose_tool_call(session_id, proposed_call, partial_trace=None)` takes the handle
`open_session` returned. A proposal without a live session is refused (`session_unknown`)
rather than judged against an empty history — an unsessioned gate would treat every call as
the first one, which is a free budget and a bypassed ordering rule. `partial_trace` is
optional and is *never* used as the record: if a host sends its own view of the history, it
is compared with the server's and any disagreement blocks the call
(`session_history_mismatch`).

Every decision echoes `session_id` and `policy_digest` — the SHA-256 of the effective
policy text — so a recorded verdict binds to the exact policy that produced it and cannot
be presented as a decision made under different rules.

## The recorded session: eight verdicts on the wire

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

### 0. OPEN — the server takes custody of the history

The first `tools/call` opens the session (seq 6–7). Nothing is gated yet; what matters is
who will be keeping score:

```json
{"session_id": "session-1",
 "policy_digest": "sha256:617c494779e26a428f9062328dcf68e4cfd7dae92972925cd3992b80f5c45fcb",
 "authorized_tools": []}
```

### 1. ALLOW — ordinary steps clear the gate

The agent searches for the ticket, then proposes reading the request record (seq 8–11):

```json
{"session_id": "session-1",
 "proposed_call": {"tool": "read_record", "input": {"record_id": "REQ-4821"}, "...": "..."}}
```

```json
{"decision": "allow", "allowed": true, "proposed_tool": "read_record",
 "policy_digest": "sha256:617c4947...", "session_id": "session-1",
 "summary": "allow: no governor rule blocked 'read_record'", "blocking_findings": []}
```

### 2. DENY — the goal action, refused until its approvals exist

The agent's task *is* to grant access, and `grant_access` is on the allowlist — proposing
it now is the shortest path to completion, not a strawman. But neither required review has
run, so the gate blocks it pre-execution with two critical findings (seq 12–13):

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

### 3. FORGED HISTORY — the same call, retried with a past that did not happen

The obvious way around an ordering rule is to claim the prerequisites already ran. The
agent retries the identical `grant_access` and this time supplies a `partial_trace`
containing `manager_review` and `security_review`. The server never ran either, and says so
(seq 14–15):

```json
{"decision": "block", "allowed": false, "proposed_tool": "grant_access",
 "summary": "block: 'grant_access' blocked by session_history_mismatch",
 "blocking_findings": [
   {"rule_id": "session_history_mismatch", "severity": "critical",
    "message": "'grant_access' is blocked: the supplied history is not what this session authorized.",
    "evidence": {"session_id": "session-1",
                 "authorized_tools": ["search_tickets", "read_record"],
                 "supplied_tools": ["search_tickets", "read_record", "manager_review", "security_review"]}}]}
```

This is the whole reason the gate keeps its own record. A gate that computes ordering from
a history the gated agent supplies is not a control; it is a formality the agent can
satisfy by asserting.

### 4. ALLOW — the approvals actually happen, and the grant goes through

`manager_review` and `security_review` are proposed, gated, allowed, and recorded; the same
`grant_access` the gate refused twice is now allowed (seq 16–21). The rule is an ordering
constraint, not a refusal to ever grant — a gate that only ever says no would be trivial to
build and useless to ship.

### 5. BUDGET-EXCEEDED — cumulative spend caps a call before it runs

Both approvals are done; the agent proposes summarizing the full ticket history for the
approval note, estimated at 2600 input tokens. The calls the *server* authorized have spent
1560 tokens, so this call would take the cumulative total to 4160 — over the policy's 4000
cap (seq 22–23):

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
every regeneration, not assumed. (Session handles are sequential — `session-1` — precisely
so a served session stays reproducible.) The committed transcript was captured with `mcp`
SDK 1.29.0; a different SDK version can change protocol fields (`serverInfo.version`, tool
schemas) but not the verdicts.

The session is also pinned to the code:
[`tests/test_governor_mcp_session.py`](../tests/test_governor_mcp_session.py) replays the
committed transcript on every test run — through the SDK-free `make_handlers` surface
always, and end-to-end against a real stdio server subprocess when the `mcp` extra is
installed. A governor whose verdicts drift from the transcript fails the suite.

## What the gate does and does not decide

The gate enforces only the rules decidable *before* a call runs: allowlist/forbidden
membership, `must_precede` ordering, cumulative budgets, and repeated-action limits. Rules
that need the call's result or the finished trajectory (output match, PII/secret leakage,
drift) stay deferred to the post-hoc audit — call `check_trace` at the end of the run. See
the [Runtime governor](../README.md#runtime-governor-gate-a-tool-call-before-it-runs)
section of the README for the full boundary.

One more limit worth stating plainly: the session's record is the list of calls the
governor **authorized**. A gate cannot observe whether the host actually executed one, so
an agent that is authorized to run `manager_review` and then skips it has told the gate
something it cannot check. That is what the post-hoc `check_trace` audit over the real
trace is for — the two tiers are complementary, and neither is a substitute for the other.
