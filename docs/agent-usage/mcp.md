# MiroFish MCP Server

The MCP server exposes MiroFish lifecycle tools, not a Graphiti proxy.

Start:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend
uv run mirofish-mcp
```

Tools:

- `mirofish_create_run`
- `mirofish_run`
- `mirofish_resume_run`
- `mirofish_get_status`
- `mirofish_get_current_stage`
- `mirofish_update_simulation_settings`
- `mirofish_approve_stage`
- `mirofish_reject_stage`
- `mirofish_rerun_stage`
- `mirofish_list_requests`
- `mirofish_get_request`
- `mirofish_submit_response`
- `mirofish_validate_response`
- `mirofish_build_graph`
- `mirofish_search_graph`
- `mirofish_export_graph`
- `mirofish_start_simulation`
- `mirofish_resume_simulation`
- `mirofish_generate_report`
- `mirofish_get_report`
- `mirofish_ask_followup_question`
- `mirofish_get_followup_answer`
- `mirofish_list_artifacts`
- `mirofish_doctor`

## Interaction Tools

After a run completes, these tools let you interact with agents through the queue:

- `mirofish_generate_web_console` — generates an interactive HTML console at `runs/<run_id>/artifacts/web/index.html`.
- `mirofish_list_agents` — lists all agent profiles from a completed run.
- `mirofish_get_agent` — returns a single agent's profile.
- `mirofish_ask_agent` — sends a question to a specific agent via `agent_queue`. Returns `need_agent_response` with a `request_id`.
- `mirofish_get_agent_answer` — after the desktop agent writes the response file, call this to validate, persist, and retrieve the answer.
- `mirofish_send_questionnaire` — sends a batch questionnaire to all agents. `questions_json` is a JSON string: `'[{"question_id":"q1","question":"Biggest risk?"}, ...]'`.
- `mirofish_get_questionnaire_result` — retrieves questionnaire answers and summary.
- `mirofish_ask_report_question` — asks a question about the report via `agent_queue`.
- `mirofish_get_report_question_answer` — retrieves and persists a report question answer.

### Web Console

The Web Console is a static HTML page with embedded run data plus live API interaction when the Flask backend is running.

1. Generate the console:
   ```bash
   uv run mirofish-agent web generate --run ../runs/chip-2036 --json
   ```
   Or via MCP: call `mirofish_generate_web_console`.

2. Open the generated file: `runs/<run_id>/artifacts/web/index.html`

3. Start the Flask backend for interactive features:
   ```bash
   cd /Users/leaf/Documents/future/MiroFish/backend
   uv run flask --app app run --port 5001
   ```

4. The console auto-detects the API at `http://localhost:5001`. You can change the base URL in the sidebar.

When the API is offline, the console falls back to displaying embedded static data from the run artifacts.

### Agent Q&A Flow

1. Call `mirofish_ask_agent(run, agent_id, question)` — returns `request_id`.
2. A desktop agent reads `runs/<run_id>/requests/<request_id>.json` and writes `runs/<run_id>/responses/<request_id>.json`.
3. Call `mirofish_get_agent_answer(run, request_id)` to validate the response and persist it to `artifacts/interactions/agent_questions/`.

### Questionnaire Flow

1. Call `mirofish_send_questionnaire(run, questions_json)` with a JSON array of `{question_id, question}` objects.
2. Each agent gets a separate `agent_queue` request per question.
3. Call `mirofish_get_questionnaire_result(run, questionnaire_id)` to collect answers and summary.

## Staged Mode

Use staged mode when a desktop agent should mirror the original MiroFish step-by-step UI flow. The simulation round count is a hard MCP field, not text hidden in the requirement.

Typical Qoder/Codex/Claude Code sequence:

1. Call `mirofish_doctor`.
2. Call `mirofish_create_run` with `mode="staged"`, `rounds=10`, `round_unit="year"`, and the seed/requirement/output path.
3. Call `mirofish_get_current_stage` and show the user the stage summary.
4. After user confirmation, call `mirofish_approve_stage`.
5. Call `mirofish_resume_run`; staged mode advances only to the next pause point or `need_agent_response`.
6. When `need_agent_response` appears, read `request_file`, write the response JSON, call `mirofish_validate_response`, then `mirofish_submit_response`.
7. Repeat resume/approve until `report.md`, `verdict.json`, `timeline.json`, and `graph_snapshot.json` exist.
8. Use `mirofish_ask_followup_question` for post-report questions.

Example `mirofish_create_run` arguments:

```json
{
  "seed": "/Users/leaf/Documents/future/MiroFish/seeds/chip.md",
  "requirement": "预测未来10年全球芯片能力格局变化",
  "output": "/Users/leaf/Documents/future/MiroFish/runs/chip-2036",
  "mode": "staged",
  "rounds": 10,
  "round_unit": "year",
  "minutes_per_round": 525600,
  "pause_each_round": false,
  "agent_count": 5,
  "simulation_name": "chip-2036"
}
```

If the user changes hard parameters before approval, call `mirofish_update_simulation_settings`. The engine marks dependent stages stale/pending so old profile/config/simulation/report outputs are not silently reused.

Example MCP server config:

```json
{
  "mcpServers": {
    "mirofish": {
      "command": "uv",
      "args": ["run", "mirofish-mcp"],
      "cwd": "/Users/leaf/Documents/future/MiroFish/backend",
      "env": {
        "MIROFISH_MODE": "agent",
        "MIROFISH_LLM_PROVIDER": "agent_queue",
        "MIROFISH_GRAPH_PROVIDER": "graphiti",
        "MIROFISH_RUNS_DIR": "./runs"
      }
    }
  }
}
```

Reference: https://modelcontextprotocol.github.io/python-sdk/server/
