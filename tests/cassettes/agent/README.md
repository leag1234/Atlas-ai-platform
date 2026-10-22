# M3 provider recordings

Recorded live on 2026-09-09 through the configured Scaleway gateway, SerpApi,
public HTTP fetcher and real M2 retrieval. Only the E4 fixture-declared faults
are injected, under tests/record_agent.py. No runtime imports these fixtures.
The complete second E4 batch is preserved, including failures; no selection
between attempts. E6 includes the five human-validated cases from b95e11f.

Exact replay checks prompts, tool schemas, every request, response usage and
final result. Gzip is lossless storage for repeated contexts: inspect with
`gzip -dc tests/cassettes/agent/E4-001.json.gz | python3 -m json.tool`.
Uncompressed SHA-256 values below make the original recordings auditable.
Raw decompressed recordings were scanned for credential patterns and injected
secret values before compression. The test checks lossless loading.

E4 trace acceptance: 18/20 (90%); failures E4-012 calculator omitted and E4-020
tokens exhausted. E6 executable with retrieved URL/date: 3/5 (60%). These are
execution metrics, not an E6 quality GO or a calibrated judge result. No live
LLM call is made by PR CI; make verify-m3 replays this full batch in CI.

| Case | State | Stop reason | Uncompressed SHA-256 |
|---|---|---|---|
| E4-001 | done | — | a236b0afba4e55085c6db6fa2132f35d26f4ec127f699100782e6361391fa05b |
| E4-002 | done | — | ea3f1ccad454faed32d5d54375d3fa24df7ee6fe35747d8b9ead0a48c91afce2 |
| E4-003 | done | — | c1b55a8808952ddd2d5a4c7acfd669f092fac5a3c0f0bc031325880281cac94a |
| E4-004 | done | — | 3bd8ca4f4e382aeeded179902c8525969b3a8f7ddf84ec234a424947b4d53a8f |
| E4-005 | done | — | cf5c8dff655c9b040d0844f24aae9c25ac1ec2d89d3a2c1dfb88367e6174d770 |
| E4-006 | done | — | 8e00eee43a02eea9ce4f69ed04244b1ac013cc2133caedbd1ea9e5fb22077e1f |
| E4-007 | done | — | 533fcacf9b62bfa6b81cc88113c4f6540081708cad33d7053606754a25307ea6 |
| E4-008 | done | — | aa7a9bd4423d396b92917f66b28fa0f61c5c8d3d54d409ba5963fac43608df13 |
| E4-009 | done | — | ffc5005a3a551b5979741d7beadd9b8fbb6bf09054b73fa3ff55bc397bf75634 |
| E4-010 | done | — | 6533d103c714a9565e7844cfcf7d266edce5e56e01093c9cc88039b613f0d2e8 |
| E4-011 | done | — | 475221c19f12e43ee1a81d3cb4586cd7ee8be275270b05ba26582c3fbe7db727 |
| E4-012 | done | — | ccbbf6141431f727f64f8562b7e22ffa5175731051260c5c16e283cb46dc08be |
| E4-013 | done | — | 9b48bc2e0cbea677d3845d2a481568b36d96cfad04aa44f172f7013910424a10 |
| E4-014 | done | — | f9cd559d81944602b795260dcd7ba1d298e8a770f46746de540880c11e72ac88 |
| E4-015 | done | — | 6e51214ed97d1cc4b766b0624c9c95feb5f53e7192bcb7ea73bdf7b3eac5b780 |
| E4-016 | done | — | 742eba69a87b0a2d1860470e94e070ee432fdbc518c07456567cd6d6ddf80643 |
| E4-017 | done | — | cc115f36198af1c5ec5cc43f20e9abb6c2eaf1b874f338b70bf992af55f5428e |
| E4-018 | done | — | a2a9f1e926a12f168048703ccda978d35458a48d9436153a6c84f91c8ba804a9 |
| E4-019 | done | — | 0c74253cef8d5dd95268fd78165b6d5dfedf5b68264842c82e43799ead220b92 |
| E4-020 | stopped | tokens | 88490cfab2a74bbc7e2dcc0aa63058f55b61866ff012ae3e7a44edb9a1595568 |
| E6-001 | done | — | a25a7ced3bcd6e19e72b2be97de6b2240731b7a37b50033199320033b9092c4f |
| E6-002 | stopped | tokens | a06beb22b70cd4c4fada0e0380a8c3af2234533650e3c371ce0afe8cb670b28c |
| E6-003 | done | — | 6493b15e07da924f5e73e3f3bc5a477aa6f803065943360a9a194c67b81ea174 |
| E6-004 | done | — | bc72b84972655ee0737138638deb8ffa0de9bcd3fb73bcc4a1844a38f1830f4f |
| E6-005 | done | — | b9568dec0fbed96debb30ff100edaa974fe53c5fae94cb9ae0c7c471c0150cac |

M16 localization: `E4-020.json.gz` changes only the application-generated final stop text to English. Provider messages, tool traces, budgets and outcomes are unchanged. Original SHA-256 above; localized SHA-256: `7550614f77ab7cfef7b7a6908deef8c301f495d18bd4740b507685b01ee178ff`.
