"""Legacy LLMClient facade backed by AgentRuntime."""

import json
import re
from pathlib import Path
from typing import Optional, Dict, Any, List

from ..config import Config
from ..adapters.llm.agent_runtime import AgentRuntime, NeedAgentResponse
from ..agent_engine.json_schema import object_schema


class LLMClient:
    """Compatibility wrapper for older services.

    New business code should call AgentRuntime directly. This class remains so
    legacy UI/report code can be migrated incrementally without direct SDK use.
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME
        legacy_run_dir = Path(Config.MIROFISH_RUNS_DIR) / "legacy-ui"
        legacy_run_dir.mkdir(parents=True, exist_ok=True)
        self.runtime = AgentRuntime(run_dir=str(legacy_run_dir))
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """
        发送聊天请求
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            response_format: 响应格式（如JSON模式）
            
        Returns:
            模型响应文本
        """
        if response_format and response_format.get("type") == "json_object":
            max_tokens = max(max_tokens, 8192)

        structured_input = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": response_format,
            "legacy_client": True,
        }
        user_prompt = "\n".join(message.get("content", "") for message in messages if message.get("role") == "user")
        system_prompt = "\n".join(message.get("content", "") for message in messages if message.get("role") == "system")
        result = self.runtime.run_task(
            run_id="legacy-ui",
            task_type="validate_json_output" if response_format else "answer_followup_question",
            stage="legacy_llm_client",
            expected_schema=object_schema({"text": {"type": "string"}}, ["text"]),
            structured_input=structured_input,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if result.status == "need_agent_response":
            raise NeedAgentResponse(result)
        if result.status != "ok":
            raise RuntimeError(result.error or "LLM provider failed")
        content = (result.output or {}).get("text")
        if content is None and result.output:
            content = json.dumps(result.output, ensure_ascii=False)
        content = content or ""
        # 部分模型（如MiniMax M2.5）会在content中包含<think>思考内容，需要移除
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """
        发送聊天请求并返回JSON
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            解析后的JSON对象
        """
        structured_input = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "legacy_client": True,
        }
        user_prompt = "\n".join(message.get("content", "") for message in messages if message.get("role") == "user")
        system_prompt = "\n".join(message.get("content", "") for message in messages if message.get("role") == "system")
        result = self.runtime.run_task(
            run_id="legacy-ui",
            task_type="validate_json_output",
            stage="legacy_llm_client",
            expected_schema={"type": "object"},
            structured_input=structured_input,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if result.status == "need_agent_response":
            raise NeedAgentResponse(result)
        if result.status != "ok":
            raise RuntimeError(result.error or "LLM provider failed")
        if isinstance(result.output, dict):
            return result.output
        response = json.dumps(result.output, ensure_ascii=False)
        # 清理markdown代码块标记
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        try:
            return json.loads(cleaned_response)
        except json.JSONDecodeError:
            raise ValueError(f"LLM返回的JSON格式无效: {cleaned_response}")
