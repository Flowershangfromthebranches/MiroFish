#!/usr/bin/env python3
"""Write a deterministic agent response for the latest or selected request."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_request(run_dir: Path, request_id: str | None) -> dict:
    requests = sorted((run_dir / "requests").glob("req_*.json"))
    if request_id:
        path = run_dir / "requests" / f"{request_id}.json"
    else:
        unanswered = [path for path in requests if not (run_dir / "responses" / path.name).exists()]
        path = unanswered[-1] if unanswered else requests[-1]
    return json.loads(path.read_text(encoding="utf-8"))


def output_for(request: dict) -> dict:
    task_type = request["type"]
    if task_type == "generate_ontology":
        return {"ontology": {"entity_types": [{"name": "Organization"}], "edge_types": [{"name": "AFFECTS"}]}}
    if task_type == "extract_triples":
        return {
            "triples": [
                {
                    "subject": "美国商务部",
                    "predicate": "限制",
                    "object": "先进AI芯片出口",
                    "fact": "美国商务部限制先进AI芯片出口。",
                    "valid_at": "2024-01-01",
                    "invalid_at": None,
                    "source": "smoke",
                    "source_file": "seed.md",
                    "evidence": "美国商务部限制先进AI芯片出口。",
                    "confidence": 0.82,
                    "metadata": {},
                }
            ]
        }
    if task_type == "generate_oasis_profiles":
        return {"profiles": [{"agent_id": "agent_1", "name": "芯片分析员", "persona": "关注芯片供应链变化。"}]}
    if task_type == "generate_simulation_config":
        return {"config": {"rounds": 1, "platforms": ["agent_queue"], "agents": ["agent_1"]}}
    if task_type == "simulate_agent_action":
        actions = []
        for item in request.get("structured_input", {}).get("actions", []):
            actions.append(
                {
                    "agent_id": str(item.get("agent_id")),
                    "action_id": str(item.get("action_id")),
                    "action_type": "CREATE_POST",
                    "content": "先进AI芯片出口限制会推动供应链分化。",
                }
            )
        return {"actions": actions}
    if task_type == "summarize_round":
        return {
            "summary_markdown": "Mock round summary generated without model APIs.",
            "key_events": [],
            "memory_updates": [],
        }
    if task_type == "update_memory":
        return {
            "memory": request.get("structured_input", {}).get("memory", {}),
            "events": request.get("structured_input", {}).get("events", []),
        }
    if task_type == "generate_report":
        return {
            "report_markdown": "# MiroFish Agent Smoke Report\n\n先进AI芯片出口限制可能推动供应链分化。",
            "verdict": {"status": "ok", "confidence": 0.7},
            "timeline": [{"valid_at": "2024-01-01", "fact": "美国商务部限制先进AI芯片出口。"}],
        }
    if task_type == "answer_followup_question":
        question = request.get("structured_input", {}).get("question", "")
        graph_results = request.get("structured_input", {}).get("graph_results", [])
        return {
            "answer_markdown": f"Mock follow-up answer for: {question}",
            "used_graph_results": graph_results[:3],
            "confidence": 0.6,
        }
    if task_type == "validate_json_output":
        return {
            "valid": True,
            "errors": [],
            "output": request.get("structured_input", {}).get("candidate", {}),
        }
    if task_type == "repair_invalid_json":
        return request.get("structured_input", {}).get("invalid_response", {}).get("output", {})
    return {"result": {}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--request-id", default=None)
    args = parser.parse_args()

    run_dir = Path(args.run)
    request = load_request(run_dir, args.request_id)
    response = {"request_id": request["request_id"], "status": "ok", "output": output_for(request)}
    response_path = run_dir / "responses" / f"{request['request_id']}.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    print(response_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
