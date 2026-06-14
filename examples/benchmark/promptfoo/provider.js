// Minimal deterministic "replay" provider for promptfoo.
//
// promptfoo is built to CALL a model/provider and assert on its output. To compare it
// fairly against plimsoll on the SAME recorded traces, this provider does not call any
// model: it reads a pre-recorded plimsoll trace and returns ONLY its `final_output`.
//
// That is the honest scope of what promptfoo's built-in output assertions can see by
// default: the final answer. (A faithful run of the trajectory / span / cost cases would
// need a much richer custom provider that replays the full span list and exposes tool
// calls + usage to assertions — see BENCHMARK_vs_promptfoo.md, "ANALYZED" rows.)
const fs = require('fs');
const path = require('path');

class TraceReplayProvider {
  id() {
    return 'plimsoll-trace-replay';
  }

  async callApi(_prompt, context) {
    const caseId = context.vars.case;
    const tracePath = path.join(__dirname, '..', 'traces', `${caseId}.json`);
    const trace = JSON.parse(fs.readFileSync(tracePath, 'utf8'));
    return { output: trace.final_output };
  }
}

module.exports = TraceReplayProvider;
