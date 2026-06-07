# opencode Usage

Open the repository:

```bash
cd /Users/leaf/Documents/future/MiroFish
```

Run MiroFish through the CLI from the backend:

```bash
cd backend
uv run mirofish-agent init --seed /path/to/seed.md --requirement "预测未来10年全球芯片能力格局变化" --output ../runs/chip-2036 --json
uv run mirofish-agent run --run ../runs/chip-2036 --json
```

When a command returns `need_agent_response`, read `request_file`, produce JSON that exactly matches `expected_schema`, write it to `expected_response_file`, validate it, and resume:

```bash
uv run mirofish-agent responses validate --run ../runs/chip-2036 --response ../runs/chip-2036/responses/req_000001.json --json
uv run mirofish-agent resume --run ../runs/chip-2036 --json
```

MCP server config shape:

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

Follow-up questions use the same queue:

```bash
uv run mirofish-agent followup ask --run ../runs/chip-2036 --question "这个预测里最大的风险是什么?" --json
uv run mirofish-agent followup show --run ../runs/chip-2036 --request-id req_000007 --json
```
