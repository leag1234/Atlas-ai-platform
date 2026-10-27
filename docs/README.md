# Project ATLAS — Sovereign LLM Platform on Open-Weight Models

> Normative reference corpus. These documents are authoritative over the code.
> Any deviation must go through an ADR (see `11-standards-ingenierie-agents.md`).

## 0. Objective

Build a conversational and agentic platform of "frontier" quality
based on **open-weight** models, first for **internal enterprise** use,
then extensible to a multi-tenant product.

Structuring constraint: **minimal infrastructure cost for a given quality**.
The default decision is therefore *not to host GPUs unless the load justifies it*
(cf. `04-finops.md`, calculated switching threshold).

## 1. How to Read This Corpus

| Doc | Content | Audience |
|---|---|---|
| `01-exigences-et-perimetre.md` | Requirements REQ-*, NFRs, out-of-scope | All |
| `02-architecture-cible.md` | C4 view, components, flows, contracts | Arch / agents |
| `03-modeles-et-inference.md` | Model choices, serving, routing | ML Infra |
| `04-finops.md` | Cost model, thresholds, levers | Arch / management |
| `05-post-training-et-caractere.md` | SFT / DPO / charter, data | ML |
| `06-harness-agent-outils-rag.md` | Orchestrator, tools, RAG, memory | Backend |
| `07-securite-et-conformite.md` | Guardrails, GDPR, AI Act, threats | Sec / legal |
| `08-plateforme-api.md` | API, multi-tenant, quotas, data | Backend |
| `09-evaluation-qualite.md` | Evals, CI gates, regression | ML / QA |
| `10-sre-observabilite.md` | SLOs, telemetry, runbooks | SRE |
| `11-standards-ingenierie-agents.md` | Rules for implementing agents | **To be read first by any agent** |
| `12-roadmap.md` | Phases, milestones, exit criteria | Management |
| `13-poc-spec.md` | PoC ATLAS-0: scope, targets, automated validation, leased infra | All / agents |
| `14-implementation-autonome.md` | Delegated control playbook: access, milestones, checkpoints | Human pilot + agents |
| `16-models-and-inference-inventory.md` | Every model in use: purpose, serverless or dedicated inference, cost; the dedicated GPU | Arch / management |
| `18-remote-runners.md` | Remote execution targets for project Work: SSH targets, permission modes, devices | Arch / backend |

## 2. Normative Conventions (RFC 2119)

- **MUST**: Blocking. A PR violating a MUST is rejected by CI or review.
- **SHOULD**: Default; a deviation requires written justification in the PR.
- **MAY**: Implementation latitude.

Each requirement carries a stable identifier `REQ-<DOMAIN>-<n>`. Code, tests,
and tickets **MUST** reference the identifier (`// covers: REQ-INF-004`).

## 3. Guiding Principles

1. **Contracts first.** No code before the contract (OpenAPI / JSON Schema /
   protobuf) is merged. Agents generate code *from* the contract.
2. **The model is replaceable.** No component outside the `model-gateway` layer
   knows the name of a model. A provider change = a config change.
3. **Cost is a first-class concern.** Any PR touching the inference path
   declares its impact `€/1k requests` (cf. `04-finops.md`).
4. **Nothing goes to prod without eval.** The quality gate (`09`) is blocking.
5. **Determinism and reproducibility.** Seeds, pinned versions, immutable artifacts.
6. **The hardest part is not serving, it's behavior.** Budget and attention
   must be allocated accordingly: ~20% infra, ~50% harness + evals, ~30% post-training.

## 4. Explicit Anti-Goals

- Do not pre-train a base model. Ever.
- Do not build a generic homegrown agent framework. We assemble.
- Do not aim for full multimodal parity in phase 1.
- Do not optimize latency before having a measured and violated SLO.
