# Implementation Summary

## Changed Areas

- Added `backend/app/agent_engine/` for strict request/response schemas, filesystem queue, persistent run state, output contracts, shared runner, and CLI.
- Added `backend/app/adapters/llm/` with `AgentRuntime`, `agent_queue`, `mock`, legacy `openai_compatible`, and `AgentModelBackendAdapter`.
- Added `backend/app/adapters/graph/` with `GraphProvider`, `GraphitiGraphProvider`, `GraphitiCompatibilityStore`, and legacy `ZepGraphProvider`.
- Added `backend/app/mcp_server/server.py` with FastMCP lifecycle tools.
- Refactored direct model/Zep call sites in LLM client, graph builder, Zep entity/tools/memory facades, OASIS profile/config generators, and simulation scripts.
- Tightened agent response validation so `skipped` outputs still match `expected_schema`, `req_*.json` filenames must match response `request_id`, and invalid stage responses create `repair_invalid_json` requests when retry policy allows.
- Added stable output schemas for every declared agent task type: `extract_triples`, `generate_ontology`, `generate_oasis_profiles`, `generate_simulation_config`, `simulate_agent_action`, `summarize_round`, `update_memory`, `generate_report`, `answer_followup_question`, `validate_json_output`, and `repair_invalid_json`.
- Persisted graph provider overrides in run metadata so `graph build --provider graphiti` survives the `waiting_agent` pause/resume boundary.
- Made explicit stage entrypoints idempotent while waiting for agent responses: repeated `graph build`, `simulate start`, and `report generate` calls return the existing request instead of creating duplicates.
- Made `responses submit` attach generated `repair_invalid_json` requests to the current waiting stage, so later `resume` reuses the repair request instead of creating duplicates.
- Extended `GraphitiCompatibilityStore` Neo4j mode so episodes, agent memory, snapshot export/import, and timeline data stay in the Neo4j-backed compatibility layer instead of falling back to file storage.
- Aligned `GraphitiCompatibilityStore` default behavior with production `doctor`: `MIROFISH_GRAPHITI_STORE=auto` uses Neo4j, and offline file storage requires explicit `MIROFISH_GRAPHITI_STORE=file`.
- Added CLI and MCP follow-up Q&A through `answer_followup_question`, with GraphProvider retrieval context and queue-validated responses.
- Added staged workflow support alongside the existing auto workflow. Staged runs pause after `seed_input`, `prediction_requirement`, `simulation_settings`, `graph_build`, `profile_and_config`, and `simulation_run` until the user approves, rejects, updates settings, or reruns a stage.
- Added hard simulation settings to CLI/MCP run creation: `rounds`, `round_unit`, `minutes_per_round`, `pause_each_round`, `agent_count`, and `simulation_name`. `rounds` is persisted in `state.json` and no longer depends on natural-language requirement parsing.
- Added staged CLI commands under `mirofish-agent stage ...` and matching MCP tools for current-stage inspection, settings updates, stage approval/rejection, and reruns.
- Exposed `AgentModelBackendAdapter.last_need_agent_response` so OASIS/CAMEL runtime calls that hit `agent_queue` can be detected by orchestration code even when CAMEL expects a `ChatCompletion` object.
- Added a Flask `NeedAgentResponse` handler so legacy API calls return HTTP 202 structured `need_agent_response` payloads instead of generic 500s.
- Removed API-key default reads from legacy business facades; provider keys are now validated only inside legacy providers or provider-aware Flask guards.
- Moved OpenAI-compatible and Zep Cloud SDK packages out of default backend dependencies and into the optional `legacy` extra, so default agent installs do not require legacy SDKs.
- Strengthened `scripts/check_provider_boundaries.py` to reject direct legacy provider imports and Graphiti/Neo4j schema assumptions outside `GraphitiCompatibilityStore`.
- Changed ontology Python code generation to use provider-neutral Pydantic base classes instead of emitting graph SDK import strings.
- Added tests under `backend/tests/` and smoke/static scripts under `scripts/`.
- Added `docker-compose.agent.yml` and `scripts/setup_agent_deps.sh` for local agent dependencies and services.
- Updated `.env.example`, `AGENT_KIT.md`, README, and `docs/agent-usage/*`, including opencode usage.

## Providers

- `MIROFISH_LLM_PROVIDER=agent_queue`: writes strict request JSON and returns `need_agent_response`.
- `MIROFISH_LLM_PROVIDER=mock`: deterministic offline provider for tests.
- `MIROFISH_LLM_PROVIDER=openai_compatible`: legacy provider; the OpenAI SDK import is isolated to `backend/app/adapters/llm/openai_compatible.py`.
- `MIROFISH_GRAPH_PROVIDER=graphiti`: default agent graph provider.
- `MIROFISH_GRAPH_PROVIDER=zep`: legacy provider; Zep SDK imports are isolated to `backend/app/adapters/graph/zep.py`.

## Dependencies And Local Services

- Graphiti source code is not copied, cloned, or vendored into this repository.
- Graphiti is installed through backend dependency management with the optional `agent` extra: `uv sync --extra agent --group dev`.
- The `agent` extra includes `graphiti-core`, `neo4j`, and `mcp`.
- Legacy OpenAI-compatible and Zep Cloud SDKs are isolated in the optional `legacy` extra: `uv sync --extra legacy`.
- Neo4j is an external local graph database service. Supported non-Docker setup paths are Neo4j Desktop, Homebrew/native install, or an existing Neo4j 5.26+ instance.
- Docker Compose remains available through `docker-compose.agent.yml`, but Docker and Docker Compose are optional and are not required by `doctor`.
- Ollama is conditional. `doctor` only hard-fails Ollama checks when `MIROFISH_GRAPH_SEARCH_MODE=semantic` or `hybrid` and `MIROFISH_EMBEDDING_PROVIDER=ollama`.
- MiroFish only maintains `GraphitiGraphProvider` and `GraphitiCompatibilityStore`; all Graphiti/Neo4j schema assumptions are isolated there.

## Minimal Demo

```bash
cd /Users/leaf/Documents/future/MiroFish
bash scripts/smoke_agent_queue_full.sh
```

Manual CLI:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend
uv run mirofish-agent init --seed /path/to/seed.md --requirement "预测未来10年全球芯片能力格局变化" --output ../runs/chip-2036 --json
uv run mirofish-agent run --run ../runs/chip-2036 --json
```

Staged CLI:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend
uv run mirofish-agent create-run \
  --seed /path/to/seed.md \
  --requirement "预测未来10年全球芯片能力格局变化" \
  --output ../runs/chip-2036 \
  --mode staged \
  --rounds 10 \
  --round-unit year \
  --json
uv run mirofish-agent stage status --run ../runs/chip-2036 --json
uv run mirofish-agent stage approve --run ../runs/chip-2036 --json
uv run mirofish-agent resume --run ../runs/chip-2036 --json
```

When `need_agent_response` is returned, write the response file and run:

```bash
uv run mirofish-agent responses validate --run ../runs/chip-2036 --response ../runs/chip-2036/responses/req_000001.json --json
uv run mirofish-agent resume --run ../runs/chip-2036 --json
```

## Codex Triple Extraction

Use this prompt when processing an `extract_triples` request:

```text
读取 request_file，严格按照 expected_schema 生成 response_file。只输出 JSON，不要添加解释。每个 triple 必须包含 subject、predicate、object、fact、evidence、confidence。不要编造现实种子中没有的事实。无法确认的关系不要写入，或将 confidence 降低。
```

## Agent Engine Coverage

Implemented full CLI/MCP lifecycle for:

- ontology request
- triple extraction request
- profile request
- simulation config request
- batched simulation action request
- report request
- follow-up Q&A request
- `report.md`, `verdict.json`, `timeline.json`, `graph_snapshot.json`

Staged workflow maps to the original UI-style process:

- `seed_input`: saves and summarizes `seed.md`.
- `prediction_requirement`: records the prediction target.
- `simulation_settings`: records hard settings such as `rounds=10` and `round_unit=year`.
- `graph_build`: runs ontology and triple extraction requests, then writes Graphiti/Neo4j.
- `profile_and_config`: generates profiles and simulation config; config rounds are forcibly taken from `simulation_settings`.
- `simulation_run`: runs the configured number of rounds and updates simulation progress.
- `report_generation`: writes report artifacts and records actual rounds in `verdict.json` and `timeline.json`.
- `followup_question`: uses AgentRuntime plus GraphProvider retrieval.

Auto mode remains available for scripts and smoke tests. Staged mode is intended for QoderWork, Codex, Claude Code, Cursor, and other desktop agents that need user confirmation between phases.

Legacy Flask/Vue UI is preserved but not fully upgraded to drive every checkpointed `agent_queue` stage. Legacy API calls return structured HTTP 202 `need_agent_response` payloads when model work needs an external agent. Full agent-mode orchestration is CLI/MCP-first.

## Graphiti Limits

`GraphitiCompatibilityStore` is a version-sensitive compatibility layer. It provides a no-LLM triplet write path and hides all Neo4j/Cypher/schema assumptions from business code. When Graphiti’s public fact triple API is available and stable in the installed version, this layer can be adapted internally without changing `GraphProvider`.

Offline tests use explicit `MIROFISH_GRAPHITI_STORE=file`. Production Graphiti/Neo4j mode uses `MIROFISH_GRAPHITI_STORE=auto` or `neo4j` with `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, and `NEO4J_DATABASE`.

## Verification Results

Latest local verification:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend && uv run pytest -q
# 46 passed, 414 warnings

cd /Users/leaf/Documents/future/MiroFish && bash scripts/smoke_agent_queue_full.sh
# CLI full agent_queue smoke passed, including follow-up Q&A

cd /Users/leaf/Documents/future/MiroFish && bash scripts/smoke_agent_queue_staged.sh
# CLI staged agent_queue smoke passed, including stage approvals and rounds=10 artifacts

cd /Users/leaf/Documents/future/MiroFish && bash scripts/smoke_mcp_full.sh
# MCP lifecycle smoke passed, including staged tool/schema checks, request validation, graph search/export, doctor, artifacts, and follow-up Q&A

cd /Users/leaf/Documents/future/MiroFish && python scripts/check_provider_boundaries.py
# Provider boundary check passed

cd /Users/leaf/Documents/future/MiroFish && bash -n scripts/setup_agent_deps.sh
# script syntax check passed

cd /Users/leaf/Documents/future/MiroFish/backend && uv run mirofish-agent doctor --json
# status: ok; Neo4j 2025.04.0 connectable; hard_failures: []

# Live Graphiti/Neo4j provider smoke:
# add_triples/search/export_snapshot/clear_run_graph passed with MIROFISH_GRAPHITI_STORE=auto.

# Live CLI Graphiti/Neo4j smoke:
# CLI Neo4j agent_queue smoke passed with graph search and final artifacts.
```

Frontend build must be run from the detected frontend package directory:

```bash
cd /Users/leaf/Documents/future/MiroFish/frontend && npm run build
# built successfully; Vite reported only chunk-size/dynamic-import warnings
```

Doctor result in the current shell:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend && uv run mirofish-agent doctor --json
# status: ok
# graphiti_package: ok
# mcp_package: ok
# neo4j_package: ok
# neo4j_connectable: ok, bolt://localhost:7687
# neo4j_version_supported: ok, 2025.04.0
# docker: optional warning only when Docker is not installed
# docker_compose: optional warning only when Docker Compose is not installed
# graph_search_mode: fulltext
# embedding_provider: none
# ollama_connectable: optional; fulltext search does not require Ollama
# ollama_embedding_model: optional
# hard_failures: []
```

Setup helper result in the current shell:

```bash
cd /Users/leaf/Documents/future/MiroFish && bash scripts/setup_agent_deps.sh --neo4j desktop --skip-services
# Python agent dependencies installed/audited
# warning: Docker optional, skipped
# ok: Neo4j version 2025.04.0
# warning: Ollama optional, skipped because MIROFISH_GRAPH_SEARCH_MODE=fulltext uses no semantic embedding
# ok: agent dependency and required service checks completed
```

This means the offline no-LLM compatibility path and the production Graphiti/Neo4j path are both verified. Docker is not required. Ollama is only required for semantic/hybrid graph search when `MIROFISH_EMBEDDING_PROVIDER=ollama`.
