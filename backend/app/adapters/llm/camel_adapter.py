"""Bridge CAMEL/OASIS model calls into AgentRuntime."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional, Type

from ...agent_engine.json_schema import object_schema
from .base import LLMProviderResult
from .agent_runtime import AgentRuntime

from camel.messages import OpenAIMessage
from camel.models import BaseModelBackend
from camel.types import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    Choice,
    CompletionUsage,
    ModelType,
)
from camel.utils import BaseTokenCounter
from pydantic import BaseModel


SIMULATE_ACTION_SCHEMA = object_schema(
    {
        "actions": {
            "type": "array",
            "items": object_schema(
                {
                    "agent_id": {"type": "string"},
                    "action_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "content": {"type": "string"},
                }
            ),
        }
    }
)


class AgentRuntimeTokenCounter(BaseTokenCounter):
    def count_tokens_from_messages(self, messages: List[OpenAIMessage]) -> int:
        return sum(len(str(message.get("content", ""))) // 4 + 1 for message in messages)

    def encode(self, text: str) -> List[int]:
        return [0] * (len(text) // 4 + 1)

    def decode(self, token_ids: List[int]) -> str:
        return ""


class AgentModelBackendAdapter(BaseModelBackend):
    """CAMEL-compatible model backend backed by AgentRuntime.

    CAMEL/OASIS scripts can use this adapter instead of directly constructing
    model SDK clients. Batch calls are preferred for same-round actions.
    """

    def __init__(self, run_id: str, run_dir: str, runtime: Optional[AgentRuntime] = None):
        super().__init__(model_type=ModelType.STUB, model_config_dict={})
        self.run_id = run_id
        self.run_dir = run_dir
        self.runtime = runtime or AgentRuntime(run_dir=run_dir)
        self.last_need_agent_response: Optional[Dict[str, Any]] = None

    @property
    def token_counter(self) -> BaseTokenCounter:
        if not self._token_counter:
            self._token_counter = AgentRuntimeTokenCounter()
        return self._token_counter

    def run_batch_actions(self, round_id: str, actions: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        action_list = list(actions)
        result = self.runtime.run_task(
            run_id=self.run_id,
            task_type="simulate_agent_action",
            stage="simulation",
            expected_schema=SIMULATE_ACTION_SCHEMA,
            structured_input={"round_id": round_id, "actions": action_list},
            system_prompt="Generate simulation actions for the requested agents.",
            user_prompt="Return JSON with actions keyed by agent_id and action_id.",
            output_contract={"batch_key": ["agent_id", "action_id"]},
        )
        if result.status == "need_agent_response":
            self.last_need_agent_response = result.to_dict()
        return result.to_dict()

    def run_single_action(self, agent_id: str, action_id: str, prompt: str) -> Dict[str, Any]:
        return self.run_batch_actions(
            round_id="single",
            actions=[{"agent_id": agent_id, "action_id": action_id, "prompt": prompt}],
        )

    def _run(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatCompletion:
        result = self._run_model_task(messages, tools)
        return self._to_chat_completion(result, tools)

    async def _arun(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatCompletion:
        result = self._run_model_task(messages, tools)
        return self._to_chat_completion(result, tools)

    def _run_model_task(self, messages: List[OpenAIMessage], tools: Optional[List[Dict[str, Any]]]) -> LLMProviderResult:
        return self.runtime.run_task(
            run_id=self.run_id,
            task_type="simulate_agent_action",
            stage="simulation_runtime",
            expected_schema=SIMULATE_ACTION_SCHEMA,
            structured_input={
                "messages": messages,
                "tools": tools or [],
                "actions": [
                    {
                        "agent_id": self._extract_agent_id(messages),
                        "action_id": f"camel_{int(time.time() * 1000)}",
                        "prompt": messages[-1].get("content", "") if messages else "",
                    }
                ],
            },
            system_prompt="You are driving one OASIS/CAMEL social simulation agent action.",
            user_prompt=json.dumps(messages, ensure_ascii=False),
            output_contract={"camel_model_backend": True},
        )

    def _to_chat_completion(self, result: LLMProviderResult, tools: Optional[List[Dict[str, Any]]]) -> ChatCompletion:
        if result.status == "need_agent_response":
            self.last_need_agent_response = result.to_dict()
            content = json.dumps(self.last_need_agent_response, ensure_ascii=False)
            tool_calls = self._tool_calls_for_actions([{"action_type": "DO_NOTHING", "action_args": {}}], tools)
            return self._completion(content=content, tool_calls=tool_calls)
        if result.status != "ok":
            return self._completion(content=result.error or "AgentRuntime model task failed")

        output = result.output or {}
        actions = output.get("actions") or []
        tool_calls = self._tool_calls_for_actions(actions, tools)
        content = output.get("content") or output.get("text") or json.dumps(output, ensure_ascii=False)
        return self._completion(content=content if not tool_calls else "", tool_calls=tool_calls)

    def _tool_calls_for_actions(
        self,
        actions: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        available = self._available_tool_names(tools)
        if not available:
            return None

        calls = []
        for index, action in enumerate(actions[:1]):
            tool_name = self._action_to_tool_name(action.get("action_type", "DO_NOTHING"))
            if tool_name not in available:
                tool_name = "do_nothing" if "do_nothing" in available else sorted(available)[0]
            args = action.get("action_args") or self._action_args(action)
            calls.append(
                {
                    "id": f"call_agent_runtime_{index}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )
        return calls or None

    def _available_tool_names(self, tools: Optional[List[Dict[str, Any]]]) -> set[str]:
        names = set()
        for tool in tools or []:
            function = tool.get("function", {}) if isinstance(tool, dict) else {}
            name = function.get("name")
            if name:
                names.add(name)
        return names

    def _action_to_tool_name(self, action_type: str) -> str:
        return str(action_type or "DO_NOTHING").lower()

    def _action_args(self, action: Dict[str, Any]) -> Dict[str, Any]:
        if action.get("content"):
            return {"content": action["content"]}
        return {}

    def _extract_agent_id(self, messages: List[OpenAIMessage]) -> str:
        for message in messages:
            content = str(message.get("content", ""))
            if "Agent" in content:
                return "camel_agent"
        return "camel_agent"

    def _completion(
        self,
        *,
        content: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatCompletion:
        message = ChatCompletionMessage(
            content=None if tool_calls else content,
            role="assistant",
            tool_calls=tool_calls,
        )
        return ChatCompletion(
            id=f"agent-runtime-{int(time.time() * 1000)}",
            model="mirofish-agent-runtime",
            object="chat.completion",
            created=int(time.time()),
            choices=[
                Choice(
                    finish_reason="tool_calls" if tool_calls else "stop",
                    index=0,
                    message=message,
                    logprobs=None,
                )
            ],
            usage=CompletionUsage(
                completion_tokens=max(1, len(content) // 4),
                prompt_tokens=1,
                total_tokens=max(2, len(content) // 4 + 1),
            ),
        )


def create_model_backend(run_id: str, run_dir: str) -> AgentModelBackendAdapter:
    return AgentModelBackendAdapter(run_id=run_id, run_dir=run_dir)
