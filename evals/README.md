# Evals

Measured from day one so agent changes are judged on numbers, not vibes.

| Suite | Command | Metrics |
|---|---|---|
| Security gate | `python evals/gate/run_eval.py [--no-ai] [--fixes]` | recall on labelled vulnerable snippets, false-positive rate on known-safe code, fix verification rate on `examples/vulnerable-demo` |
| Content quality | `python evals/content/run_eval.py` | answer-first structure, no invented facts (TODO markers where unknown), schema validity, publish-guardrail pass rate |
| Cost | Admin → Observability → Agent costs, or `AIClient.cost_summary()` | tokens and dollars per agent, task and page |

CI runs the gate eval without AI (`--no-ai`) and fails if the false-positive rate exceeds
10% or recall drops below 90%. Runs with AI use your own key and the per-run spend limit.

Add cases to `evals/gate/cases/*.yaml`: vulnerable cases list the rule ids that must be found;
safe cases must produce no findings at medium severity or above.
