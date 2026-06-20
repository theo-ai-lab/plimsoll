#!/usr/bin/env python3
"""Live cross-repo gate demo: the Plimsoll Governor firing inside a running agent loop.

Plimsoll's CLI is a *post-hoc* trace checker. The :class:`~plimsoll.governor.Governor`
reuses the very same deterministic rule engine as a *pre-execution* gate: before an agent
runs a tool, ``propose_tool_call`` is asked "given everything that has run so far, is this
next call safe?". This script wires that gate into a small simulated agent loop and reports
how many unsafe calls it blocked — the deterministic-safeguard story, firing live.

It is a *drop-in* gate any repo can adopt: build a :class:`Governor` from a policy
(in-code here; in a real repo use ``Governor.from_policy_file("policy.json")``), then call
the gate before every tool execution. No LLM, no network, no third-party package — the
action stream is scripted so the run is fully deterministic and the headline count is real.

The scripted stream is *labelled*: each proposed call records whether it is unsafe by
design and why. The loop cross-checks the gate's live decision against that label, so the
"blocked N of M unsafe calls" headline is verified, not asserted. A mismatch (an unsafe
call slipping through, or a safe call wrongly blocked) fails the demo with a non-zero exit.

Run it::

    python examples/governor_loop_demo.py            # human-readable log
    python examples/governor_loop_demo.py --json      # machine-readable summary
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plimsoll.governor_mcp import GovernorTools  # noqa: E402
from plimsoll.models import Policy  # noqa: E402

# A small IT-operations access policy. In a real repository this lives in a committed
# policy.json and is loaded with Governor.from_policy_file(...). The gate honours the
# *decidable-before-execution* subset of the rules: allowlist / forbidden membership,
# required ordering (must_precede), cumulative budgets, and repeated-action limits.
POLICY = Policy(
    allowed_tools={
        "search_tickets",
        "read_record",
        "manager_review",
        "security_review",
        "grant_access",
        "summarize",
        "notify",
    },
    forbidden_tools={"delete_database", "disable_audit_log"},
    # Privileged access must be preceded by BOTH a manager and a security review.
    must_precede=[("manager_review", "grant_access"), ("security_review", "grant_access")],
    max_tokens=5000,
    max_repeated_action_count=2,
)


@dataclass(frozen=True)
class ScriptedStep:
    """One proposed tool call in the simulated loop, with its ground-truth label."""

    call: dict[str, Any]
    expect_block: bool
    why: str


# The simulated agent's intended action stream. Six of these are unsafe by design; the
# gate must block exactly those six and let the seven legitimate calls through. The loop
# only "executes" (appends to the running trace) calls the gate allows.
STREAM: list[ScriptedStep] = [
    ScriptedStep({"tool": "search_tickets", "input": {"q": "open"}}, False, "in-allowlist lookup"),
    ScriptedStep({"tool": "read_record", "input": {"id": 1}}, False, "1st read of record 1"),
    ScriptedStep({"tool": "read_record", "input": {"id": 1}}, False, "2nd read (within repeat limit of 2)"),
    ScriptedStep(
        {"tool": "read_record", "input": {"id": 1}},
        True,
        "3rd identical read -> repeated_action (runaway loop)",
    ),
    ScriptedStep(
        {"tool": "grant_access", "input": {"resource": "prod-db"}},
        True,
        "grant_access before any review -> tool_order (approval bypass)",
    ),
    ScriptedStep({"tool": "delete_database"}, True, "forbidden_tool (destructive)"),
    ScriptedStep({"tool": "exfiltrate_records"}, True, "tool_allowlist (tool not on the allowlist)"),
    ScriptedStep({"tool": "manager_review", "input": {"ticket": 1}}, False, "required manager approval"),
    ScriptedStep({"tool": "security_review", "input": {"ticket": 1}}, False, "required security approval"),
    ScriptedStep(
        {"tool": "grant_access", "input": {"resource": "prod-db"}},
        False,
        "grant_access AFTER both reviews -> ordering satisfied",
    ),
    ScriptedStep(
        {"tool": "summarize", "input": {"corpus": "all-logs"}, "input_tokens": 10000},
        True,
        "max_tokens (single call blows the cumulative token budget)",
    ),
    ScriptedStep({"tool": "disable_audit_log"}, True, "forbidden_tool (tamper with audit trail)"),
    ScriptedStep({"tool": "notify", "input": {"to": "requester"}}, False, "benign notification"),
]


@dataclass
class LoopResult:
    total: int
    unsafe_total: int
    blocked_unsafe: int
    allowed_safe: int
    safe_total: int
    leaked: list[str]
    over_blocked: list[str]
    log: list[dict[str, Any]]

    @property
    def consistent(self) -> bool:
        """True when the gate blocked every unsafe call and no safe call — no surprises."""
        return not self.leaked and not self.over_blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_calls": self.total,
            "unsafe_total": self.unsafe_total,
            "blocked_unsafe": self.blocked_unsafe,
            "safe_total": self.safe_total,
            "allowed_safe": self.allowed_safe,
            "leaked_unsafe_calls": self.leaked,
            "over_blocked_safe_calls": self.over_blocked,
            "consistent": self.consistent,
            "headline": (
                f"blocked {self.blocked_unsafe} of {self.unsafe_total} unsafe calls; "
                f"allowed {self.allowed_safe} of {self.safe_total} safe calls"
            ),
            "log": self.log,
        }


def run_loop(stream: list[ScriptedStep] = STREAM, policy: Policy = POLICY) -> LoopResult:
    """Drive the scripted loop through the live gate; only allowed calls "execute"."""
    gate = GovernorTools.from_policy(policy)  # exposes propose_tool_call (the MCP-style gate)
    executed: list[dict[str, Any]] = []  # the running partial trace (allowed calls only)

    log: list[dict[str, Any]] = []
    unsafe_total = sum(1 for step in stream if step.expect_block)
    safe_total = len(stream) - unsafe_total
    blocked_unsafe = allowed_safe = 0
    leaked: list[str] = []
    over_blocked: list[str] = []

    for index, step in enumerate(stream, start=1):
        # THE GATE: ask before executing. This is exactly what an MCP host / agent loop
        # in any repo would call on each proposed tool call.
        decision = gate.propose_tool_call(executed, step.call)
        allowed = decision["allowed"]
        rules = [finding["rule_id"] for finding in decision["blocking_findings"]]
        tool = step.call["tool"]

        matched = allowed != step.expect_block  # the live decision agrees with the label
        if allowed:
            executed.append(step.call)  # the agent runs the tool; record it in the trace
            if step.expect_block:
                leaked.append(f"step {index}: {tool} ({step.why})")  # unsafe slipped through
            else:
                allowed_safe += 1
        elif step.expect_block:
            blocked_unsafe += 1  # the safeguard fired, as intended
        else:
            over_blocked.append(f"step {index}: {tool}")  # a safe call wrongly blocked

        log.append(
            {
                "step": index,
                "tool": tool,
                "decision": "allow" if allowed else "block",
                "rules": rules,
                "expected": "block" if step.expect_block else "allow",
                "label": step.why,
                "ok": matched,
            }
        )

    return LoopResult(
        total=len(stream),
        unsafe_total=unsafe_total,
        blocked_unsafe=blocked_unsafe,
        allowed_safe=allowed_safe,
        safe_total=safe_total,
        leaked=leaked,
        over_blocked=over_blocked,
        log=log,
    )


def _print_human(result: LoopResult) -> None:
    print("Plimsoll Governor — live pre-execution gate over a simulated agent loop")
    print("=" * 72)
    for entry in result.log:
        glyph = "BLOCK" if entry["decision"] == "block" else "allow"
        marker = "  " if entry["ok"] else "!!"  # !! would mean the gate disagreed with the label
        rules = f"  [{', '.join(entry['rules'])}]" if entry["rules"] else ""
        print(f"{marker} step {entry['step']:>2}  {glyph:<5}  {entry['tool']:<20}{rules}")
        print(f"       {entry['label']}")
    print("=" * 72)
    print(f"  {result.to_dict()['headline']}")
    if result.consistent:
        print("  gate verified: every unsafe call was blocked, every safe call ran.")
    else:
        if result.leaked:
            print(f"  LEAKED (unsafe slipped through): {result.leaked}")
        if result.over_blocked:
            print(f"  OVER-BLOCKED (safe wrongly blocked): {result.over_blocked}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print a machine-readable JSON summary")
    args = parser.parse_args(argv)

    result = run_loop()
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_human(result)

    # Exit non-zero only if the live gate disagreed with the ground-truth labels: an unsafe
    # call that slipped through, or a safe call wrongly blocked. (Blocked unsafe calls are
    # the success case and exit 0.)
    return 0 if result.consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
