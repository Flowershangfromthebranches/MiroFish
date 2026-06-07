# Codex Desktop Usage

Open `/Users/leaf/Documents/future/MiroFish` in Codex Desktop.

Initialize a run:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend
uv run mirofish-agent init --seed /path/to/seed.md --requirement "预测未来10年全球芯片能力格局变化" --output ../runs/chip-2036 --json
uv run mirofish-agent run --run ../runs/chip-2036 --json
```

When the CLI returns `need_agent_response`, read `request_file`, generate the response JSON, and write it to `expected_response_file`.

Codex triple extraction prompt:

```text
读取 request_file，严格按照 expected_schema 生成 response_file。只输出 JSON，不要添加解释。每个 triple 必须包含 subject、predicate、object、fact、evidence、confidence。不要编造现实种子中没有的事实。无法确认的关系不要写入，或将 confidence 降低。
```

Validate and continue:

```bash
uv run mirofish-agent responses validate --run ../runs/chip-2036 --response ../runs/chip-2036/responses/req_000001.json --json
uv run mirofish-agent resume --run ../runs/chip-2036 --json
```

Repeat until status is `completed`. Final artifacts are in:

```text
runs/chip-2036/artifacts/report.md
runs/chip-2036/artifacts/verdict.json
runs/chip-2036/artifacts/timeline.json
runs/chip-2036/artifacts/graph_snapshot.json
```

Ask a follow-up question after a completed run:

```bash
uv run mirofish-agent followup ask --run ../runs/chip-2036 --question "先进AI芯片出口限制有什么影响?" --json
uv run mirofish-agent requests show --run ../runs/chip-2036 --request-id req_000007 --json
uv run mirofish-agent followup show --run ../runs/chip-2036 --request-id req_000007 --json
```

Follow-up answers are written under `runs/chip-2036/artifacts/followups/`.

MCP config example:

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
        "MIROFISH_GRAPH_PROVIDER": "graphiti"
      }
    }
  }
}
```
