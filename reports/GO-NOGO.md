# Decision: NO-GO

Report generated from archived evaluations and measurements, with no simulated values.
Sources: reports/bench.json.gz, reports/demo.json.gz; evaluation and calibration: make eval (see reports/M5.md).

| Criterion | Measurement | Target | Proven criterion | Provenance / limit |
|---|---:|---|---|---|
| P1 | 0.2735169830120867 | < 2.0 | yes | serverless TTFT p95, seconds |
| P2 | 124.14582000928351 | > 30.0 | yes | serverless decoding p95, tok/s |
| P3 | 4.90906582500611 | < 12.0 | no | one live request, seconds; not a load p95 |
| P4 | 3.572405473998515 | < 30.0 | no | one live request, seconds; not a load p95 |
| P5 | 0.20360010414507257 | ≤ 0.2 | no | maximum mean degradation, two waves of eight |
| P6 | 10 tools / 120 s / 0,05 EUR | hard-budget tests | yes | recorded make test-budgets |
| P7 | unavailable | > 0.5 | no | local vLLM cache; no M6 GPU |
| P8 | unavailable | < 0.02 | no | no amortized GPU; serverless bench + demo average, excluding diagnostics: 0.00077778 EUR |
| P9 | unavailable | > 0.97 | no | two-week uptime, no history |

## Quality

| Suite | Normalized score |
|---|---:|
| E1 | 0.9750 |
| E2 | 1.0000 |
| E3 | 1.0000 |
| E4 | 0.9000 |
| E5 | 1.0000 |
| E6 | 0.8800 |
| E7 | 1.0000 |
| E8 | 1.0000 |
| E9 | 0.8400 |

Cross-calibration: κ=0.7872. M5 quality GO: False.

The micro-benchmark measures serverless, not the local GPU. RAG/web journeys are
single observations, not a load distribution. CI replay durations are not inference times.

Retained gaps: incorrect E2-30 citation, expired E6-004 web source, imprecise E9-004
terminology, language gaps. The agentic RAG adapter truncates at 400 bytes and can mask
an answer that is present.

Actions before GO: correct these gaps, measure cache and amortized GPU cost, perform
representative RAG/web load testing, and collect fourteen days of uptime. A human phase-1
decision is required.
