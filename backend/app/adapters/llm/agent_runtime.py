"""Business-facing model runtime facade."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import LLMProvider, LLMProviderResult, LLMTask
from .factory import create_llm_provider


class NeedAgentResponse(RuntimeError):
    def __init__(self, result: LLMProviderResult):
        self.result = result
        super().__init__(f"Agent response required: {result.request_file}")


class AgentRuntime:
    def __init__(self, provider: Optional[LLMProvider] = None, *, run_dir: str | None = None):
        self.provider = provider or create_llm_provider(run_dir=run_dir)
        self.run_dir = run_dir

    def run_task(
        self,
        *,
        run_id: str,
        task_type: str,
        stage: str,
        expected_schema: Dict[str, Any],
        input_text: Optional[str] = None,
        input_files: Optional[List[str]] = None,
        structured_input: Optional[Dict[str, Any]] = None,
        system_prompt: str = "",
        user_prompt: str = "",
        validation_rules: Optional[Dict[str, Any]] = None,
        retry_policy: Optional[Dict[str, Any]] = None,
        context_refs: Optional[List[str]] = None,
        output_contract: Optional[Dict[str, Any]] = None,
    ) -> LLMProviderResult:
        task = LLMTask(
            run_id=run_id,
            task_type=task_type,
            stage=stage,
            expected_schema=expected_schema,
            input_text=input_text,
            input_files=input_files,
            structured_input=structured_input,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            validation_rules=validation_rules,
            retry_policy=retry_policy,
            context_refs=context_refs,
            output_contract=output_contract,
        )
        return self.provider.run_task(task)

    def require_output(self, **kwargs: Any) -> Dict[str, Any]:
        result = self.run_task(**kwargs)
        if result.status == "need_agent_response":
            raise NeedAgentResponse(result)
        if result.status != "ok":
            raise RuntimeError(result.error or f"LLM task failed with status {result.status}")
        return result.output or {}
