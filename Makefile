# ATLAS-0 — standard targets. The verify-* targets define completion.
# scripts/verify-*.sh are PROTECTED (CODEOWNERS): the agent does not edit them.
.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash

help: ## list targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

# --- dev ---
lint: ## lint + format check
	@bash scripts/lint.sh
typecheck: ## strict type checking
	@bash scripts/typecheck.sh
test: ## unit + contract + integration tests (no live LLM)
	@bash scripts/test.sh
scan-secrets: ## secret detection
	@bash scripts/scan_secrets.sh

# --- evaluations ---
eval-smoke: ## quick subset (<3 min), used in PR CI
	@bash scripts/eval.sh --smoke
eval: ## full suite (<20 min), HTML report + diff + per language
	@bash scripts/eval.sh --full

# --- demo ---
demo: ## run a complete RAG+web+escalation scenario
	@bash scripts/demo.sh

# --- milestones (each calls its protected script) ---
verify-m0: ## verifiable framework
	@PYTHONPATH=services/edge-bff python3 scripts/verify_m0.py
verify-m1: ## reproducible GPU infrastructure + gateway
	@bash scripts/verify-m1.sh
verify-m2: ## ingestion + RAG
	@bash scripts/verify-m2.sh
verify-m3: ## harness + web tools
	@bash scripts/verify-m3.sh
verify-m4: ## cascade + UI + escalation
	@bash scripts/verify-m4.sh
verify-m5: ## full evaluations + telemetry
	@bash scripts/verify-m5.sh
verify-m6: ## hardening + benchmark + report
	@bash scripts/verify-m6.sh

.PHONY: help lint typecheck test scan-secrets eval-smoke eval demo \
        verify-m0 verify-m1 verify-m2 verify-m3 verify-m4 verify-m5 verify-m6

.PHONY: ingest
ingest: ## ingest corpus through gateway into Postgres/pgvector
	@python3 -m services.retrieval.pipeline

.PHONY: eval-retrieval
eval-retrieval: ## evaluate E1 and resolve citations in generated answers
	@python3 -m services.retrieval.evaluate

.PHONY: test-web-security test-budgets eval-tools eval-web
test-web-security: ## SSRF, robots, HTTP body and limit checks
	@PYTHONPATH=.:tests python3 -m unittest test_web_security test_web_transport test_tool_runtime test_budgets.BudgetTests.test_fetch_limit -v
test-budgets: ## four hard budgets and cancellation of active calls
	@PYTHONPATH=.:tests python3 -m unittest test_budgets -v
eval-tools: ## live E4 with errors injected only in the test harness
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/agent_gate_eval.py tools
eval-web: ## end-to-end E6; quality subject to human answer keys and judge
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/agent_gate_eval.py web

.PHONY: eval-routing test-fallback
eval-routing: ## E8: confusion matrix and critical under-routing
	@PYTHONPATH=.:services/model-gateway python3 tests/routing_eval.py
test-fallback: ## closed local endpoint then replay of the recorded Scaleway response
	@PYTHONPATH=services/model-gateway:tests python3 -m unittest test_cascade -v

.PHONY: report bench
report: ## GO/NO-GO report from archived measurements
	@python3 -m evals.decision
bench: ## bounded serverless microbenchmark, no GPU
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/m6_bench.py

.PHONY: serve test-ui verify-m7
serve: ## local UI, CPU gateway, retrieval and adapter; GPU disabled by default
	@bash scripts/serve.sh
test-ui: ## integrated HTTP request with citation and log, recorded provider
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/ui_gate.py 2>&1
verify-m7: ## protected UI and interaction verifier
	@bash scripts/ui_gate.sh

.PHONY: test-serverless verify-m8
test-serverless: ## live M8 probes or explicit replay, no GPU
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/serverless_gate.py
verify-m8: ## protected serverless verifier
	@bash scripts/verify-m8.sh
.PHONY: test-memory verify-m9
test-memory: ## integrated memory and project tests on synthetic data
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/memory_gate.py
verify-m9: ## persistent memory, isolation and erasure
	@bash scripts/verify-m9.sh
.PHONY: test-large-input verify-m10
test-large-input: ## large public sources: real retrieval and recorded provider
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/large_input_gate.py 2>&1
verify-m10: ## sourced large inputs within budgets
	@bash scripts/verify-m10.sh
.PHONY: test-streaming verify-m11
test-streaming:
	@PYTHONPATH=.:services/model-gateway ATLAS_STREAM_REPORT=BRAIN/eval/streaming.json python3 -m unittest discover -s tests -p 'test_stream*.py' 2>&1
verify-m11:
	@bash scripts/verify-m11.sh
.PHONY: test-vision verify-m12
test-vision: ## M12: adapter/gateway HTTP, recorded or live vision transport
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/vision_gate.py
verify-m12: ## protected multimodal verifier
	@bash scripts/verify-m12.sh

.PHONY: test-imagegen verify-m13
test-imagegen: ## real GPU generation and shutdown at end of cycle
	@timeout 1500 bash infra/imagegen.sh 2>&1
verify-m13: ## protected sovereign generation gate
	@bash scripts/verify-m13.sh

.PHONY: serve-imagegen
serve-imagegen: ## enable GPU for chat for ten minutes after loading
	@timeout 2400 bash infra/imagegen.sh serve 2>&1

.PHONY: test-mcp verify-m14
test-mcp: ## live MCP or explicit replay of recorded synthetic exchanges
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/mcp_gate.py
verify-m14: ## read and confirmed write, protected gate unchanged
	@bash scripts/verify-m14.sh

.PHONY: serve-devapi
serve-devapi: ## local developer API, individual keys and quotas; no GPU
	@bash scripts/serve-devapi.sh

.PHONY: verify-m17 test-journeys
verify-m17: ## integration journeys through the public chat API
	@bash scripts/verify-m17.sh
test-journeys: ## recorded external exchanges through the real HTTP stack
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/journey_runner.py

.PHONY: test-devapi verify-m15
test-devapi: ## developer API: real local HTTP and recorded provider streams
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/devapi_gate.py
verify-m15: ## protected auth/quotas/tools/streaming/context/cost gate
	@bash scripts/verify-m15.sh

.PHONY: test-devapi-e2e
test-devapi-e2e: ## live provider: function then answer and useful context >=24KiB
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/devapi_live.py

.PHONY: test-devapi-clients
test-devapi-clients: ## isolated native Codex/Claude, live provider and external assertions
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/devapi_clients.py

.PHONY: verify-m16
verify-m16: ## English localization and preservation of multilingual evaluation data
	@bash scripts/verify-m16.sh

.PHONY: test-serve-idempotent
test-serve-idempotent: ## explicit disruptive J8, outside the CI gate
	@PYTHONPATH=.:tests:services/model-gateway python3 tests/serve_idempotent.py

.PHONY: verify-m18
verify-m18: ## conversation integrity and image fidelity
	@bash scripts/verify-m18.sh

.PHONY: verify-m19
verify-m19: ## explicit intent, language and permanent settings locks
	@bash scripts/verify-m19.sh

.PHONY: verify-m20
verify-m20: ## real-usage journeys, honest failures and unified startup
	@PYTHONPATH=.:tests:services/model-gateway python3 -m unittest tests.test_service_delivery
	@bash scripts/verify-m20.sh

.PHONY: verify-m21
verify-m21: ## reasoning, expert evidence and native activity
	@bash scripts/verify-m21.sh

.PHONY: verify-m22
verify-m22: ## search discipline, graded vision and narration
	@bash scripts/verify-m22.sh

.PHONY: verify-m23 test-m23
verify-m23: ## readable rendering, English code and complete document attachments
	@bash scripts/verify-m23.sh
	@$(MAKE) test-m23
test-m23: ## exact recorded public HTTP journeys J39-J41
	@if [[ "$${GITHUB_ACTIONS:-}" == true && "$${M23_LIVE:-}" != 1 ]]; then \
	  PYTHONPATH=.:tests:services/model-gateway python3 tests/journey_runner.py; \
	else \
	  PYTHONPATH=.:tests:services/model-gateway python3 tests/m23_gate.py; \
	fi

.PHONY: verify-m24
verify-m24: ## documents, contained file tools and actual rendering
	@bash scripts/verify-m24.sh

.PHONY: verify-m25
verify-m25: ## general disposition and model-window conversation limits
	@bash scripts/verify-m25.sh

.PHONY: verify-m26
verify-m26: ## live acceptance proved, attachment path, retrieved sources
	@bash scripts/verify-m26.sh

.PHONY: m26-live
m26-live: ## fresh public HTTP acceptance with real providers
	@bash scripts/m26-live.sh

.PHONY: project-install
project-install: ## provision project organization/admin and explicit legacy ownership
	@python3 -m services.retrieval.project_install $(PROJECT_INSTALL_ARGS)
