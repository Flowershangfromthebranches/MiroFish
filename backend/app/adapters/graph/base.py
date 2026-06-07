"""Unified graph provider contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class GraphTriple(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    predicate: str
    object: str
    fact: str
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None
    source: Optional[str] = None
    source_file: Optional[str] = None
    evidence: str
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GraphProvider(ABC):
    name: str

    @abstractmethod
    def add_episode(self, run_id: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def add_triples(self, run_id: str, triples: List[Dict[str, Any] | GraphTriple]) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def search(self, run_id: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def neighbors(self, run_id: str, entity: str, depth: int = 2) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def list_entities(self, run_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_entity(self, run_id: str, entity: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def update_memory(self, run_id: str, agent_id: str, memory: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_agent_memory(self, run_id: str, agent_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def write_agent_memory(self, run_id: str, agent_id: str, memory: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def export_snapshot(self, run_id: str, output_path: str) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def import_snapshot(self, run_id: str, input_path: str) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def clear_run_graph(self, run_id: str) -> Dict[str, Any]:
        raise NotImplementedError
