# 16. Models and inference inventory

Status: as of 2026-10-21. Source of truth for identifiers is the environment (`.env`, `stable.env`) and `services/model-gateway/routing.yaml`; this page is a reading aid, not a configuration file. Prices come from `services/model-gateway/pricing.yaml` (Scaleway pages consulted 2026-09-09 to 2026-09-17) and were not re-checked on the date above.

## Inventory

"On demand" means a pay-per-token serverless API (Scaleway Generative APIs, Europe). "Dedicated" means a rented machine billed by the hour.

| Model | Use | Inference service | Cost |
| --- | --- | --- | --- |
| `qwen3.5-397b-a17b` | Main chat (public profile `atlas-qwen`), reasoning, escalation | On demand, Scaleway Generative APIs | 0.60 EUR input / 3.60 EUR output per million tokens |
| `glm-5.2` | Code (profile `atlas-glm`), Code mode, evaluation judge | On demand | 1.80 EUR / 5.50 EUR |
| `deepseek-v4-flash-0731` | Fast answers (profile `atlas-deepseek`) | On demand | 0.40 EUR / 0.80 EUR |
| `gemma-4-26b-a4b-it` (fallback `pixtral-12b-2409`) | Reading attached images | On demand | 0.25 / 0.50 EUR; 0.20 / 0.20 EUR |
| `lightonocr-3-0.8b` | OCR of scanned PDF pages | On demand | see `pricing.yaml` |
| `qwen3-embedding-8b` | Retrieval over attached documents | On demand | 0.10 EUR input per million tokens |
| FLUX.1 schnell (`unsloth/FLUX.1-schnell`, pinned revision) | Realistic image generation | Dedicated: Scaleway Instance `atlas-gpu`, type L4-1-24G, zone fr-par-2, started on demand, weights on a persistent 200 GB block volume | about 0.79 EUR per hour while running, plus storage; at most 0.05 EUR per image |
| Qwen3-30B-A3B (`LOCAL_MODEL`) | Optional local engine for simple answers | Dedicated GPU, configured in `routing.yaml`, not active | no per-token cost, GPU billed hourly |
| Whisper small (`Systran/faster-whisper-small`) | Dictation (fr, en, de, es, it) | On the main VM, CPU | included in the VM |
| Piper voices (fr_FR-siwis, en_US-ljspeech, de_DE-thorsten, es_ES-sharvard, it_IT-serena) | Read aloud, offline | On the main VM, CPU | included in the VM |
| `paraphrase-multilingual-MiniLM-L12-v2` and `mmarco-mMiniLMv2-L12-H384-v1` | Corpus embeddings and reranking | On the main VM, CPU | included in the VM |

Text-drawn SVG illustrations are written by the chat model; they have no model of their own.

## Dedicated GPU

- Where: Scaleway console, the Atlas project, zone `fr-par-2`, product Instances, server `atlas-gpu`. It exists only while an image window is open; the launcher `infra/imagegen.sh` or the window script destroys it at the end. The root disk is destroyed with it; the weights volume stays.
- Capacity: GPU instance types are often in shortage in `fr-par-1`, `fr-par-2` and `pl-waw-2`; creation can succeed and leave the server stopped. The window script retries until its deadline.
- Cost control: a window is bounded by a deadline; the launcher reserves its worst case; the per-image cap is 0.05 EUR.

## Why image generation is not on demand

As of 2026-10-20 the Scaleway Generative APIs catalog lists text, code, vision, OCR, embedding and speech models but no image generation model, so the only Scaleway path is a dedicated GPU. Alternatives that were looked at and not adopted:

- Black Forest Labs API (the authors of FLUX): hosted, pay per image, with an advertised EU endpoint and data residency; sources are vendor pages and directories, to be verified before any adoption. It would add a non-Scaleway processor to the execution path, which is a sovereignty decision for the owner.
- OVHcloud Public Cloud GPU instances (L4, L40S, H100, hourly): same model as Scaleway, a second supplier against shortages; a GPU quota request is likely needed first.
- Spheron (GPU marketplace) and Hetzner dedicated GPU servers: considered and not retained (provider location unclear; monthly billing).

## Sovereignty note

The authors of the language models are not European (Alibaba, Zhipu, DeepSeek, Google, OpenAI); they are served by a European provider, and no US AI provider is called in the execution path. Web search (SerpAPI, Tavily) remains the known exception.
