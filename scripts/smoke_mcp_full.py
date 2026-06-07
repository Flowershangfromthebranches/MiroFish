#!/usr/bin/env python3
"""MCP lifecycle smoke.

This verifies the FastMCP server can be constructed and then exercises the
same lifecycle service used by MCP tools. If the MCP SDK is missing, the script
fails with a clear dependency blocker.
"""

from __future__ import annotations

import json
import os
import tempfile
import asyncio
from pathlib import Path

from write_mock_agent_response import output_for


async def call_tool(server, name: str, arguments: dict) -> dict:
    result = await server.call_tool(name, arguments)
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]["result"]
    if isinstance(result, dict):
        return result.get("result", result)
    raise RuntimeError(f"Unexpected MCP tool result for {name}: {result!r}")


async def async_main() -> int:
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        print("BLOCKER: MCP Python SDK package 'mcp' is not installed.")
        print(f"Import error: {exc}")
        return 2

    from app.mcp_server.server import create_server

    server = create_server()
    if server is None:
        print("BLOCKER: create_server returned None")
        return 2

    tmp = Path(tempfile.mkdtemp())
    os.environ["MIROFISH_MODE"] = "agent"
    os.environ["MIROFISH_LLM_PROVIDER"] = "agent_queue"
    os.environ["MIROFISH_GRAPH_PROVIDER"] = "graphiti"
    os.environ["MIROFISH_GRAPHITI_STORE"] = "file"
    os.environ["MIROFISH_GRAPHITI_COMPAT_PATH"] = str(tmp / "graphiti-store.json")
    os.environ.pop("LLM_API_KEY", None)
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("ZEP_API_KEY", None)

    seed = tmp / "seed.md"
    seed.write_text("美国商务部限制先进AI芯片出口。", encoding="utf-8")
    run_dir = tmp / "mcp-run"
    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}
    required = {
        "mirofish_create_run",
        "mirofish_run",
        "mirofish_resume_run",
        "mirofish_get_status",
        "mirofish_get_current_stage",
        "mirofish_update_simulation_settings",
        "mirofish_approve_stage",
        "mirofish_reject_stage",
        "mirofish_rerun_stage",
        "mirofish_list_requests",
        "mirofish_get_request",
        "mirofish_submit_response",
        "mirofish_validate_response",
        "mirofish_build_graph",
        "mirofish_search_graph",
        "mirofish_export_graph",
        "mirofish_start_simulation",
        "mirofish_resume_simulation",
        "mirofish_generate_report",
        "mirofish_get_report",
        "mirofish_ask_followup_question",
        "mirofish_get_followup_answer",
        "mirofish_list_artifacts",
        "mirofish_doctor",
    }
    missing = sorted(required - tool_names)
    if missing:
        print(f"BLOCKER: MCP tools missing: {missing}")
        return 2
    create_tool = next(tool for tool in tools if tool.name == "mirofish_create_run")
    create_schema = getattr(create_tool, "inputSchema", {}) or {}
    create_props = create_schema.get("properties", {})
    if "rounds" not in create_props or "mode" not in create_props:
        print(f"BLOCKER: mirofish_create_run schema does not expose rounds/mode: {create_schema}")
        return 2

    staged_dir = tmp / "mcp-staged-run"
    staged = await call_tool(
        server,
        "mirofish_create_run",
        {
            "seed": str(seed),
            "requirement": "预测未来10年全球芯片能力格局变化",
            "output": str(staged_dir),
            "mode": "staged",
            "rounds": 10,
            "round_unit": "year",
        },
    )
    assert staged["status"] == "created", staged
    assert staged["state"]["workflow_mode"] == "staged", staged
    assert staged["state"]["simulation_settings"]["rounds"] == 10, staged
    current_stage = await call_tool(server, "mirofish_get_current_stage", {"run": str(staged_dir)})
    assert current_stage["stage"]["current_stage"] == "seed_input", current_stage
    approved_stage = await call_tool(server, "mirofish_approve_stage", {"run": str(staged_dir)})
    assert approved_stage["next_stage"] == "prediction_requirement", approved_stage

    created = await call_tool(
        server,
        "mirofish_create_run",
        {"seed": str(seed), "requirement": "预测未来10年全球芯片能力格局变化", "output": str(run_dir), "rounds": 10},
    )
    assert created["status"] == "created"

    result = await call_tool(server, "mirofish_run", {"run": str(run_dir)})
    for _ in range(10):
        if result["status"] == "completed":
            status = await call_tool(server, "mirofish_get_status", {"run": str(run_dir)})
            assert status["status"] == "ok", status
            report = await call_tool(server, "mirofish_get_report", {"run": str(run_dir)})
            assert report["status"] == "ok"
            search = await call_tool(
                server,
                "mirofish_search_graph",
                {"run": str(run_dir), "query": "先进AI芯片出口", "limit": 5},
            )
            assert search["status"] == "ok", search
            exported = await call_tool(server, "mirofish_export_graph", {"run": str(run_dir), "output": None})
            assert exported["status"] == "ok", exported
            artifacts = await call_tool(server, "mirofish_list_artifacts", {"run": str(run_dir)})
            artifact_names = {artifact["name"] for artifact in artifacts["artifacts"]}
            assert {"report.md", "verdict.json", "timeline.json", "graph_snapshot.json"}.issubset(artifact_names)
            doctor = await call_tool(server, "mirofish_doctor", {"runs_dir": str(tmp / "doctor-runs")})
            assert doctor["status"] == "ok", doctor
            followup = await call_tool(
                server,
                "mirofish_ask_followup_question",
                {"run": str(run_dir), "question": "先进AI芯片出口限制有什么影响?", "limit": 5},
            )
            assert followup["status"] == "need_agent_response", followup
            followup_request = await call_tool(
                server,
                "mirofish_get_request",
                {"run": str(run_dir), "request_id": followup["request_id"]},
            )
            request = followup_request["request"]
            response_path = run_dir / "responses" / f"{request['request_id']}.json"
            response_path.write_text(
                json.dumps(
                    {"request_id": request["request_id"], "status": "ok", "output": output_for(request)},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            submitted = await call_tool(
                server,
                "mirofish_submit_response",
                {"run": str(run_dir), "response": str(response_path)},
            )
            assert submitted["ok"], submitted
            answer = await call_tool(
                server,
                "mirofish_get_followup_answer",
                {"run": str(run_dir), "request_id": request["request_id"]},
            )
            assert answer["status"] == "ok", answer
            print(f"MCP lifecycle smoke passed: {run_dir}")
            return 0
        assert result["status"] == "need_agent_response", result
        listed = await call_tool(server, "mirofish_list_requests", {"run": str(run_dir)})
        assert any(item["request_id"] == result["request_id"] for item in listed["requests"])
        request_result = await call_tool(
            server,
            "mirofish_get_request",
            {"run": str(run_dir), "request_id": result["request_id"]},
        )
        request = request_result["request"]
        request_id = request["request_id"]
        response_path = run_dir / "responses" / f"{request_id}.json"
        response_path.write_text(
            json.dumps({"request_id": request_id, "status": "ok", "output": output_for(request)}, ensure_ascii=False),
            encoding="utf-8",
        )
        validation = await call_tool(
            server,
            "mirofish_validate_response",
            {"run": str(run_dir), "response": str(response_path)},
        )
        assert validation["ok"], validation
        submitted = await call_tool(
            server,
            "mirofish_submit_response",
            {"run": str(run_dir), "response": str(response_path)},
        )
        assert submitted["ok"], submitted
        result = await call_tool(server, "mirofish_resume_run", {"run": str(run_dir)})

    print("MCP lifecycle smoke did not complete within expected steps")
    return 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
