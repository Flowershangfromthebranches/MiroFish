"""Strict request/response schemas for desktop-agent driven runs."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


AgentTaskType = Literal[
    "extract_triples",
    "generate_ontology",
    "generate_oasis_profiles",
    "generate_simulation_config",
    "simulate_agent_action",
    "summarize_round",
    "update_memory",
    "generate_report",
    "answer_followup_question",
    "validate_json_output",
    "repair_invalid_json",
]

AGENT_TASK_TYPES = {
    "extract_triples",
    "generate_ontology",
    "generate_oasis_profiles",
    "generate_simulation_config",
    "simulate_agent_action",
    "summarize_round",
    "update_memory",
    "generate_report",
    "answer_followup_question",
    "validate_json_output",
    "repair_invalid_json",
}


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_AGENT = "waiting_agent"
    AWAITING_USER_CONFIRMATION = "awaiting_user_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_repair_attempts: int = Field(default=1, ge=0)
    repair_attempts_used: int = Field(default=0, ge=0)


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    run_id: str
    type: AgentTaskType
    stage: str
    input_text: Optional[str] = None
    input_files: List[str] = Field(default_factory=list)
    structured_input: Dict[str, Any] = Field(default_factory=dict)
    system_prompt: str = ""
    user_prompt: str = ""
    expected_schema: Dict[str, Any]
    validation_rules: Dict[str, Any] = Field(default_factory=dict)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    context_refs: List[str] = Field(default_factory=list)
    output_contract: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not value.startswith("req_"):
            raise ValueError("request_id must start with req_")
        return value

    @field_validator("expected_schema")
    @classmethod
    def validate_expected_schema(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if not value or value.get("type") != "object":
            raise ValueError("expected_schema must be a non-empty JSON object schema")
        return value


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    status: Literal["ok", "error", "skipped"]
    output: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    validation: Optional[Dict[str, Any]] = None

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not value.startswith("req_"):
            raise ValueError("request_id must start with req_")
        return value


class AgentNeedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["need_agent_response"] = "need_agent_response"
    request_id: str
    request_file: str
    expected_response_file: str
    stage: str
    type: str


class StageCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: StageStatus = StageStatus.PENDING
    request_ids: List[str] = Field(default_factory=list)
    artifact_paths: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    stale: bool = False
    stale_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    run_dir: str
    requirement: str
    seed_files: List[str] = Field(default_factory=list)
    mode: str = "agent"
    workflow_mode: Literal["auto", "staged"] = "auto"
    current_stage: str = "ontology"
    stages: Dict[str, StageCheckpoint]
    seed_path: Optional[str] = None
    simulation_settings: Dict[str, Any] = Field(default_factory=dict)
    graph_summary: Dict[str, Any] = Field(default_factory=dict)
    profiles_summary: Dict[str, Any] = Field(default_factory=dict)
    config_summary: Dict[str, Any] = Field(default_factory=dict)
    simulation_progress: Dict[str, Any] = Field(default_factory=dict)
    report_artifacts: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    errors: List[str] = Field(default_factory=list)
    repair_request: Optional[AgentNeedResponse] = None
