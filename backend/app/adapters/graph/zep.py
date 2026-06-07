"""Legacy Zep Cloud graph provider.

This is the only backend/app path allowed to import the Zep SDK.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import GraphProvider, GraphTriple
from ...config import Config


class ZepGraphProvider(GraphProvider):
    name = "zep"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or Config.ZEP_API_KEY
        if not self.api_key:
            raise RuntimeError("ZEP_API_KEY is required for legacy zep graph provider")
        try:
            from zep_cloud.client import Zep
        except ImportError as exc:
            raise RuntimeError(
                "zep-cloud package is required for legacy zep graph provider; "
                "install with `uv sync --extra legacy`"
            ) from exc

        self.client = Zep(api_key=self.api_key)

    def add_episode(self, run_id: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            from zep_cloud import EpisodeData
        except ImportError as exc:
            raise RuntimeError(
                "zep-cloud package is required for legacy zep graph provider; "
                "install with `uv sync --extra legacy`"
            ) from exc

        result = self.client.graph.add(graph_id=run_id, data=EpisodeData(data=content, type="text", metadata=metadata or {}))
        return {"run_id": run_id, "result": str(result)}

    def add_triples(self, run_id: str, triples: List[Dict[str, Any] | GraphTriple]) -> Dict[str, Any]:
        parsed = [triple if isinstance(triple, GraphTriple) else GraphTriple.model_validate(triple) for triple in triples]
        content = "\n".join(triple.fact for triple in parsed)
        self.add_episode(run_id, content, {"source": "agent_triples"})
        return {"run_id": run_id, "triples_added": len(parsed), "mode": "zep_episode_compat"}

    def search(self, run_id: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        result = self.client.graph.search(graph_id=run_id, query=query, limit=limit, scope="edges", reranker="rrf")
        rows = []
        for edge in getattr(result, "edges", []) or []:
            rows.append(
                {
                    "uuid": getattr(edge, "uuid_", None) or getattr(edge, "uuid", ""),
                    "predicate": getattr(edge, "name", ""),
                    "fact": getattr(edge, "fact", ""),
                    "source_node_uuid": getattr(edge, "source_node_uuid", ""),
                    "target_node_uuid": getattr(edge, "target_node_uuid", ""),
                }
            )
        return rows

    def neighbors(self, run_id: str, entity: str, depth: int = 2) -> List[Dict[str, Any]]:
        matches = self.search(run_id, entity, limit=50)
        return matches[: max(depth, 1) * 20]

    def list_entities(self, run_id: str) -> List[Dict[str, Any]]:
        nodes = self.client.graph.node.get_by_graph_id(graph_id=run_id)
        return [self._node_to_dict(node) for node in nodes]

    def get_entity(self, run_id: str, entity: str) -> Optional[Dict[str, Any]]:
        entity_lower = entity.casefold()
        for node in self.list_entities(run_id):
            if node.get("name", "").casefold() == entity_lower:
                return node
        return None

    def update_memory(self, run_id: str, agent_id: str, memory: Dict[str, Any]) -> Dict[str, Any]:
        return self.write_agent_memory(run_id, agent_id, memory)

    def get_agent_memory(self, run_id: str, agent_id: str) -> Dict[str, Any]:
        result = self.search(run_id, f"agent memory {agent_id}", limit=10)
        return {"agent_id": agent_id, "facts": result}

    def write_agent_memory(self, run_id: str, agent_id: str, memory: Dict[str, Any]) -> Dict[str, Any]:
        self.add_episode(run_id, json.dumps({"agent_id": agent_id, "memory": memory}, ensure_ascii=False), {"type": "agent_memory"})
        return {"run_id": run_id, "agent_id": agent_id, "memory": memory}

    def export_snapshot(self, run_id: str, output_path: str) -> Dict[str, Any]:
        nodes = self.list_entities(run_id)
        edges = self.search(run_id, "", limit=1000)
        snapshot = {"run_id": run_id, "provider": "zep", "entities": nodes, "triples": edges}
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"output_path": str(path), "nodes": len(nodes), "triples": len(edges)}

    def import_snapshot(self, run_id: str, input_path: str) -> Dict[str, Any]:
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
        self.add_episode(run_id, json.dumps(data, ensure_ascii=False), {"type": "snapshot_import"})
        return {"run_id": run_id, "imported": True, "mode": "zep_episode_compat"}

    def clear_run_graph(self, run_id: str) -> Dict[str, Any]:
        self.client.graph.delete(graph_id=run_id)
        return {"run_id": run_id, "cleared": True}

    def _node_to_dict(self, node: Any) -> Dict[str, Any]:
        return {
            "uuid": getattr(node, "uuid_", None) or getattr(node, "uuid", ""),
            "name": getattr(node, "name", ""),
            "labels": getattr(node, "labels", []) or [],
            "summary": getattr(node, "summary", "") or "",
            "attributes": getattr(node, "attributes", {}) or {},
        }
