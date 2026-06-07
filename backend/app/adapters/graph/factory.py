"""Graph provider factory."""

from __future__ import annotations

import os
from typing import Optional

from .base import GraphProvider
from .graphiti import GraphitiGraphProvider
from .zep import ZepGraphProvider


def create_graph_provider(provider: Optional[str] = None) -> GraphProvider:
    provider_name = provider or os.environ.get("MIROFISH_GRAPH_PROVIDER")
    if not provider_name:
        mode = os.environ.get("MIROFISH_MODE", "agent")
        provider_name = "graphiti" if mode == "agent" else "zep"

    provider_name = provider_name.lower()
    if provider_name == "graphiti":
        return GraphitiGraphProvider()
    if provider_name == "zep":
        return ZepGraphProvider()
    raise ValueError(f"Unsupported MIROFISH_GRAPH_PROVIDER: {provider_name}")
