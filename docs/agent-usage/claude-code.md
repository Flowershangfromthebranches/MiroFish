# Claude Code Usage

Open the repository:

```bash
cd /Users/leaf/Documents/future/MiroFish
```

Run the CLI from the backend:

```bash
cd backend
uv run mirofish-agent init --seed /path/to/seed.md --requirement "预测未来10年全球芯片能力格局变化" --output ../runs/chip-2036 --json
uv run mirofish-agent run --run ../runs/chip-2036 --json
```

For each `need_agent_response`, inspect the request:

```bash
uv run mirofish-agent requests show --run ../runs/chip-2036 --request-id req_000001 --json
```

Write the response file exactly matching `expected_schema`, validate it, then resume:

```bash
uv run mirofish-agent responses validate --run ../runs/chip-2036 --response ../runs/chip-2036/responses/req_000001.json --json
uv run mirofish-agent resume --run ../runs/chip-2036 --json
```

Claude Code MCP config:

```json
{
  "mcpServers": {
    "mirofish": {
      "command": "uv",
      "args": ["run", "mirofish-mcp"],
      "cwd": "/Users/leaf/Documents/future/MiroFish/backend"
    }
  }
}
```
