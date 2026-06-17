from __future__ import annotations

import hashlib
import json
import os
from html import escape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from plimsoll import __version__
from plimsoll.models import CaseReport, Finding, TraceRun
from plimsoll.passk import (
    PassKReport,
    gate_message,
    html_section,
    markdown_section,
    sarif_rule_and_result,
)
from plimsoll.rules import trace_metrics

SEVERITY_WEIGHTS = {"critical": 45, "high": 30, "medium": 15, "low": 5}

# Each rule maps to the policy key that configures it, so a SARIF finding can be
# anchored to the exact line of the committed policy file that triggered it.
# GitHub code scanning only renders a result when artifactLocation.uri resolves to
# a file committed in the analyzed repo and the result carries a region.startLine;
# anchoring to the (often uncommitted) trace path or a trace:// URI degrades or
# fails the upload, so we anchor to the policy file instead.
RULE_TO_POLICY_KEY = {
    "expected_output": "expected_output_mode",
    "tool_allowlist": "allowed_tools",
    "forbidden_tool": "forbidden_tools",
    "required_tool": "required_tools",
    "max_steps": "max_steps",
    "max_duration_ms": "max_duration_ms",
    "max_tokens": "max_tokens",
    "max_estimated_cost_usd": "max_estimated_cost_usd",
    "repeated_action": "max_repeated_action_count",
    "pii_leak": "pii_patterns",
    "secret_leak": "secret_patterns",
    "trajectory_drift": "max_tool_sequence_distance",
    "trajectory_mismatch": "trajectory_match_mode",
    "tool_order": "must_precede",
}

RULE_DESCRIPTIONS = {
    "expected_output": "The final output did not match the policy's expected result.",
    "tool_allowlist": "The trace used a tool outside the policy allowlist.",
    "forbidden_tool": "The trace used a tool the policy forbids.",
    "required_tool": "The trace skipped a tool the policy requires.",
    "max_steps": "The trace exceeded the configured step budget.",
    "max_duration_ms": "The trace exceeded the configured duration budget.",
    "max_tokens": "The trace exceeded the configured token budget.",
    "max_estimated_cost_usd": "The trace exceeded the configured estimated-cost budget.",
    "repeated_action": "The trace repeated identical tool actions beyond the policy limit.",
    "retry_drift": "A retry changed the tool input after an error, which can hide nondeterministic behavior.",
    "pii_leak": "The trace contains text matching PII patterns.",
    "secret_leak": "The trace contains text matching secret-like / high-entropy patterns.",
    "trajectory_drift": "The tool sequence drifted beyond the allowed edit distance from the baseline.",
    "trajectory_mismatch": "The tool trajectory did not satisfy the configured baseline match mode.",
    "tool_order": "A high-risk tool ran before a required preceding step (e.g. an approval).",
    "reliability_pass_k": "The pass^k reliability metric fell below the configured floor "
    "(the agent is flaky across repeated runs of the same task).",
}


def build_case_report(trace: TraceRun, findings: list[Finding], diff: dict[str, Any] | None = None) -> CaseReport:
    penalty = sum(SEVERITY_WEIGHTS.get(finding.severity, 10) for finding in findings)
    score = max(0, 100 - penalty)
    return CaseReport(
        case_id=trace.case_id,
        run_id=trace.run_id,
        score=score,
        passed=not any(finding.severity in {"critical", "high"} for finding in findings),
        metrics=trace_metrics(trace),
        findings=findings,
        trajectory_diff=diff or {},
    )


def summarize(reports: list[CaseReport]) -> dict[str, Any]:
    total = len(reports)
    passed = sum(1 for report in reports if report.passed)
    findings = [finding for report in reports for finding in report.findings]
    severity_counts: dict[str, int] = {}
    for finding in findings:
        severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1
    return {
        "cases": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "average_score": round(sum(report.score for report in reports) / total, 1) if total else 0.0,
        "severity_counts": severity_counts,
    }


def report_to_dict(reports: list[CaseReport], passk: PassKReport | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": summarize(reports),
        "cases": [
            {
                "case_id": report.case_id,
                "run_id": report.run_id,
                "score": report.score,
                "passed": report.passed,
                "metrics": report.metrics,
                "findings": [
                    {
                        "rule_id": finding.rule_id,
                        "severity": finding.severity,
                        "message": finding.message,
                        "evidence": finding.evidence,
                    }
                    for finding in report.findings
                ],
                "trajectory_diff": report.trajectory_diff,
            }
            for report in reports
        ],
    }
    if passk is not None:
        payload["reliability"] = passk.to_dict()
    return payload


def write_junit_report(path: Path, reports: list[CaseReport], passk: PassKReport | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    failures = sum(1 for report in reports if not report.passed)
    # When a pass^k gate is configured it appears as one extra testcase, failing iff the
    # reliability floor is breached. Omitting passk leaves the suite byte-identical to before.
    passk_failed = passk is not None and passk.gate_failed
    total_tests = len(reports) + (1 if passk is not None else 0)
    total_failures = failures + (1 if passk_failed else 0)
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": "Plimsoll",
            "tests": str(total_tests),
            "failures": str(total_failures),
            "errors": "0",
        },
    )
    for report in reports:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": "Plimsoll",
                "name": report.case_id,
            },
        )
        if not report.passed:
            message = "; ".join(f"{finding.rule_id} [{finding.severity}]" for finding in report.findings)
            failure = ElementTree.SubElement(case, "failure", {"message": message, "type": "PlimsollFinding"})
            failure.text = "\n".join(
                f"{finding.rule_id} [{finding.severity}]: {finding.message}" for finding in report.findings
            )
    if passk is not None:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {"classname": "Plimsoll", "name": f"reliability.pass_caret_{passk.k}"},
        )
        if passk_failed:
            failure = ElementTree.SubElement(
                case,
                "failure",
                {"message": f"pass^{passk.k} below floor", "type": "PlimsollReliability"},
            )
            failure.text = gate_message(passk)
    tree = ElementTree.ElementTree(suite)
    ElementTree.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_sarif_report(
    path: Path,
    reports: list[CaseReport],
    policy_path: Path | None = None,
    input_path: Path | None = None,
    passk: PassKReport | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    findings = [finding for report in reports for finding in report.findings]
    rule_ids = sorted({finding.rule_id for finding in findings})
    anchor_uri, key_lines = _sarif_anchor(policy_path, input_path)
    rules = [
        {
            "id": rule_id,
            "name": "".join(part.capitalize() for part in rule_id.split("_")),
            "shortDescription": {"text": rule_id.replace("_", " ")},
            "fullDescription": {"text": RULE_DESCRIPTIONS.get(rule_id, rule_id.replace("_", " "))},
            "help": {"text": RULE_DESCRIPTIONS.get(rule_id, rule_id.replace("_", " "))},
            "defaultConfiguration": {"level": _sarif_level(_rule_severity(rule_id, findings))},
            "properties": {
                "tags": ["agent-trace", "reliability"],
                "problem": {"severity": _problem_severity(_rule_severity(rule_id, findings))},
            },
        }
        for rule_id in rule_ids
    ]
    results = [
        {
            "ruleId": finding.rule_id,
            "ruleIndex": rule_ids.index(finding.rule_id),
            "level": _sarif_level(finding.severity),
            "kind": "fail",
            "message": {"text": f"{finding.severity}: {finding.message}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": anchor_uri},
                        "region": {"startLine": _finding_line(finding, key_lines)},
                    }
                }
            ],
            "partialFingerprints": {"plimsoll/v1": _sarif_fingerprint(finding)},
            "properties": {"caseId": finding.case_id, "severity": finding.severity, "evidence": finding.evidence},
        }
        for finding in findings
    ]
    # A failed pass^k gate joins the SARIF run as one extra rule + result, anchored to the
    # same committed file. With passk omitted the output is byte-identical to before.
    if passk is not None:
        extra = sarif_rule_and_result(passk, anchor_uri)
        if extra is not None:
            rule, result = extra
            result["ruleIndex"] = len(rules)
            rules.append(rule)
            results.append(result)
    payload = {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Plimsoll",
                        "version": __version__,
                        "rules": rules,
                    }
                },
                "automationDetails": {"id": "plimsoll/agent-trace-policy"},
                "columnKind": "unicodeCodePoints",
                "results": results,
            }
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sarif_anchor(policy_path: Path | None, input_path: Path | None) -> tuple[str, dict[str, int]]:
    """Pick a committed file to anchor findings to, plus a policy-key -> line map.

    Prefers the policy file (always committed in a CI gate); falls back to the input
    path. Never emits a non-file scheme such as ``trace://`` (which fails GitHub's
    SARIF upload).
    """
    if policy_path is not None and Path(policy_path).is_file():
        path = Path(policy_path)
        return _relative_uri(path), _policy_key_lines(path)
    if input_path is not None:
        return _relative_uri(Path(input_path)), {}
    return "policy.json", {}


def _relative_uri(path: Path) -> str:
    try:
        relative = os.path.relpath(path, start=Path.cwd())
    except ValueError:
        relative = path.name
    if relative.startswith(".."):
        relative = path.name
    return relative.replace(os.sep, "/")


def _policy_key_lines(policy_path: Path) -> dict[str, int]:
    lines: dict[str, int] = {}
    try:
        text = policy_path.read_text(encoding="utf-8")
    except OSError:
        return lines
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if not stripped.startswith('"') or '"' not in stripped[1:]:
            continue
        key, after = stripped[1:].split('"', 1)
        # Only treat a quoted token as a policy key when it is followed by a colon,
        # so multi-line string array elements are not mistaken for keys.
        if after.lstrip().startswith(":"):
            lines.setdefault(key, number)
    return lines


def _finding_line(finding: Finding, key_lines: dict[str, int]) -> int:
    key = RULE_TO_POLICY_KEY.get(finding.rule_id)
    if key and key in key_lines:
        return key_lines[key]
    return 1


def _rule_severity(rule_id: str, findings: list[Finding]) -> str:
    for finding in findings:
        if finding.rule_id == rule_id:
            return finding.severity
    return "medium"


def _sarif_fingerprint(finding: Finding) -> str:
    basis = json.dumps(
        {"rule": finding.rule_id, "case": finding.case_id, "evidence": finding.evidence},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def render_markdown(reports: list[CaseReport], passk: PassKReport | None = None) -> str:
    """Render a GitHub-flavored Markdown summary (for PR comments and the CI step summary)."""
    summary = summarize(reports)
    verdict = "❌ FAIL" if summary["failed"] else "✅ PASS"
    counts = summary["severity_counts"]
    breakdown = ", ".join(f"{counts[sev]} {sev}" for sev in ("critical", "high", "medium", "low") if counts.get(sev))
    lines = [
        f"## Plimsoll — {verdict}",
        "",
        (
            f"**{summary['passed']}/{summary['cases']}** cases passed &middot; "
            f"average score **{summary['average_score']}** &middot; "
            f"**{sum(counts.values())}** findings{f' ({breakdown})' if breakdown else ''}"
        ),
        "",
    ]
    findings = [(report.case_id, finding) for report in reports for finding in report.findings]
    if findings:
        lines += ["| Case | Rule | Severity | Finding |", "| --- | --- | --- | --- |"]
        for case_id, finding in findings:
            cell_case = str(case_id).replace("|", "\\|")
            cell_rule = finding.rule_id.replace("|", "\\|")
            cell_message = finding.message.replace("|", "\\|")
            lines.append(f"| {cell_case} | `{cell_rule}` | {finding.severity} | {cell_message} |")
    else:
        lines.append("No findings. All checked cases passed critical and high-severity gates.")
    lines.append("")
    if passk is not None:
        lines.append(markdown_section(passk))
    return "\n".join(lines)


def write_markdown_report(path: Path, reports: list[CaseReport], passk: PassKReport | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(reports, passk), encoding="utf-8")


_RULE_PROVENANCE = {
    "tool_allowlist",
    "forbidden_tool",
    "required_tool",
    "max_steps",
    "max_duration_ms",
    "max_tokens",
    "max_estimated_cost_usd",
    "repeated_action",
    "trajectory_drift",
    "trajectory_mismatch",
    "tool_order",
}

# Self-contained stylesheet (light + dark, system fonts only, no web fonts / CDN). Kept as a plain
# string (not an f-string) so CSS braces are literal. Tokens match the published design direction.
_REPORT_CSS = """
*{box-sizing:border-box}
:root{color-scheme:light dark;
--bg:#FFFFFF;--surface:#FAFAFA;--elevated:#FFFFFF;--text:#0B0C0E;--muted:#5A6068;--faint:#71777E;
--hairline:rgba(15,17,21,0.08);--hairline-strong:rgba(15,17,21,0.14);--grid-ink:rgba(15,17,21,0.012);
--accent:#2F6BE0;--accent-ring:rgba(47,107,224,0.40);
--pass:#118A4F;--pass-tint:rgba(17,138,79,0.11);
--fail:#C8362E;--fail-tint:rgba(200,54,46,0.10);--fail-tint-strong:rgba(200,54,46,0.16);
--warn:#97640A;--warn-tint:rgba(151,100,10,0.13);
--t12:12px;--t13:13px;--t14:14px;--t16:16px;--t20:20px;--t28:28px;--t44:44px;
--rc:6px;--rch:4px;--rp:9999px;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
--mono:ui-monospace,"SF Mono","JetBrains Mono","IBM Plex Mono","Cascadia Code",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0A0B0D;--surface:#101114;--elevated:#15171B;--text:#ECEDEE;--muted:#9AA0A6;--faint:#8A9099;--hairline:rgba(255,255,255,0.10);--hairline-strong:rgba(255,255,255,0.16);--grid-ink:rgba(255,255,255,0.013);--accent:#5B9BFF;--accent-ring:rgba(91,155,255,0.45);--pass:#3FB97A;--pass-tint:rgba(63,185,122,0.14);--fail:#FF6B6B;--fail-tint:rgba(255,107,107,0.12);--fail-tint-strong:rgba(255,107,107,0.17);--warn:#E0A93B;--warn-tint:rgba(224,169,59,0.15)}}
:root[data-theme="light"]{--bg:#FFFFFF;--surface:#FAFAFA;--elevated:#FFFFFF;--text:#0B0C0E;--muted:#5A6068;--faint:#71777E;--hairline:rgba(15,17,21,0.08);--hairline-strong:rgba(15,17,21,0.14);--grid-ink:rgba(15,17,21,0.012);--accent:#2F6BE0;--accent-ring:rgba(47,107,224,0.40);--pass:#118A4F;--pass-tint:rgba(17,138,79,0.11);--fail:#C8362E;--fail-tint:rgba(200,54,46,0.10);--fail-tint-strong:rgba(200,54,46,0.16);--warn:#97640A;--warn-tint:rgba(151,100,10,0.13)}
:root[data-theme="dark"]{--bg:#0A0B0D;--surface:#101114;--elevated:#15171B;--text:#ECEDEE;--muted:#9AA0A6;--faint:#8A9099;--hairline:rgba(255,255,255,0.10);--hairline-strong:rgba(255,255,255,0.16);--grid-ink:rgba(255,255,255,0.013);--accent:#5B9BFF;--accent-ring:rgba(91,155,255,0.45);--pass:#3FB97A;--pass-tint:rgba(63,185,122,0.14);--fail:#FF6B6B;--fail-tint:rgba(255,107,107,0.12);--fail-tint-strong:rgba(255,107,107,0.17);--warn:#E0A93B;--warn-tint:rgba(224,169,59,0.15)}
html,body{margin:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:var(--t14);line-height:1.55;-webkit-font-smoothing:antialiased;background-image:repeating-linear-gradient(0deg,var(--grid-ink),var(--grid-ink) 1px,transparent 1px,transparent 32px),repeating-linear-gradient(90deg,var(--grid-ink),var(--grid-ink) 1px,transparent 1px,transparent 32px)}
main{max-width:1100px;margin:0 auto;padding:40px 24px 80px}
h1,h2,h3{margin:0;font-weight:500;letter-spacing:-0.02em}
h2{font-size:var(--t20);margin-top:12px}
.muted{color:var(--muted)}.faint{color:var(--faint)}.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.seclabel{font-family:var(--mono);font-size:var(--t12);letter-spacing:0.06em;text-transform:uppercase;color:var(--faint);margin:48px 0 12px}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;padding-bottom:20px;border-bottom:1px solid var(--hairline)}
.brand{display:flex;gap:12px;align-items:center}
.brand .mk{width:26px;height:26px;border:1.5px solid var(--text);border-radius:var(--rch);display:flex;align-items:center;justify-content:center;font-size:13px}
.brand h1{font-size:var(--t20)}.brand .sub{font-size:var(--t13);color:var(--muted)}
.topright{display:flex;align-items:center;gap:16px}
.cases{font-size:var(--t13);color:var(--muted)}.cases b{color:var(--text);font-weight:500}
.seg{display:none;border:1px solid var(--hairline);border-radius:var(--rch);overflow:hidden}
.js .seg{display:inline-flex}
.seg button{font:inherit;font-size:var(--t12);font-family:var(--mono);color:var(--muted);background:transparent;border:0;padding:5px 11px;cursor:pointer}
.seg button+button{border-left:1px solid var(--hairline)}
.seg button[aria-pressed="true"]{background:var(--surface);color:var(--text)}
.verdict{margin:28px 0 8px;padding:22px 24px;border:1px solid var(--hairline);border-left-width:3px;border-radius:var(--rc)}
.verdict.fail{background:var(--fail-tint);border-left-color:var(--fail)}
.verdict.pass{background:var(--pass-tint);border-left-color:var(--pass)}
.verdict-top{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.verdict-word{font-size:var(--t44);font-weight:600;letter-spacing:-0.03em;line-height:1}
.verdict.fail .verdict-word,.verdict.fail .vglyph{color:var(--fail)}
.verdict.pass .verdict-word,.verdict.pass .vglyph{color:var(--pass)}
.vglyph{font-size:26px;line-height:1}
.verdict-chips{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.verdict-sub{margin-top:12px;font-family:var(--mono);font-size:var(--t14)}
.verdict.fail .verdict-sub b{color:var(--fail)}.verdict.pass .verdict-sub b{color:var(--pass)}
.verdict-stats{margin-top:16px;font-family:var(--mono);font-size:var(--t13);color:var(--muted);font-variant-numeric:tabular-nums;display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.verdict-stats b{color:var(--text);font-weight:500}.verdict-stats .sep{color:var(--faint)}
.pill{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:var(--t12);padding:3px 9px;border-radius:var(--rp);white-space:nowrap;border:1px solid transparent}
.pill.critical,.pill.high,.pill.fail{color:var(--fail);background:var(--fail-tint);border-color:color-mix(in srgb,var(--fail) 30%,transparent)}
.pill.medium,.pill.warn{color:var(--warn);background:var(--warn-tint);border-color:color-mix(in srgb,var(--warn) 30%,transparent)}
.pill.low{color:var(--accent);background:color-mix(in srgb,var(--accent) 12%,transparent);border-color:color-mix(in srgb,var(--accent) 30%,transparent)}
.pill.pass{color:var(--pass);background:var(--pass-tint);border-color:color-mix(in srgb,var(--pass) 30%,transparent)}
.tag{font-family:var(--mono);font-size:11px;letter-spacing:0.04em;color:var(--faint);border:1px solid var(--hairline);border-radius:var(--rch);padding:1px 6px;text-transform:uppercase}
.code{font-family:var(--mono);font-size:var(--t13);background:var(--surface);border:1px solid var(--hairline);border-radius:var(--rch);padding:1px 6px}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--hairline);border:1px solid var(--hairline);border-radius:var(--rc);overflow:hidden;margin:16px 0}
.card{background:var(--elevated);padding:16px 18px}
.card .k{font-family:var(--mono);font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:var(--faint)}
.card .v{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:var(--t28);margin-top:6px;letter-spacing:-0.01em}
.card .v.fail{color:var(--fail)}.card .v small{font-size:var(--t14);color:var(--muted)}
.fs{border:1px solid var(--hairline);border-radius:var(--rc);background:var(--elevated);overflow:hidden}
.fs .row{display:flex;align-items:center;gap:12px;padding:12px 16px}
.fs .row+.row{border-top:1px solid var(--hairline)}
.case{border:1px solid var(--hairline);border-radius:var(--rc);background:var(--elevated);margin-top:16px;overflow:hidden}
.case-head{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:16px 20px;border-bottom:1px solid var(--hairline)}
.case-head h3{font-size:var(--t16)}
.case-head .meta{font-family:var(--mono);font-size:var(--t12);color:var(--muted);margin-top:4px}
.case-body{padding:20px}
.why{border:1px solid color-mix(in srgb,var(--fail) 30%,transparent);border-left-width:3px;background:var(--fail-tint);border-radius:var(--rc);padding:14px 16px;margin-bottom:20px}
.why b{display:block;margin-bottom:6px}.why .line{font-size:var(--t13);margin-top:3px}.why code{font-family:var(--mono);color:var(--fail)}
.kv{width:100%;border-collapse:collapse;margin:4px 0 8px}
.kv td{padding:7px 0;border-top:1px solid var(--hairline);vertical-align:top;font-size:var(--t13)}
.kv td:first-child{color:var(--muted);font-family:var(--mono);font-size:var(--t12);width:170px}
.kv td:last-child{font-family:var(--mono);font-variant-numeric:tabular-nums}
.seq .viol{color:var(--fail)}
table.find{width:100%;border-collapse:collapse;margin-top:6px}
table.find th{text-align:left;font-family:var(--mono);font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:var(--faint);font-weight:400;padding:8px 14px 8px 0;border-bottom:1px solid var(--hairline)}
table.find td{padding:12px 14px 12px 0;border-top:1px solid var(--hairline);vertical-align:top;font-size:var(--t13)}
details.ev>summary{cursor:pointer;color:var(--accent);font-family:var(--mono);font-size:var(--t12);list-style:none}
details.ev>summary::-webkit-details-marker{display:none}
details.ev>summary::before{content:"\\25B8 ";color:var(--faint)}
details.ev[open]>summary::before{content:"\\25BE "}
.evwrap{position:relative;margin-top:8px}
pre{margin:0;font-family:var(--mono);font-size:var(--t12);background:var(--surface);border:1px solid var(--hairline);border-radius:var(--rc);padding:12px 14px;overflow:auto;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.copy{position:absolute;top:8px;right:8px;font:inherit;font-family:var(--mono);font-size:11px;color:var(--muted);background:var(--elevated);border:1px solid var(--hairline);border-radius:var(--rch);padding:2px 8px;cursor:pointer}
.traj{margin-top:8px}
.traj-head{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:6px}
.traj-head h3{font-size:var(--t16)}
.legend{font-family:var(--mono);font-size:var(--t12);color:var(--muted);margin:0 0 12px;display:flex;gap:16px;flex-wrap:wrap}
.legend .g{color:var(--faint)}
.diffseg{display:none}.js .diffseg{display:inline-flex}
.stepper{border:1px solid var(--hairline);border-radius:var(--rc);overflow:hidden}
.step{display:grid;grid-template-columns:30px 26px 1fr auto;align-items:center;gap:10px;padding:9px 0}
.step+.step{border-top:1px solid var(--hairline)}
.step .gut{text-align:center;font-family:var(--mono);color:var(--faint)}
.step .node{display:flex;justify-content:center}
.step .dot{width:9px;height:9px;border-radius:50%;border:1.5px solid var(--faint)}
.step .name{font-family:var(--mono);font-size:var(--t13);display:flex;align-items:center;gap:8px}
.step .d{font-family:var(--mono);font-size:var(--t12);font-variant-numeric:tabular-nums;color:var(--muted);display:flex;gap:16px;padding-right:16px}
.step.del{background:var(--fail-tint)}
.step.del .name{text-decoration:line-through;color:var(--fail)}
.step.del .gut,.step.del .d{color:var(--fail)}.step.del .dot{border-color:var(--fail)}
.step.ins{background:var(--pass-tint)}.step.ins .gut{color:var(--pass)}.step.ins .dot{border-color:var(--pass)}
.step.viol{background:var(--fail-tint-strong)}.step.viol .gut{color:var(--fail)}.step.viol .dot{border-color:var(--fail);background:var(--fail)}
.drawer{border-top:1px solid var(--hairline)}
.drawer-body{font-family:var(--mono);font-size:var(--t12);color:var(--muted);padding:12px 16px 14px 66px;display:grid;grid-template-columns:auto 1fr;gap:5px 16px}
.drawer-body .lbl{color:var(--faint)}.drawer-body b{color:var(--text);font-weight:500}.drawer-body .bad{color:var(--fail)}
.delta-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--hairline);border:1px solid var(--hairline);border-radius:var(--rc);overflow:hidden;margin-top:14px}
.dc{background:var(--elevated);padding:14px 16px}
.dc .k{font-family:var(--mono);font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:var(--faint)}
.dc .v{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:var(--t20);margin-top:6px}
.dc .v.neg{color:var(--fail)}.dc .v.pos{color:var(--pass)}
.note{font-family:var(--mono);font-size:var(--t12);color:var(--muted);margin-top:10px}
.final{margin-top:14px;border:1px solid var(--hairline);border-radius:var(--rc);overflow:hidden}
.final .fo{padding:14px 16px}.final .fo+.fo{border-top:1px solid var(--hairline)}
.final .fo .k{font-family:var(--mono);font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:var(--faint);margin-bottom:6px}
.final .fo.cand{background:var(--fail-tint)}
.split{display:none;border:1px solid var(--hairline);border-radius:var(--rc);overflow:hidden;margin-top:2px}
.split .cols{display:grid;grid-template-columns:1fr 1fr}
.split .col{padding:14px 16px}.split .col+.col{border-left:1px solid var(--hairline)}
.split .col .k{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:0.06em;color:var(--faint);margin-bottom:8px}
.split .col .s{font-family:var(--mono);font-size:var(--t13);padding:2px 0}
.show-split .stepper{display:none}.show-split .split{display:block}
.footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--hairline);font-family:var(--mono);font-size:var(--t12);color:var(--faint);display:flex;gap:10px;flex-wrap:wrap;align-items:baseline}
.footer .dot{color:var(--pass)}.footer .sep{color:var(--hairline-strong)}
a{color:var(--accent)}
:focus-visible{outline:0;box-shadow:0 0 0 2px var(--accent-ring);border-radius:var(--rch)}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
@media (max-width:760px){.cards,.delta-cards{grid-template-columns:repeat(2,1fr)}.topbar{flex-direction:column}.verdict-chips{margin-left:0}.split .cols{grid-template-columns:1fr}.split .col+.col{border-left:0;border-top:1px solid var(--hairline)}}
"""

# Progressive enhancement only: the report is fully readable with this script disabled.
_REPORT_JS = """
(function(){var d=document,root=d.documentElement;root.classList.add('js');
function theme(m){if(m==='auto'){root.removeAttribute('data-theme');try{localStorage.removeItem('tp-theme')}catch(e){}}else{root.setAttribute('data-theme',m);try{localStorage.setItem('tp-theme',m)}catch(e){}}
var cur=root.getAttribute('data-theme')||'auto';d.querySelectorAll('[data-theme-btn]').forEach(function(b){b.setAttribute('aria-pressed',String(b.getAttribute('data-theme-btn')===cur))})}
d.querySelectorAll('[data-theme-btn]').forEach(function(b){b.addEventListener('click',function(){theme(b.getAttribute('data-theme-btn'))})});theme(root.getAttribute('data-theme')||'auto');
d.querySelectorAll('[data-diff-toggle]').forEach(function(b){b.addEventListener('click',function(){var s=b.closest('.traj');if(!s)return;s.classList.toggle('show-split',b.getAttribute('data-diff-toggle')==='split');s.querySelectorAll('[data-diff-toggle]').forEach(function(x){x.setAttribute('aria-pressed',String(x===b))})})});
d.querySelectorAll('pre[data-copy]').forEach(function(pre){var btn=d.createElement('button');btn.className='copy';btn.type='button';btn.textContent='copy';btn.addEventListener('click',function(){if(navigator.clipboard){navigator.clipboard.writeText(pre.textContent).then(function(){btn.textContent='copied';setTimeout(function(){btn.textContent='copy'},1200)})}});pre.parentNode.insertBefore(btn,pre)});
})();
"""


def write_html_report(path: Path, reports: list[CaseReport], passk: PassKReport | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = summarize(reports)
    case_label = f'<div class="seclabel">Case &middot; {summary["cases"]} total</div>' if reports else ""
    parts = [_verdict_band(reports, summary), _stat_cards(summary)]
    if passk is not None:
        parts.append(html_section(passk))
    parts += [
        _finding_summary(reports),
        case_label,
        "\n".join(_case_block(report) for report in reports),
    ]
    body = "\n".join(parts)
    head_script = (
        "<script>try{var e=document.documentElement;e.classList.add('js');"
        "var t=localStorage.getItem('tp-theme');if(t)e.setAttribute('data-theme',t);}catch(_){}</script>"
    )
    html = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Plimsoll Report</title>\n"
        f"{head_script}\n<style>{_REPORT_CSS}</style>\n</head>\n<body>\n<main>\n"
        f"{_topbar(summary)}\n{body}\n{_footer()}\n</main>\n"
        f"<script>{_REPORT_JS}</script>\n</body>\n</html>\n"
    )
    path.write_text(html, encoding="utf-8")


def _topbar(summary: dict[str, Any]) -> str:
    seg = (
        '<div class="seg" role="group" aria-label="Theme">'
        '<button type="button" data-theme-btn="light">Light</button>'
        '<button type="button" data-theme-btn="dark">Dark</button>'
        '<button type="button" data-theme-btn="auto">Auto</button></div>'
    )
    return (
        '<div class="topbar"><div class="brand"><span class="mk" aria-hidden="true">PL</span>'
        "<div><h1>Plimsoll Report</h1>"
        '<div class="sub">Deterministic regression checks for recorded AI-agent traces.</div></div></div>'
        f'<div class="topright"><span class="cases"><b>{summary["passed"]}</b> / {summary["cases"]} cases passed</span>'
        f"{seg}</div></div>"
    )


def _footer() -> str:
    bits = ["deterministic", "no LLM judge", "no network", "same trace in, same findings out", "Plimsoll"]
    inner = '<span class="sep" aria-hidden="true">·</span>'.join(f"<span>{escape(b)}</span>" for b in bits)
    return f'<div class="footer"><span class="dot" aria-hidden="true">●</span>{inner}</div>'


_GLYPH = {"critical": "✕", "high": "▲", "medium": "◆", "low": "▪"}


def _provenance(rule_id: str) -> str:
    return "RULE" if rule_id in _RULE_PROVENANCE else "CODE"


def _severity_pill(severity: str) -> str:
    glyph = _GLYPH.get(severity, "")
    return f'<span class="pill {escape(severity)}">{glyph} {escape(severity)}</span>'


def _verdict_band(reports: list[CaseReport], summary: dict[str, Any]) -> str:
    failed = summary["failed"]
    state = "fail" if failed else "pass"
    word = "FAILED" if failed else "PASSED"
    glyph = "✕" if failed else "✓"
    counts = summary["severity_counts"]
    total = sum(counts.values())
    chips = "".join(
        f'<span class="pill {sev}">{_GLYPH[sev]} {sev} {counts[sev]}</span>'
        for sev in ("critical", "high", "medium", "low")
        if counts.get(sev)
    )
    plural = "s" if total != 1 else ""
    sub = f"<b>{total} finding{plural}</b> block this run" if failed else "All checks passed."
    steps = sum(int(report.metrics.get("steps", 0)) for report in reports)
    duration = sum(int(report.metrics.get("duration_ms", 0)) for report in reports)
    case_plural = "s" if summary["cases"] != 1 else ""
    stat_bits = [
        f"<span><b>{summary['failed']}</b> failed</span>",
        f"<span><b>{summary['passed']}</b> passed</span>",
        f"<span><b>{summary['cases']}</b> case{case_plural}</span>",
        f"<span>avg score <b>{summary['average_score']}</b></span>",
        f"<span><b>{steps}</b> steps</span>",
        f"<span><b>{duration}</b> ms</span>",
    ]
    stats = '<span class="sep" aria-hidden="true">·</span>'.join(stat_bits)
    return (
        f'<div class="verdict {state}" role="status" aria-label="Overall result: {word}">'
        f'<div class="verdict-top"><span class="vglyph" aria-hidden="true">{glyph}</span>'
        f'<span class="verdict-word">{word}</span>'
        f'<span class="verdict-chips">{chips}</span></div>'
        f'<div class="verdict-sub">{sub}</div>'
        f'<div class="verdict-stats">{stats}</div></div>'
    )


def _stat_cards(summary: dict[str, Any]) -> str:
    failed = summary["failed"]
    total = sum(summary["severity_counts"].values())
    pass_rate = f"{summary['pass_rate'] * 100:.0f}"
    cards = [
        ("Pass rate", f"{pass_rate}<small>%</small>", ""),
        ("Avg score", f"{summary['average_score']}", ""),
        ("Cases", f"{summary['cases']}", ""),
        ("Failed", f"{failed}", " fail" if failed else ""),
        ("Findings", f"{total}", " fail" if total else ""),
    ]
    inner = "".join(
        f'<div class="card"><div class="k">{label}</div><div class="v{cls}">{value}</div></div>'
        for label, value, cls in cards
    )
    return f'<div class="cards">{inner}</div>'


def _finding_summary(reports: list[CaseReport]) -> str:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings = sorted(
        (finding for report in reports for finding in report.findings),
        key=lambda finding: (order.get(finding.severity, 9), finding.rule_id),
    )
    if not findings:
        return (
            "<h2>Finding Summary</h2>"
            '<div class="fs"><div class="row muted">No findings. '
            "All checked cases passed critical and high-severity gates.</div></div>"
        )
    rows = "".join(
        f'<div class="row">{_severity_pill(finding.severity)}'
        f'<span class="code">{escape(finding.rule_id)}</span>'
        f'<span class="tag">{_provenance(finding.rule_id)}</span>'
        f'<span class="msg">{escape(finding.message)}</span></div>'
        for finding in findings
    )
    plural = "s" if len(findings) != 1 else ""
    return (
        f'<h2>Finding Summary <span class="faint mono"> failed-first &middot; {len(findings)} finding{plural}</span></h2>'
        f'<div class="fs">{rows}</div>'
    )


def _case_block(report: CaseReport) -> str:
    badge = "pass" if report.passed else "fail"
    glyph = "✓" if report.passed else "▲"
    status = "PASS" if report.passed else "REVIEW"
    head = (
        '<div class="case-head"><div><h3>'
        + escape(report.case_id)
        + f'</h3><div class="meta">run {escape(report.run_id)} &middot; score {report.score}</div></div>'
        + f'<span class="pill {badge}">{glyph} {status}</span></div>'
    )
    body = "".join(
        [
            _why_section(report),
            _run_metrics_table(report),
            _findings_table(report),
            _trajectory(report.trajectory_diff, report) if report.trajectory_diff else "",
        ]
    )
    return f'<div class="case">{head}<div class="case-body">{body}</div></div>'


def _why_section(report: CaseReport) -> str:
    blockers = [finding for finding in report.findings if finding.severity in {"critical", "high"}]
    if not blockers:
        return ""
    lines = "".join(
        f'<div class="line"><code>{escape(finding.rule_id)}</code> &mdash; {escape(finding.message)}</div>'
        for finding in blockers[:4]
    )
    return f'<div class="why"><b>Why this needs review</b>{lines}</div>'


def _run_metrics_table(report: CaseReport) -> str:
    after_set = {finding.evidence.get("after") for finding in report.findings if finding.rule_id == "tool_order"}
    rows = []
    for key, value in report.metrics.items():
        if key == "tool_sequence":
            parts = " &rarr; ".join(
                (f'<span class="viol">{escape(str(tool))}</span>' if tool in after_set else escape(str(tool)))
                for tool in value
            )
            cell = f'<span class="seq">[ {parts} ]</span>'
        elif key == "duration_ms":
            cell = f"{value} ms"
        elif key == "tokens":
            cell = f"{value} tok"
        elif key == "estimated_cost_usd":
            cell = f"${value:.4f}"
        else:
            cell = escape(str(value))
        rows.append(f"<tr><td>{escape(str(key))}</td><td>{cell}</td></tr>")
    return f'<div class="seclabel">Run metrics</div><table class="kv"><tbody>{"".join(rows)}</tbody></table>'


def _findings_table(report: CaseReport) -> str:
    if not report.findings:
        rows = '<tr><td colspan="4" class="muted">No findings.</td></tr>'
    else:
        rows = "".join(
            "<tr>"
            f'<td><span class="code">{escape(finding.rule_id)}</span> '
            f'<span class="tag">{_provenance(finding.rule_id)}</span></td>'
            f"<td>{_severity_pill(finding.severity)}</td>"
            f"<td>{escape(finding.message)}</td>"
            '<td><details class="ev"><summary>evidence</summary>'
            f'<div class="evwrap"><pre data-copy>{_pretty_json(finding.evidence)}</pre></div></details></td>'
            "</tr>"
            for finding in report.findings
        )
    head = "<tr><th>Rule</th><th>Severity</th><th>Finding</th><th>Evidence</th></tr>"
    return f'<div class="seclabel">Findings</div><table class="find"><thead>{head}</thead><tbody>{rows}</tbody></table>'


def _required_chain(after: str, pairs: list[tuple[str, str]]) -> list[str]:
    """Tools that must transitively precede ``after`` under must_precede, topologically
    ordered, ending with ``after`` itself.

    Only edges reachable from ``after`` are followed, so two *independent* ordering rules
    (e.g. authorize->charge and pack->ship) never get spliced into one invented chain.
    """
    preds: dict[str, set[str]] = {}
    for before, aft in pairs:
        preds.setdefault(aft, set()).add(before)
    needed: set[str] = set()
    stack = list(preds.get(after, set()))
    while stack:
        tool = stack.pop()
        if tool in needed:
            continue
        needed.add(tool)
        stack.extend(preds.get(tool, set()))
    nodes = needed | {after}
    indeg = {node: 0 for node in nodes}
    adj: dict[str, list[str]] = {node: [] for node in nodes}
    for before, aft in pairs:
        if before in nodes and aft in nodes:
            adj[before].append(aft)
            indeg[aft] += 1
    order: list[str] = []
    queue = sorted(node for node in nodes if indeg[node] == 0)
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in sorted(adj[node]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
        queue.sort()
    if len(order) != len(nodes):  # cycle guard: append any leftovers deterministically
        order.extend(sorted(node for node in nodes if node not in order))
    return order


def _order_drawer(finding: Finding, chain_pairs: list[tuple[str, str]], sequence: list[str]) -> str:
    """Render the explanation drawer for a single tool_order violation, attributed to its
    own evidence so two violations never share one (wrong) chain or fired-at step."""
    after = finding.evidence.get("after", "")
    before = finding.evidence.get("before", "")
    fired = finding.evidence.get("fired_at", "?")
    chain_tools = _required_chain(after, chain_pairs) if chain_pairs else [before, after]
    chain = " &rarr; ".join(escape(str(name)) for name in chain_tools)
    bypassed = sum(1 for name in chain_tools[:-1] if name not in sequence)
    got = " &rarr; ".join(escape(str(name)) for name in sequence)
    plural = "s" if bypassed != 1 else ""
    return (
        '<div class="drawer"><div class="drawer-body">'
        f'<span class="lbl">fired at</span><span>step <b>{escape(str(fired))}</b> &middot; '
        f"with no preceding {escape(str(before))}</span>"
        f'<span class="lbl">requires</span><span>{chain}</span>'
        f'<span class="lbl">got</span><span class="bad">{got}</span>'
        f'<span class="lbl">impact</span><span>{escape(str(after))} ran without its required '
        f"approval{plural} ({bypassed} bypassed)</span></div></div>"
    )


def _trajectory(diff: dict[str, Any], report: CaseReport) -> str:
    steps = diff.get("steps", [])
    order_findings = [finding for finding in report.findings if finding.rule_id == "tool_order"]
    after_set = {finding.evidence.get("after") for finding in order_findings}
    before_set = {finding.evidence.get("before") for finding in order_findings}
    sequence = report.metrics.get("tool_sequence", [])
    chain_pairs = [tuple(pair) for pair in diff.get("must_precede", [])]
    required_tools = {name for pair in chain_pairs for name in pair}
    findings_by_after: dict[str, list[Finding]] = {}
    for finding in order_findings:
        findings_by_after.setdefault(finding.evidence.get("after"), []).append(finding)
    emitted: set[int] = set()

    rows = []
    for step in steps:
        tool = step.get("tool", "")
        op = step.get("op", "equal")
        # A violation is anchored to the candidate-side occurrence of the tool (equal/insert);
        # the deleted baseline copy of a reordered tool is a dropped step, not the violation row.
        viol = tool in after_set and op != "delete"
        cls = "viol" if viol else ("del" if op == "delete" else ("ins" if op == "insert" else ""))
        gut = "✕" if viol else ("−" if op == "delete" else ("+" if op == "insert" else "·"))
        if op == "delete" and (tool in required_tools or tool in before_set):
            tagx = '<span class="pill fail">required step dropped</span>'
        elif viol:
            tagx = '<span class="pill fail">✕ tool_order</span>'
        else:
            tagx = ""
        if viol and op == "equal":
            right = "<span>present in both</span>"
        else:
            right = f"<span>Δ {step.get('delta_ms', 0):+} ms</span><span>Δ {step.get('delta_tokens', 0):+} tok</span>"
        rows.append(
            f'<div class="step {cls}"><span class="gut" aria-hidden="true">{gut}</span>'
            '<span class="node"><span class="dot"></span></span>'
            f'<span class="name">{escape(str(tool))}{tagx}</span>'
            f'<span class="d">{right}</span></div>'
        )
        if viol:
            # Emit one drawer per violation, each keyed to its own finding — never reuse a
            # single finding's chain/fired-at across unrelated ordering rules.
            for finding in findings_by_after.get(tool, []):
                if id(finding) in emitted:
                    continue
                emitted.add(id(finding))
                rows.append(_order_drawer(finding, chain_pairs, sequence))
    # Defensive: surface any violation whose `after` never rendered as a candidate step.
    for finding in order_findings:
        if id(finding) not in emitted:
            emitted.add(id(finding))
            rows.append(_order_drawer(finding, chain_pairs, sequence))
    stepper = f'<div class="stepper">{"".join(rows)}</div>'

    delta = diff.get("metrics_delta", {})

    def _card(key: str, label: str, unit: str) -> str:
        value = delta.get(key, 0)
        is_num = isinstance(value, (int, float))
        cls = "neg" if is_num and value < 0 else ("pos" if is_num and value > 0 else "")
        if key == "estimated_cost_usd":
            text = "±$0.0000" if not value else f"${value:+.4f}"
        else:
            text = f"{value:+}{unit}"
        return f'<div class="dc"><div class="k">Δ {label}</div><div class="v {cls}">{text}</div></div>'

    delta_cards = (
        '<div class="delta-cards">'
        + _card("steps", "steps", "")
        + _card("duration_ms", "duration", " ms")
        + _card("tokens", "tokens", " tok")
        + _card("estimated_cost_usd", "est. cost", "")
        + "</div>"
    )

    note = ""
    if report.findings and any(
        isinstance(delta.get(key), (int, float)) and delta.get(key, 0) < 0 for key in ("steps", "duration_ms", "tokens")
    ):
        note = (
            '<div class="note">~ Fewer steps, duration, and tokens are not a win here &mdash; '
            "the deltas are negative because required steps were skipped.</div>"
        )

    final = ""
    output = diff.get("final_output", {})
    if output:
        cand_cls = " cand" if output.get("changed") else ""
        final = (
            '<div class="final">'
            f'<div class="fo"><div class="k">baseline</div><div>{escape(str(output.get("baseline", "")))}</div></div>'
            f'<div class="fo{cand_cls}"><div class="k">candidate</div>'
            f"<div>{escape(str(output.get('current', '')))}</div></div></div>"
        )

    seq = diff.get("tool_sequence", {})
    base_col = "".join(f'<div class="s">{escape(str(tool))}</div>' for tool in seq.get("baseline", []))
    cur_col = "".join(f'<div class="s">{escape(str(tool))}</div>' for tool in seq.get("current", []))
    split = (
        '<div class="split"><div class="cols">'
        f'<div class="col"><div class="k">baseline</div>{base_col}</div>'
        f'<div class="col"><div class="k">candidate</div>{cur_col}</div></div></div>'
    )

    legend = (
        '<div class="legend"><span><span class="g">−</span> removed</span>'
        '<span><span class="g">+</span> inserted</span>'
        '<span><span class="g">·</span> unchanged</span>'
        '<span><span class="g">✕</span> ordering violation</span></div>'
    )
    toggle = (
        '<div class="seg diffseg" role="group" aria-label="Diff mode">'
        '<button type="button" data-diff-toggle="unified" aria-pressed="true">Unified</button>'
        '<button type="button" data-diff-toggle="split" aria-pressed="false">Split</button></div>'
    )
    raw = (
        '<details class="ev" style="margin-top:14px"><summary>raw trajectory_diff (json)</summary>'
        f'<div class="evwrap"><pre data-copy>{_pretty_json(diff)}</pre></div></details>'
    )
    return (
        '<div class="traj"><div class="seclabel">Trajectory Diff</div>'
        f'<div class="traj-head"><h3>Trajectory Diff</h3>{toggle}</div>'
        f"{legend}{stepper}{split}{delta_cards}{note}{final}{raw}</div>"
    )


def _pretty_json(value: Any) -> str:
    return escape(json.dumps(value, indent=2, sort_keys=True))


def _sarif_level(severity: str) -> str:
    if severity in {"critical", "high"}:
        return "error"
    if severity == "medium":
        return "warning"
    return "note"


def _problem_severity(severity: str) -> str:
    if severity in {"critical", "high"}:
        return "error"
    if severity == "medium":
        return "warning"
    return "recommendation"
