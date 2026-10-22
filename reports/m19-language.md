# M19 language and settings increment

REQ-ENG-004/009/011; M19 D2–D4. The contract was merged in PR #91 after
[green CI](https://github.com/leag1234/cloclo/actions/runs/34940590635).

Chat, follow-up, project and vision requests carry explicit language instructions.
Short messages inherit conversation language, with detection and UI-locale fallback.
Web excerpt reduction preserves the model's explicit reading query. Regression
proof: the old reduction discarded the passage containing the requested year;
the new test retains it. Settings tests lock capabilities, language resources,
CI wait length, image dimensions, sequence length, seed, watchdog and model identity.

Text and vision exchanges were refreshed from the provider; unchanged image requests
reuse previously recorded real GPU outputs. One real search timeout recovered.
J12 initially exhausted tokens after failed/irrelevant page reads; the query fix
and a bounded retry completed it without raising limits. This is an intermediate
increment: tool-driven generation and J15–J20 are still pending, so M19 is incomplete.

The compact capability prompt keeps the full M10 source within the unchanged budget;
the recorder now reserves using the actual serverless configuration.
