# M5 recordings

`evaluation.json.gz` contains 193 real provider responses recorded on 2026-09-10.
Each entry retains role, exact messages, answer, model metadata and observed usage/
SSE timing. Replay requires exact messages; it never calls a provider on a miss.
Old grading rubrics/translation prompts remain for audit, not score substitution.
The selected run costs 0.30840295 EUR at configured catalogue prices; all archived
calls cost 0.35268165 EUR. One unmetered timeout reserves at most 0.05 EUR.
Two refreshed web traces cost 0.0086982 EUR. No GPU was used.

E3 grading explicitly recognizes the gateway's no-answer protocol and assesses
zero invention. E9 names target languages explicitly. Quality failures remain in
the report, including a wrong RAG citation and a stale web source. A missing cache
measurement is null. Recorded inference timing must not be confused with replay
wall time. Full measurements and limitations: `reports/M5.md`.
