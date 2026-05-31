"""Regenerate the IT access-request demo: traces, policy evaluation, and reports.

Deterministic and offline. Writes:
  examples/access-request/traces/{clean,failed,fixed}.trace.json
  examples/access-request/reports/{failed,fixed}-report.{json,html,md}
  examples/access-request/reports/failed-report.{sarif.json,junit.xml}

Run from anywhere: ``python scripts/build_access_request_demo.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AR = ROOT / "examples" / "access-request"


def main() -> int:
    # Make the package importable when run as a plain script, and the reference agent
    # importable from the example directory.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(AR))

    import agent  # examples/access-request/agent.py

    from plimsoll.diff import trajectory_diff
    from plimsoll.io import load_policy, load_trace, write_json
    from plimsoll.report import (
        build_case_report,
        report_to_dict,
        summarize,
        write_html_report,
        write_junit_report,
        write_markdown_report,
        write_sarif_report,
    )
    from plimsoll.rules import evaluate_trace

    traces_dir = AR / "traces"
    reports_dir = AR / "reports"
    policy_path = AR / "policies" / "access-control-policy.json"

    for name, trace in agent.build_traces().items():
        write_json(traces_dir / f"{name}.trace.json", trace)

    policy = load_policy(policy_path)
    baseline = load_trace(traces_dir / "clean.trace.json")

    for name in ("failed", "fixed"):
        trace = load_trace(traces_dir / f"{name}.trace.json")
        reports = [
            build_case_report(trace, evaluate_trace(trace, policy, baseline), trajectory_diff(trace, baseline, policy))
        ]
        write_json(reports_dir / f"{name}-report.json", report_to_dict(reports))
        write_html_report(reports_dir / f"{name}-report.html", reports)
        write_markdown_report(reports_dir / f"{name}-report.md", reports)
        if name == "failed":
            write_sarif_report(reports_dir / f"{name}-report.sarif.json", reports, policy_path=policy_path)
            write_junit_report(reports_dir / f"{name}-report.junit.xml", reports)
        summary = summarize(reports)
        rule_ids = sorted({finding.rule_id for report in reports for finding in report.findings})
        print(f"{name}: passed={summary['passed']}/{summary['cases']} findings={rule_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
