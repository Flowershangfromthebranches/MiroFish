# MiroFish Agent Kit

MiroFish now includes a full agent engine for desktop tools such as Codex, Claude Code, Cursor, and opencode.

Default agent mode:

```bash
export MIROFISH_MODE=agent
export MIROFISH_LLM_PROVIDER=agent_queue
export MIROFISH_GRAPH_PROVIDER=graphiti
export MIROFISH_RUNS_DIR=./runs
```

Agent mode does not require `LLM_API_KEY`, `OPENAI_API_KEY`, or `ZEP_API_KEY`. Model work is written to `runs/<run_id>/requests/*.json`; a desktop agent writes matching response JSON into `runs/<run_id>/responses/*.json`; MiroFish validates the response and resumes the run.

Local graph service setup:

```bash
cd /Users/leaf/Documents/future/MiroFish
bash scripts/setup_agent_deps.sh --neo4j desktop
```

Agent dependencies are installed through the backend `agent` optional extra. Legacy OpenAI-compatible and Zep Cloud SDKs are not part of the default agent path; install them only when explicitly using `MIROFISH_LLM_PROVIDER=openai_compatible` or `MIROFISH_GRAPH_PROVIDER=zep`:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend
uv sync --extra legacy
```

Neo4j Desktop, Homebrew/native Neo4j, and existing Neo4j instances are supported. Docker Compose is optional only. `mirofish-agent doctor --json` does not fail because Docker is missing; it fails when required Graphiti/Neo4j dependencies, Neo4j connectivity, or Neo4j `5.26+` are unavailable. Ollama checks are required only when `MIROFISH_GRAPH_SEARCH_MODE=semantic` or `hybrid` and `MIROFISH_EMBEDDING_PROVIDER=ollama`; `fulltext` search with `MIROFISH_EMBEDDING_PROVIDER=none` does not require Ollama.

Minimal demo:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend
uv run mirofish-agent init --seed /path/to/seed.md --requirement "预测未来10年全球芯片能力格局变化" --output ../runs/chip-2036
uv run mirofish-agent run --run ../runs/chip-2036 --json
uv run mirofish-agent requests show --run ../runs/chip-2036 --request-id req_000001 --json
uv run mirofish-agent responses validate --run ../runs/chip-2036 --response ../runs/chip-2036/responses/req_000001.json --json
uv run mirofish-agent resume --run ../runs/chip-2036 --json
```

Follow-up questions also use the same provider boundary and queue:

```bash
uv run mirofish-agent followup ask --run ../runs/chip-2036 --question "先进AI芯片出口限制有什么影响?" --json
uv run mirofish-agent followup show --run ../runs/chip-2036 --request-id req_000007 --json
```

Supported agent queue task types:

```text
extract_triples
generate_ontology
generate_oasis_profiles
generate_simulation_config
simulate_agent_action
summarize_round
update_memory
generate_report
answer_followup_question
validate_json_output
repair_invalid_json
```

Full smoke:

```bash
cd /Users/leaf/Documents/future/MiroFish
bash scripts/smoke_agent_queue_full.sh
bash scripts/smoke_mcp_full.sh
python scripts/check_provider_boundaries.py
```

Tool-specific guides:

- [Codex](./docs/agent-usage/codex.md)
- [Claude Code](./docs/agent-usage/claude-code.md)
- [Cursor](./docs/agent-usage/cursor.md)
- [opencode](./docs/agent-usage/opencode.md)
- [MCP](./docs/agent-usage/mcp.md)

The Flask/Vue UI is preserved as the legacy interactive interface. Legacy API calls that hit `agent_queue` return a structured HTTP 202 `need_agent_response` payload instead of a 500, but full checkpointed agent-mode orchestration remains CLI/MCP-first.
