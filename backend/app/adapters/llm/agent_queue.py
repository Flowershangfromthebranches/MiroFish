"""LLM provider that delegates model work to external desktop agents."""

from __future__ import annotations

from pathlib import Path

from .base import LLMProvider, LLMProviderResult, LLMTask
from ...agent_engine.queue import AgentQueue


class AgentQueueLLMProvider(LLMProvider):
    name = "agent_queue"

    def __init__(self, run_dir: str | Path | None = None):
        self.run_dir = Path(run_dir) if run_dir else None

    def run_task(self, task: LLMTask) -> LLMProviderResult:
        if not self.run_dir:
            raise RuntimeError("agent_queue provider requires run_dir")

        queue = AgentQueue(self.run_dir)
        need = queue.create_request(
            run_id=task.run_id,
            task_type=task.task_type,
            stage=task.stage,
            expected_schema=task.expected_schema,
            input_text=task.input_text,
            input_files=task.input_files,
            structured_input=task.structured_input,
            system_prompt=task.system_prompt,
            user_prompt=task.user_prompt,
            validation_rules=task.validation_rules,
            retry_policy=task.retry_policy,
            context_refs=task.context_refs,
            output_contract=task.output_contract,
        )
        return LLMProviderResult(
            status="need_agent_response",
            request_id=need.request_id,
            request_file=need.request_file,
            expected_response_file=need.expected_response_file,
        )
