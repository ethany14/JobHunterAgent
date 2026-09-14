# JobHunterAgent

JobHunterAgent is a LangGraph workflow that analyzes a resume and job description,
matches requirements to quoted resume evidence, writes a tailored resume, verifies
every generated claim, and pauses for human approval. Unsupported claims enter a
bounded revision loop before review.

```text
Analyze → Match → Write → Verify → Revise when needed → Human Review
```

## Run locally

Create `.env` with `LLM_MODEL_ID`, `LLM_API_KEY`, and optionally `LLM_BASE_URL`, then run:

```powershell
.\.venv\Scripts\python.exe main.py resume.txt job.txt `
  --thread-id application-001 --output result.json
```

Run unit tests and quality evaluations with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m evals.run_evals
.\.venv\Scripts\python.exe -m evals.run_ablation
.\.venv\Scripts\python.exe -m evals.run_stability_evals
```

## Evaluation baseline

The current baseline uses Gemini 3.7 Flash at temperature 0, five synthetic workflow
cases, and three synthetic adversarial cases. Four workflow cases are valid inputs;
one case checks input validation.

Across four valid workflows, the verification layer added one model call, about 425
tokens, $0.00013 estimated cost, and 1.65 seconds of latency per workflow on average.
In three synthetic adversarial cases, it detected and removed all four injected
unsupported claims with no false positives among the supported control claims.

| Average per valid workflow | Writer-only | Full agent | Increase |
|---|---:|---:|---:|
| Latency | 7.78s | 9.43s | 1.65s |
| Model calls | 4 | 5 | 1 |
| Tokens | 2,053.8 | 2,478.3 | 424.5 |
| Estimated cost | $0.003586 | $0.003716 | $0.000130 |

The four-workflow total increase was 4 model calls, 1,698 tokens, and $0.0005205.
Running the additional adversarial suite cost approximately $0.00503. These are
evaluation expenses; the adversarial-suite cost is not a per-request production cost.

The cost estimate uses the Gemini 3.7 Flash paid-tier list price through December 31,
2026. Provider charges may differ. Results are based on a small synthetic dataset and
one run per case, so the observed 100% detection and revision rates should not be
treated as estimates of real-world accuracy.

See [`evals/results/langgraph_v0.1_baseline.json`](evals/results/langgraph_v0.1_baseline.json)
and [`evals/results/ablation_v0.1.json`](evals/results/ablation_v0.1.json) for complete
case-level results, token counts, pricing assumptions, and run metadata.

## Repeated stability evaluation

The v0.2 stability evaluation runs 20 synthetic workflow cases three times each.
Backend, Data Analyst, Business Analyst, and AI Engineer each contribute five cases.
The dataset includes four prompt-injection cases, four synonym cases, and four
numeric-constraint cases. A separate set contains 18 injected unsupported claims
across ten adversarial cases, also repeated three times.

| Metric | Measured result |
|---|---:|
| Workflows reaching human review | 60/60 |
| Mean latency | 9.27s |
| P50 latency | 8.94s |
| P95 latency | 11.82s |
| Mean tokens per workflow | 2,360.4 |
| Mean estimated cost per workflow | $0.003608 |
| Canonical recall, macro / micro | 100% / 100% |
| Unsupported-claim detection recall | 100% |
| Supported-control false-positive rate | 0% |
| Revision success rate | 100% |
| Decision consistency across three runs | 95% of cases |
| Exact tailored-resume consistency | 5% of cases |

All 12 prompt-injection runs reached human review without a forbidden claim and
recalled all expected missing requirements. The 12 synonym runs produced no
unexpected missing requirements. The 12 numeric-constraint runs recalled all
expected constrained requirements.

The one decision inconsistency occurred in a Business Analyst perfect-match case:
all three runs passed, but one run needed an automatic revision while two did not.
Only one of 20 cases produced byte-equivalent normalized resume JSON across all
three runs. Temperature 0 therefore improved reproducibility but did not make the
generated wording deterministic. These results remain a small synthetic benchmark
and should not be generalized to production traffic.

See [`evals/results/stability_v0.2.json`](evals/results/stability_v0.2.json) for all
90 run records and their consistency signatures.
