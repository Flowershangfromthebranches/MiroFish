"""Expected output contracts for all agent task types."""

from __future__ import annotations

from typing import Any, Dict

from .json_schema import TRIPLE_SCHEMA, object_schema


ONTOLOGY_OUTPUT_SCHEMA = object_schema({"ontology": {"type": "object"}}, ["ontology"])

TRIPLES_OUTPUT_SCHEMA = object_schema(
    {"triples": {"type": "array", "items": TRIPLE_SCHEMA}},
    ["triples"],
)

PROFILES_OUTPUT_SCHEMA = object_schema(
    {"profiles": {"type": "array", "items": {"type": "object"}}},
    ["profiles"],
)

SIMULATION_CONFIG_OUTPUT_SCHEMA = object_schema({"config": {"type": "object"}}, ["config"])

SIMULATE_ACTION_OUTPUT_SCHEMA = object_schema(
    {
        "actions": {
            "type": "array",
            "items": object_schema(
                {
                    "agent_id": {"type": "string"},
                    "action_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "content": {"type": "string"},
                },
                ["agent_id", "action_id", "action_type", "content"],
            ),
        }
    },
    ["actions"],
)

REPORT_OUTPUT_SCHEMA = object_schema(
    {
        "report_markdown": {"type": "string", "minLength": 1},
        "verdict": {"type": "object"},
        "timeline": {"type": "array", "items": {"type": "object"}},
    },
    ["report_markdown", "verdict", "timeline"],
)

FOLLOWUP_OUTPUT_SCHEMA = object_schema(
    {
        "answer_markdown": {"type": "string", "minLength": 1},
        "used_graph_results": {"type": "array", "items": {"type": "object"}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    ["answer_markdown", "used_graph_results", "confidence"],
)

AGENT_QUESTION_OUTPUT_SCHEMA = object_schema(
    {
        "agent_id": {"type": "string", "minLength": 1},
        "answer_markdown": {"type": "string", "minLength": 1},
        "used_memory": {"type": "array", "items": {"type": "object"}},
        "used_graph_results": {"type": "array", "items": {"type": "object"}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    ["agent_id", "answer_markdown", "used_memory", "used_graph_results", "confidence"],
)

AGENT_QUESTIONNAIRE_OUTPUT_SCHEMA = object_schema(
    {
        "questionnaire_id": {"type": "string", "minLength": 1},
        "answers": {
            "type": "array",
            "items": object_schema(
                {
                    "agent_id": {"type": "string", "minLength": 1},
                    "question_id": {"type": "string", "minLength": 1},
                    "answer_markdown": {"type": "string", "minLength": 1},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                ["agent_id", "question_id", "answer_markdown", "confidence"],
            ),
        },
        "summary_markdown": {"type": "string"},
    },
    ["questionnaire_id", "answers", "summary_markdown"],
)

QUESTIONNAIRE_SUMMARY_OUTPUT_SCHEMA = object_schema(
    {
        "questionnaire_id": {"type": "string", "minLength": 1},
        "summary_markdown": {"type": "string", "minLength": 1},
        "answer_count": {"type": "integer", "minimum": 0},
    },
    ["questionnaire_id", "summary_markdown", "answer_count"],
)

REPORT_QUESTION_OUTPUT_SCHEMA = object_schema(
    {
        "answer_markdown": {"type": "string", "minLength": 1},
        "used_graph_results": {"type": "array", "items": {"type": "object"}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    ["answer_markdown", "used_graph_results", "confidence"],
)

ROUND_SUMMARY_OUTPUT_SCHEMA = object_schema(
    {
        "summary_markdown": {"type": "string", "minLength": 1},
        "key_events": {"type": "array", "items": {"type": "object"}},
        "memory_updates": {"type": "array", "items": {"type": "object"}},
    },
    ["summary_markdown", "key_events", "memory_updates"],
)

MEMORY_UPDATE_OUTPUT_SCHEMA = object_schema(
    {
        "memory": {"type": "object"},
        "events": {"type": "array", "items": {"type": "object"}},
    },
    ["memory", "events"],
)

VALIDATE_JSON_OUTPUT_SCHEMA = object_schema(
    {
        "valid": {"type": "boolean"},
        "errors": {"type": "array", "items": {"type": "string"}},
        "output": {"type": "object"},
    },
    ["valid", "errors", "output"],
)

GENERIC_REPAIR_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


TASK_OUTPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "extract_triples": TRIPLES_OUTPUT_SCHEMA,
    "generate_ontology": ONTOLOGY_OUTPUT_SCHEMA,
    "generate_oasis_profiles": PROFILES_OUTPUT_SCHEMA,
    "generate_simulation_config": SIMULATION_CONFIG_OUTPUT_SCHEMA,
    "simulate_agent_action": SIMULATE_ACTION_OUTPUT_SCHEMA,
    "summarize_round": ROUND_SUMMARY_OUTPUT_SCHEMA,
    "update_memory": MEMORY_UPDATE_OUTPUT_SCHEMA,
    "generate_report": REPORT_OUTPUT_SCHEMA,
    "answer_followup_question": FOLLOWUP_OUTPUT_SCHEMA,
    "answer_agent_question": AGENT_QUESTION_OUTPUT_SCHEMA,
    "answer_agent_questionnaire": AGENT_QUESTIONNAIRE_OUTPUT_SCHEMA,
    "summarize_questionnaire": QUESTIONNAIRE_SUMMARY_OUTPUT_SCHEMA,
    "ask_report_question": REPORT_QUESTION_OUTPUT_SCHEMA,
    "validate_json_output": VALIDATE_JSON_OUTPUT_SCHEMA,
    "repair_invalid_json": GENERIC_REPAIR_OUTPUT_SCHEMA,
}


STAGE_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "ontology": {
        "task_type": "generate_ontology",
        "schema": ONTOLOGY_OUTPUT_SCHEMA,
        "system_prompt": "Generate a compact ontology for prediction graph construction.",
        "user_prompt": "Return JSON with an ontology object. Do not include explanations.",
    },
    "graph": {
        "task_type": "extract_triples",
        "schema": TRIPLES_OUTPUT_SCHEMA,
        "system_prompt": "Extract factual entity-relationship-entity triples from the seed only.",
        "user_prompt": (
            "Read the seed and return triples. Each triple must include subject, predicate, "
            "object, fact, evidence, confidence, time fields, source fields, and metadata. "
            "Do not invent facts not supported by evidence."
        ),
    },
    "profiles": {
        "task_type": "generate_oasis_profiles",
        "schema": PROFILES_OUTPUT_SCHEMA,
        "system_prompt": "Generate OASIS/CAMEL-compatible agent profiles from the seed and graph facts.",
        "user_prompt": "Return JSON with profiles array.",
    },
    "config": {
        "task_type": "generate_simulation_config",
        "schema": SIMULATION_CONFIG_OUTPUT_SCHEMA,
        "system_prompt": "Generate a simulation config that can run without direct model API keys.",
        "user_prompt": "Return JSON with config object.",
    },
    "simulation": {
        "task_type": "simulate_agent_action",
        "schema": SIMULATE_ACTION_OUTPUT_SCHEMA,
        "system_prompt": "Generate batched simulation actions keyed by agent_id and action_id.",
        "user_prompt": "Return JSON with actions array.",
    },
    "report": {
        "task_type": "generate_report",
        "schema": REPORT_OUTPUT_SCHEMA,
        "system_prompt": "Generate final prediction report artifacts from seed, graph, profiles, config, and simulation actions.",
        "user_prompt": "Return report_markdown, verdict, and timeline.",
    },
}
