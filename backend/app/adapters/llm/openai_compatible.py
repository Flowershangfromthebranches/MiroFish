"""Legacy OpenAI-compatible provider.

This is the only backend/app path allowed to import the OpenAI SDK.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from .base import LLMProvider, LLMProviderResult, LLMTask, ProviderConfigurationError
from ...config import Config


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME
        if not self.api_key:
            raise ProviderConfigurationError("LLM_API_KEY is required for openai_compatible provider")

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderConfigurationError(
                "openai package is required for openai_compatible legacy provider; "
                "install with `uv sync --extra legacy`"
            ) from exc

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def run_task(self, task: LLMTask) -> LLMProviderResult:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": task.system_prompt or "Return valid JSON."},
                {"role": "user", "content": task.user_prompt or task.input_text or json.dumps(task.structured_input or {}, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        content = response.choices[0].message.content or "{}"
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
        content = re.sub(r"^```(?:json)?\s*\n?", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\n?```\s*$", "", content).strip()
        try:
            output: Dict[str, Any] = json.loads(content)
        except json.JSONDecodeError as exc:
            return LLMProviderResult(status="error", error=f"invalid JSON from openai_compatible provider: {exc}")
        return LLMProviderResult(status="ok", output=output)
