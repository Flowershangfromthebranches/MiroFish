# Cursor Usage

Open `/Users/leaf/Documents/future/MiroFish` as the workspace.

CLI flow:

```bash
cd backend
uv run mirofish-agent init --seed /path/to/seed.md --requirement "预测未来10年全球芯片能力格局变化" --output ../runs/chip-2036 --json
uv run mirofish-agent run --run ../runs/chip-2036 --json
```

Cursor can handle request files by reading `runs/<run_id>/requests/req_*.json` and writing strict responses to `runs/<run_id>/responses/req_*.json`.

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
