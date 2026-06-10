"""Deterministic provider for tests and offline smoke runs."""

from __future__ import annotations

from typing import Any, Dict

from .base import LLMProvider, LLMProviderResult, LLMTask


class MockLLMProvider(LLMProvider):
    name = "mock"

    def run_task(self, task: LLMTask) -> LLMProviderResult:
        output = self._output_for(task)
        return LLMProviderResult(status="ok", output=output)

    def _output_for(self, task: LLMTask) -> Dict[str, Any]:
        required = task.expected_schema.get("required", []) if task.expected_schema else []
        if "text" in required:
            return {"text": "Mock text response generated without model APIs."}
        if task.task_type == "generate_ontology":
            return {
                "ontology": {
                    "entity_types": [{"name": "Organization", "description": "An organization"}],
                    "edge_types": [{"name": "AFFECTS", "description": "Affects another entity"}],
                }
            }
        if task.task_type == "extract_triples":
            return {
                "triples": [
                    {
                        "subject": "Seed Entity",
                        "predicate": "relates_to",
                        "object": "Prediction Topic",
                        "fact": "Seed Entity relates to Prediction Topic.",
                        "valid_at": None,
                        "invalid_at": None,
                        "source": "mock",
                        "source_file": None,
                        "evidence": "mock evidence",
                        "confidence": 0.5,
                        "metadata": {},
                    }
                ]
            }
        if task.task_type == "generate_oasis_profiles":
            return {
                "profiles": [
                    {
                        "agent_id": "agent_1",
                        "name": "Seed Analyst",
                        "persona": "Tracks seed facts and reacts conservatively.",
                    }
                ]
            }
        if task.task_type == "generate_simulation_config":
            return {"config": {"rounds": 1, "agents": ["agent_1"], "platforms": ["agent_queue"]}}
        if task.task_type == "simulate_agent_action":
            actions = []
            for item in (task.structured_input or {}).get("actions", []):
                actions.append(
                    {
                        "agent_id": item.get("agent_id"),
                        "action_id": item.get("action_id"),
                        "action_type": "CREATE_POST",
                        "content": "Mock simulated action.",
                    }
                )
            return {"actions": actions or [{"agent_id": "agent_1", "action_id": "act_1", "action_type": "CREATE_POST", "content": "Mock simulated action."}]}
        if task.task_type == "summarize_round":
            return {
                "summary_markdown": "Mock round summary generated without model APIs.",
                "key_events": [],
                "memory_updates": [],
            }
        if task.task_type == "update_memory":
            return {
                "memory": (task.structured_input or {}).get("memory", {}),
                "events": (task.structured_input or {}).get("events", []),
            }
        if task.task_type == "generate_report":
            return {
                "report_markdown": "# MiroFish Agent Report\n\nMock report generated without model APIs.",
                "verdict": {"status": "mock", "confidence": 0.5},
                "timeline": [{"step": "mock", "summary": "Mock simulation completed."}],
            }
        if task.task_type == "answer_followup_question":
            return {
                "answer_markdown": "Mock follow-up answer generated without model APIs.",
                "used_graph_results": (task.structured_input or {}).get("graph_results", []),
                "confidence": 0.5,
            }
        if task.task_type == "answer_agent_question":
            agent_id = (task.structured_input or {}).get("agent_id", "agent_1")
            return {
                "agent_id": agent_id,
                "answer_markdown": f"Mock answer from {agent_id} generated without model APIs.",
                "used_memory": [],
                "used_graph_results": (task.structured_input or {}).get("graph_results", []),
                "confidence": 0.5,
            }
        if task.task_type == "answer_agent_questionnaire":
            questionnaire_id = (task.structured_input or {}).get("questionnaire_id", "q_mock")
            questions = (task.structured_input or {}).get("questions", [])
            agents = (task.structured_input or {}).get("agents", [])
            answers = []
            for question in questions:
                for agent in agents:
                    answers.append({
                        "agent_id": agent.get("agent_id", "agent_1"),
                        "question_id": question.get("question_id", "q1"),
                        "answer_markdown": f"Mock answer from {agent.get('agent_id', 'agent_1')} to {question.get('question_id', 'q1')}.",
                        "confidence": 0.5,
                    })
            return {
                "questionnaire_id": questionnaire_id,
                "answers": answers,
                "summary_markdown": "Mock questionnaire summary generated without model APIs.",
            }
        if task.task_type == "summarize_questionnaire":
            return {
                "questionnaire_id": (task.structured_input or {}).get("questionnaire_id", "q_mock"),
                "summary_markdown": "Mock questionnaire summary generated without model APIs.",
                "answer_count": len((task.structured_input or {}).get("answers", [])),
            }
        if task.task_type == "ask_report_question":
            return {
                "answer_markdown": "Mock report question answer generated without model APIs.",
                "used_graph_results": (task.structured_input or {}).get("graph_results", []),
                "confidence": 0.5,
            }
        if task.task_type == "validate_json_output":
            return {
                "valid": True,
                "errors": [],
                "output": (task.structured_input or {}).get("candidate", {}),
            }
        if task.task_type == "repair_invalid_json":
            invalid_response = (task.structured_input or {}).get("invalid_response", {})
            return invalid_response.get("output", {})
        return {"result": task.structured_input or {}}
