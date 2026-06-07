"""Unified business-facing LLM provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class LLMTask:
    run_id: str
    task_type: str
    stage: str
    expected_schema: Dict[str, Any]
    input_text: Optional[str] = None
    input_files: Optional[List[str]] = None
    structured_input: Optional[Dict[str, Any]] = None
    system_prompt: str = ""
    user_prompt: str = ""
    validation_rules: Optional[Dict[str, Any]] = None
    retry_policy: Optional[Dict[str, Any]] = None
    context_refs: Optional[List[str]] = None
    output_contract: Optional[Dict[str, Any]] = None


@dataclass
class LLMProviderResult:
    status: str
    output: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None
    request_file: Optional[str] = None
    expected_response_file: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "status": self.status,
            "output": self.output,
            "request_id": self.request_id,
            "request_file": self.request_file,
            "expected_response_file": self.expected_response_file,
            "error": self.error,
        }
        return {key: value for key, value in data.items() if value is not None}


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def run_task(self, task: LLMTask) -> LLMProviderResult:
        raise NotImplementedError


class ProviderConfigurationError(RuntimeError):
    pass
