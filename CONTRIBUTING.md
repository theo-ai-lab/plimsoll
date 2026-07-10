# Contributing to Plimsoll

Thanks for your interest in improving Plimsoll. This is a small, focused tool,
and contributions that keep it small and focused are the most welcome.

## Principles

Please keep changes aligned with the project's design constraints:

- **Deterministic.** The same trace and policy must always produce the same
  findings and reports. No randomness, no wall-clock dependence in results.
- **Zero runtime dependencies.** Plimsoll runs on the standard library only.
  The sole development dependency is `ruff`. Pull requests must not add a runtime
  dependency.
- **Local-first.** The tool makes no network calls and needs no account, telemetry,
  or API keys. It reads local files and writes local reports.
- **Honest scope.** Plimsoll is a deterministic trace harness, not an LLM
  judge. It does not prove semantic correctness beyond the configured expected
  output and trace policies. Keep documentation and findings claims accurate.

## Development setup

Requires Python 3.11+.

```bash
python -m pip install -e '.[dev]'
```

Run the tests (233 tests):

```bash
python -m unittest discover -s tests
```

Lint and compile-check:

```bash
ruff check .
python -m compileall plimsoll
```

Both CLI entry points work and are worth a quick manual check:

```bash
python -m plimsoll --version
plimsoll --help
```

## Adding a new rule

Rules live in `plimsoll/rules.py`. Each check is a function that inspects a
`TraceRun` (and, where relevant, the `Policy` and an optional baseline) and
returns a list of `Finding` objects. An empty list means the check passed.

To add a rule:

1. Write a `check_*` function in `plimsoll/rules.py` that returns
   `list[Finding]`.
2. Wire it into `evaluate_trace` so it runs as part of evaluation.
3. Choose an appropriate severity. Findings of `high` or `critical` severity
   drive the non-zero exit code.
4. Add a test in `tests/` (rule behavior belongs in `tests/test_rules.py`).
   Cover both the passing and failing cases.

Existing checks include `expected_output`, `tool_allowlist`, `forbidden_tool`,
`required_tool`, `tool_order` (`must_precede` ordering), budgets (`max_steps` /
`max_duration_ms` / `max_tokens` / `max_estimated_cost_usd`), `repeated_action`,
`retry_drift`, `pii_leak`, `secret_leak`, `trajectory_drift` (edit distance), and
`trajectory_mismatch` (match modes: strict / unordered / subset / superset). Use
them as references
for shape and severity.

## Commit style

This project uses [Conventional Commits](https://www.conventionalcommits.org/),
for example `feat: add required_tool ordering check` or
`fix: correct token budget comparison`.

## Pull requests

Before opening a pull request, confirm:

- Tests are added or updated and the full suite passes.
- `ruff check .` is clean.
- No new runtime dependencies were introduced.
- Documentation and any `CHANGELOG` entries are updated to match the change.
- Every claim in code, docs, and the PR description is accurate.

Keep changes scoped and the diff easy to review. Smaller, well-tested pull
requests land faster.

## License

By contributing, you agree that your contributions are licensed under the
project's MIT License.
