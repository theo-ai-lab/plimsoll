# Plimsoll — Related Work and Precise Slot

Where Plimsoll sits among deterministic agent-trace checkers: what is genuinely prior art (most of the
primitives are), and what is actually differentiated (packaging and posture, not technique). Every
external reference here was web-verified (June 2026). The point of this page is to be honest about the
competition a reviewer already knows — especially **promptfoo**, the closest capability competitor.

## 1 · Deterministic agent-trace checking — the primitives are table stakes

Plimsoll's checks — trajectory matching, tool allow/forbid/required, budget ceilings, expected-output
matching — are **established 2025–26 primitives**. The honest framing is to say so and compete on
packaging (§2), not to imply Plimsoll invented deterministic trace checking.

| System | What it does | Relation to Plimsoll |
|---|---|---|
| **promptfoo** ([deterministic assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/deterministic/)) | The closest capability competitor: 40+ **deterministic**, no-LLM, no-account assertions incl. `trajectory:tool-sequence`, `trajectory:tool-args-match`, latency and cost ceilings — run offline on recorded traces, with GitHub-Action CI gating. | Covers most of Plimsoll's check list already. Plimsoll is **not** the only deterministic, account-free option; the differentiators are in §2, not the primitives. |
| **agentevals** (LangChain, [repo](https://github.com/langchain-ai/agentevals)) | Readymade trajectory evaluators; `create_trajectory_match_evaluator` with `strict / unordered / subset / superset` modes. | Plimsoll's trajectory match-mode vocabulary **mirrors agentevals** (the README already concedes this). The prior art for the matching semantics. |
| **DeepEval** (Confident AI, [repo](https://github.com/confident-ai/deepeval)) | `ToolCorrectnessMetric` is deterministic (reference-based tool/order/arg comparison). **Note:** its `ArgumentCorrectnessMetric` is *LLM-based*, not deterministic — do not cite it as deterministic. | Undercuts any "only Plimsoll is deterministic" implication. |
| **Ragas** ([agent metrics](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/agents/)) | `ToolCallAccuracy` is a deterministic sequence-alignment check of tool calls + arguments. | Another deterministic tool-trajectory checker. |
| **Inspect AI** (UK AISI, [repo](https://github.com/UKGovernmentBEIS/inspect_ai)) | Deterministic scorers, reproducible seeding; primarily a *model-running* eval framework. | Heavier and model-running; Plimsoll is a no-model CI gate over already-recorded traces. |
| **AgentAssay** (Bhardwaj, [arXiv:2603.02601](https://arxiv.org/abs/2603.02601), Mar 2026) | Concurrent academic statement of the same thesis — token-efficient regression testing of non-deterministic agents via **trace-first, offline** analysis at zero token cost (via statistical behavioral fingerprinting). | Independent academic convergence on Plimsoll's exact "trace-first, offline, zero-token" idea. Differentiate: declarative policy + baseline vs statistical fingerprint. |

## 2 · Plimsoll's precise slot — packaging and posture, not new primitives

What is defensibly differentiated (verified as uncommon), stated as craft, not invention:

1. **Pure-stdlib, zero runtime dependencies, clone-and-run.** agentevals, promptfoo, and DeepEval all
   carry dependency trees; a dependency-free CI gate is a real supply-chain advantage
   (`pyproject.toml` → `dependencies = []`).
2. **SARIF 2.1.0 findings anchored to the exact policy-file line** that triggered them, rendering in
   GitHub's code-scanning Security tab. No trajectory-eval tool surveyed emits SARIF; in the agent
   space SARIF shows up only in security-analysis systems. *(Differentiated positioning — not claimed
   as "first.")*
3. **`must_precede` ordering invariants** engineered so a refusal/escalation path stays valid — they
   constrain *order*, not *presence*, so the gate never forces the dangerous privileged action to occur.
   More safety-specific than a generic tool-sequence assertion.
4. **Retry-drift detection** — a retry that silently changed its input after an error.
5. **One no-network / no-LLM / no-account / no-install CI gate** emitting all five CI surfaces
   (JSON / HTML / JUnit / SARIF / Markdown) with tri-state exit codes and severity-weighted, fail-closed
   scoring.

**Plimsoll's one-sentence slot:** a dependency-free, offline CI gate that re-packages established
deterministic trace-checking primitives with policy-line-anchored SARIF and safety-specific ordering
invariants — *engineering judgment and scope discipline over proven techniques, not a new evaluation
method, and strongest pitched exactly that way.*

## 3 · What Plimsoll is not

- **Not an LLM judge** — no semantic-quality scoring (tone, helpfulness, grounding). Pair it with a
  judge for those.
- **Not an observability platform** (Phoenix / Braintrust / Langfuse) — no dashboards, live ingestion,
  or hosted storage.
- **Not the only deterministic option** — promptfoo and agentevals cover much of the same ground (§1);
  Plimsoll competes on §2, not on having invented the checks.
