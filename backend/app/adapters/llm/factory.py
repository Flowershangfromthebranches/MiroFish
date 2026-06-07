"""LLM provider factory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .agent_queue import AgentQueueLLMProvider
from .base import LLMProvider
from .mock import MockLLMProvider
from .openai_compatible import OpenAICompatibleProvider


def create_llm_provider(provider: Optional[str] = None, *, run_dir: str | Path | None = None) -> LLMProvider:
    provider_name = provider or os.environ.get("MIROFISH_LLM_PROVIDER")
    if not provider_name:
        mode = os.environ.get("MIROFISH_MODE", "agent")
        provider_name = "agent_queue" if mode == "agent" else "openai_compatible"

    provider_name = provider_name.lower()
    if provider_name == "agent_queue":
        return AgentQueueLLMProvider(run_dir=run_dir)
    if provider_name == "mock":
        return MockLLMProvider()
    if provider_name == "openai_compatible":
        return OpenAICompatibleProvider()
    raise ValueError(f"Unsupported MIROFISH_LLM_PROVIDER: {provider_name}")
