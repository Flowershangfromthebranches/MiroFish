"""Graphiti provider with no-LLM triplet write compatibility.

The compatibility store intentionally hides all Neo4j/Cypher and Graphiti
schema assumptions from business code.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import GraphProvider, GraphTriple


class GraphitiDependencyError(RuntimeError):
    pass


class GraphitiCompatibilityStore:
    """Version-sensitive Graphiti/Neo4j no-LLM triplet store."""

    def __init__(self, *, store_path: str | None = None, require_neo4j: bool = False):
        self.store_mode = os.environ.get("MIROFISH_GRAPHITI_STORE", "auto").lower()
        if self.store_mode not in {"auto", "neo4j", "file"}:
            raise GraphitiDependencyError("MIROFISH_GRAPHITI_STORE must be auto, neo4j, or file")
        self.neo4j_uri = os.environ.get("NEO4J_URI")
        if self.store_mode in {"auto", "neo4j"} and not self.neo4j_uri:
            self.neo4j_uri = "bolt://localhost:7687"
        self.neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        self.neo4j_password = os.environ.get("NEO4J_PASSWORD", "password")
        self.neo4j_database = os.environ.get("NEO4J_DATABASE", "neo4j")
        self.store_path = Path(store_path or os.environ.get("MIROFISH_GRAPHITI_COMPAT_PATH", "./runs/.graphiti_compat_store.json"))
        self.driver = None

        should_use_neo4j = self.store_mode in {"auto", "neo4j"}
        if should_use_neo4j:
            spec = importlib.util.find_spec("neo4j")
            if not spec:
                raise GraphitiDependencyError(
                    "neo4j Python package is required for GraphitiCompatibilityStore neo4j/auto mode; "
                    "set MIROFISH_GRAPHITI_STORE=file for offline compatibility tests"
                )
            else:
                from neo4j import GraphDatabase

                self.driver = GraphDatabase.driver(
                    self.neo4j_uri,
                    auth=(self.neo4j_user, self.neo4j_password),
                )
                self._ensure_constraints()

        if not self.driver:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.store_path.exists():
                self._write_file_store({"runs": {}})

    def close(self) -> None:
        if self.driver:
            self.driver.close()

    def normalize_entity(self, value: str) -> str:
        return re.sub(r"\s+", " ", value.strip()).casefold()

    def add_triplet(self, run_id: str, triple: GraphTriple) -> Dict[str, Any]:
        if self.driver:
            return self._add_triplet_neo4j(run_id, triple)
        return self._add_triplet_file(run_id, triple)

    def add_triples(self, run_id: str, triples: List[GraphTriple]) -> Dict[str, Any]:
        for triple in triples:
            self.add_triplet(run_id, triple)
        return {"provider": "graphiti", "store": "neo4j" if self.driver else "file", "triples_added": len(triples)}

    def get_or_create_entity_node(self, run_id: str, name: str, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        normalized = self.normalize_entity(name)
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                record = session.run(
                    """
                    MERGE (e:MiroFishEntity {group_id: $run_id, normalized_name: $normalized})
                    ON CREATE SET e.uuid = $uuid, e.name = $name, e.labels = $labels, e.created_at = datetime()
                    ON MATCH SET e.name = coalesce(e.name, $name)
                    RETURN e
                    """,
                    run_id=run_id,
                    normalized=normalized,
                    uuid=self._entity_uuid(run_id, normalized),
                    name=name,
                    labels=labels or ["Entity"],
                ).single()
                node = record["e"]
                return self._to_jsonable(dict(node.items()))

        data = self._read_file_store()
        run = self._file_run(data, run_id)
        entities = run["entities"]
        if normalized not in entities:
            entities[normalized] = {
                "uuid": self._entity_uuid(run_id, normalized),
                "name": name,
                "normalized_name": normalized,
                "labels": labels or ["Entity"],
                "summary": "",
                "attributes": {},
            }
            self._write_file_store(data)
        return entities[normalized]

    def search_facts(self, run_id: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                result = session.run(
                    """
                    MATCH (s:MiroFishEntity {group_id: $run_id})-[r:MIROFISH_FACT]->(o:MiroFishEntity {group_id: $run_id})
                    WHERE toLower(r.fact) CONTAINS toLower($search_query)
                       OR toLower(s.name) CONTAINS toLower($search_query)
                       OR toLower(o.name) CONTAINS toLower($search_query)
                    RETURN s, r, o
                    LIMIT $limit
                    """,
                    run_id=run_id,
                    search_query=query,
                    limit=limit,
                )
                return [self._record_to_fact(row) for row in result]

        data = self._read_file_store()
        run = self._file_run(data, run_id)
        query_lower = query.casefold()
        terms = self._query_terms(query_lower)
        matches = []
        for triple in run["triples"].values():
            haystack = " ".join(
                [
                    triple.get("subject", ""),
                    triple.get("predicate", ""),
                    triple.get("object", ""),
                    triple.get("fact", ""),
                    triple.get("evidence", ""),
                ]
            ).casefold()
            compact_haystack = re.sub(r"\W+", "", haystack)
            if query_lower in haystack or any(term in haystack or term in compact_haystack for term in terms):
                matches.append(triple)
        return matches[:limit]

    def search_nodes(self, run_id: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        nodes = self.list_entities(run_id)
        query_lower = query.casefold()
        return [node for node in nodes if query_lower in node.get("name", "").casefold()][:limit]

    def neighbors(self, run_id: str, entity: str, depth: int = 2) -> List[Dict[str, Any]]:
        normalized = self.normalize_entity(entity)
        max_depth = max(1, min(int(depth), 10))
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                result = session.run(
                    f"""
                    MATCH path=(start:MiroFishEntity {{group_id: $run_id, normalized_name: $normalized}})-[*1..{max_depth}]-(n:MiroFishEntity {{group_id: $run_id}})
                    RETURN nodes(path) AS nodes, relationships(path) AS relationships
                    LIMIT 100
                    """,
                    run_id=run_id,
                    normalized=normalized,
                )
                return [
                    {
                        "nodes": [self._to_jsonable(dict(n.items())) for n in row["nodes"]],
                        "relationships": [self._to_jsonable(dict(r.items())) for r in row["relationships"]],
                    }
                    for row in result
                ]

        data = self._read_file_store()
        run = self._file_run(data, run_id)
        frontier = {normalized}
        seen = {normalized}
        facts = []
        for _ in range(max(depth, 1)):
            next_frontier = set()
            for triple in run["triples"].values():
                subject = self.normalize_entity(triple["subject"])
                obj = self.normalize_entity(triple["object"])
                if subject in frontier or obj in frontier:
                    facts.append(triple)
                    if subject not in seen:
                        next_frontier.add(subject)
                    if obj not in seen:
                        next_frontier.add(obj)
            seen.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break
        return facts

    def list_entities(self, run_id: str) -> List[Dict[str, Any]]:
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                result = session.run(
                    "MATCH (e:MiroFishEntity {group_id: $run_id}) RETURN e ORDER BY e.name",
                    run_id=run_id,
                )
                return [self._to_jsonable(dict(row["e"].items())) for row in result]

        data = self._read_file_store()
        return list(self._file_run(data, run_id)["entities"].values())

    def get_entity(self, run_id: str, entity: str) -> Optional[Dict[str, Any]]:
        normalized = self.normalize_entity(entity)
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                row = session.run(
                    "MATCH (e:MiroFishEntity {group_id: $run_id, normalized_name: $normalized}) RETURN e",
                    run_id=run_id,
                    normalized=normalized,
                ).single()
                return self._to_jsonable(dict(row["e"].items())) if row else None

        data = self._read_file_store()
        return self._file_run(data, run_id)["entities"].get(normalized)

    def update_memory(self, run_id: str, agent_id: str, memory: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_agent_memory(run_id, agent_id)
        current.update(memory)
        return self.write_agent_memory(run_id, agent_id, current)

    def get_agent_memory(self, run_id: str, agent_id: str) -> Dict[str, Any]:
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                row = session.run(
                    """
                    MATCH (m:MiroFishAgentMemory {group_id: $run_id, agent_id: $agent_id})
                    RETURN m.memory_json AS memory_json
                    """,
                    run_id=run_id,
                    agent_id=agent_id,
                ).single()
                if not row:
                    return {}
                return json.loads(row["memory_json"] or "{}")

        data = self._read_file_store()
        return self._file_run(data, run_id)["memory"].get(agent_id, {})

    def write_agent_memory(self, run_id: str, agent_id: str, memory: Dict[str, Any]) -> Dict[str, Any]:
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                session.run(
                    """
                    MERGE (m:MiroFishAgentMemory {group_id: $run_id, agent_id: $agent_id})
                    ON CREATE SET m.uuid = $uuid, m.created_at = datetime()
                    SET m.memory_json = $memory_json, m.updated_at = datetime()
                    """,
                    run_id=run_id,
                    agent_id=agent_id,
                    uuid=self._memory_uuid(run_id, agent_id),
                    memory_json=json.dumps(memory, ensure_ascii=False),
                )
            return {"run_id": run_id, "agent_id": agent_id, "memory": memory}

        data = self._read_file_store()
        run = self._file_run(data, run_id)
        run["memory"][agent_id] = memory
        self._write_file_store(data)
        return {"run_id": run_id, "agent_id": agent_id, "memory": memory}

    def export_snapshot(self, run_id: str, output_path: str) -> Dict[str, Any]:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot(run_id)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"output_path": str(path), "nodes": len(snapshot["entities"]), "triples": len(snapshot["triples"])}

    def import_snapshot(self, run_id: str, input_path: str) -> Dict[str, Any]:
        snapshot = json.loads(Path(input_path).read_text(encoding="utf-8"))
        if self.driver:
            self.clear_run_graph(run_id)
            for entity in snapshot.get("entities", []):
                name = entity.get("name")
                if name:
                    self.get_or_create_entity_node(run_id, name, entity.get("labels"))
            triples = [GraphTriple.model_validate(self._clean_triple_payload(triple)) for triple in snapshot.get("triples", [])]
            self.add_triples(run_id, triples)
            for episode in snapshot.get("episodes", []):
                self.add_episode(run_id, episode.get("content", ""), episode.get("metadata", {}))
            for agent_id, memory in snapshot.get("memory", {}).items():
                self.write_agent_memory(run_id, agent_id, memory)
            return {
                "run_id": run_id,
                "imported": True,
                "entities": len(snapshot.get("entities", [])),
                "triples": len(triples),
                "episodes": len(snapshot.get("episodes", [])),
            }

        data = self._read_file_store()
        data["runs"][run_id] = {
            "entities": {self.normalize_entity(e["name"]): e for e in snapshot.get("entities", [])},
            "triples": {self._triple_uuid(run_id, t): t for t in snapshot.get("triples", [])},
            "episodes": snapshot.get("episodes", []),
            "memory": snapshot.get("memory", {}),
        }
        self._write_file_store(data)
        return {"run_id": run_id, "imported": True}

    def clear_run_graph(self, run_id: str) -> Dict[str, Any]:
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                session.run(
                    """
                    MATCH (n {group_id: $run_id})
                    DETACH DELETE n
                    """,
                    run_id=run_id,
                )
            return {"run_id": run_id, "cleared": True}

        data = self._read_file_store()
        data["runs"].pop(run_id, None)
        self._write_file_store(data)
        return {"run_id": run_id, "cleared": True}

    def add_episode(self, run_id: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        episode_uuid = self._episode_uuid(run_id, content, metadata_json)
        if self.driver:
            with self.driver.session(database=self.neo4j_database) as session:
                session.run(
                    """
                    MERGE (ep:MiroFishEpisode {group_id: $run_id, uuid: $uuid})
                    ON CREATE SET ep.created_at = datetime()
                    SET ep.content = $content,
                        ep.metadata_json = $metadata_json,
                        ep.updated_at = datetime()
                    """,
                    run_id=run_id,
                    uuid=episode_uuid,
                    content=content,
                    metadata_json=metadata_json,
                )
            return {"run_id": run_id, "uuid": episode_uuid}

        data = self._read_file_store()
        run = self._file_run(data, run_id)
        episode = {"content": content, "metadata": metadata or {}}
        run["episodes"].append(episode)
        self._write_file_store(data)
        return {"run_id": run_id, "episode_index": len(run["episodes"]) - 1}

    def snapshot(self, run_id: str) -> Dict[str, Any]:
        if self.driver:
            return {
                "run_id": run_id,
                "provider": "graphiti",
                "store": "neo4j",
                "entities": self.list_entities(run_id),
                "triples": self._list_facts_neo4j(run_id),
                "episodes": self._list_episodes_neo4j(run_id),
                "memory": self._list_memory_neo4j(run_id),
            }

        data = self._read_file_store()
        run = self._file_run(data, run_id)
        return {
            "run_id": run_id,
            "provider": "graphiti",
            "store": "neo4j" if self.driver else "file",
            "entities": list(run["entities"].values()),
            "triples": list(run["triples"].values()),
            "episodes": run["episodes"],
            "memory": run["memory"],
        }

    def timeline(self, run_id: str) -> List[Dict[str, Any]]:
        triples = self.snapshot(run_id)["triples"]
        return sorted(
            [
                {
                    "valid_at": triple.get("valid_at"),
                    "invalid_at": triple.get("invalid_at"),
                    "fact": triple.get("fact"),
                    "source": triple.get("source"),
                }
                for triple in triples
            ],
            key=lambda item: item.get("valid_at") or "",
        )

    def _ensure_constraints(self) -> None:
        with self.driver.session(database=self.neo4j_database) as session:
            session.run(
                "CREATE CONSTRAINT mirofish_entity IF NOT EXISTS FOR (e:MiroFishEntity) REQUIRE (e.group_id, e.normalized_name) IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT mirofish_episode IF NOT EXISTS FOR (ep:MiroFishEpisode) REQUIRE (ep.group_id, ep.uuid) IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT mirofish_agent_memory IF NOT EXISTS FOR (m:MiroFishAgentMemory) REQUIRE (m.group_id, m.agent_id) IS UNIQUE"
            )

    def _list_facts_neo4j(self, run_id: str, limit: int = 100_000) -> List[Dict[str, Any]]:
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                """
                MATCH (s:MiroFishEntity {group_id: $run_id})-[r:MIROFISH_FACT]->(o:MiroFishEntity {group_id: $run_id})
                RETURN s, r, o
                LIMIT $limit
                """,
                run_id=run_id,
                limit=limit,
            )
            return [self._record_to_fact(row) for row in result]

    def _list_episodes_neo4j(self, run_id: str) -> List[Dict[str, Any]]:
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                """
                MATCH (ep:MiroFishEpisode {group_id: $run_id})
                RETURN ep
                ORDER BY ep.created_at
                """,
                run_id=run_id,
            )
            episodes = []
            for row in result:
                episode = dict(row["ep"].items())
                metadata = json.loads(episode.pop("metadata_json", "{}") or "{}")
                episodes.append({"content": episode.get("content", ""), "metadata": metadata, "uuid": episode.get("uuid")})
            return episodes

    def _list_memory_neo4j(self, run_id: str) -> Dict[str, Any]:
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                """
                MATCH (m:MiroFishAgentMemory {group_id: $run_id})
                RETURN m.agent_id AS agent_id, properties(m) AS props
                """,
                run_id=run_id,
            )
            return {row["agent_id"]: json.loads((row["props"] or {}).get("memory_json") or "{}") for row in result}

    def _add_triplet_neo4j(self, run_id: str, triple: GraphTriple) -> Dict[str, Any]:
        subject_norm = self.normalize_entity(triple.subject)
        object_norm = self.normalize_entity(triple.object)
        triple_id = self._triple_uuid(run_id, triple.model_dump())
        with self.driver.session(database=self.neo4j_database) as session:
            session.run(
                """
                MERGE (s:MiroFishEntity {group_id: $run_id, normalized_name: $subject_norm})
                ON CREATE SET s.uuid = $subject_uuid, s.name = $subject, s.labels = ['Entity'], s.created_at = datetime()
                MERGE (o:MiroFishEntity {group_id: $run_id, normalized_name: $object_norm})
                ON CREATE SET o.uuid = $object_uuid, o.name = $object, o.labels = ['Entity'], o.created_at = datetime()
                MERGE (s)-[r:MIROFISH_FACT {uuid: $triple_id, group_id: $run_id}]->(o)
                SET r.predicate = $predicate,
                    r.fact = $fact,
                    r.valid_at = $valid_at,
                    r.invalid_at = $invalid_at,
                    r.source = $source,
                    r.source_file = $source_file,
                    r.evidence = $evidence,
                    r.confidence = $confidence,
                    r.metadata_json = $metadata_json
                """,
                run_id=run_id,
                subject_norm=subject_norm,
                object_norm=object_norm,
                subject_uuid=self._entity_uuid(run_id, subject_norm),
                object_uuid=self._entity_uuid(run_id, object_norm),
                subject=triple.subject,
                object=triple.object,
                triple_id=triple_id,
                predicate=triple.predicate,
                fact=triple.fact,
                valid_at=triple.valid_at,
                invalid_at=triple.invalid_at,
                source=triple.source,
                source_file=triple.source_file,
                evidence=triple.evidence,
                confidence=triple.confidence,
                metadata_json=json.dumps(triple.metadata, ensure_ascii=False),
            )
        return {"uuid": triple_id}

    def _add_triplet_file(self, run_id: str, triple: GraphTriple) -> Dict[str, Any]:
        data = self._read_file_store()
        run = self._file_run(data, run_id)
        self.get_or_create_entity_node(run_id, triple.subject)
        self.get_or_create_entity_node(run_id, triple.object)
        data = self._read_file_store()
        run = self._file_run(data, run_id)
        triple_data = triple.model_dump()
        triple_data["uuid"] = self._triple_uuid(run_id, triple_data)
        run["triples"][triple_data["uuid"]] = triple_data
        self._write_file_store(data)
        return {"uuid": triple_data["uuid"]}

    def _record_to_fact(self, row: Any) -> Dict[str, Any]:
        rel = dict(row["r"].items())
        return {
            "subject": row["s"].get("name"),
            "predicate": rel.get("predicate"),
            "object": row["o"].get("name"),
            "fact": rel.get("fact"),
            "valid_at": rel.get("valid_at"),
            "invalid_at": rel.get("invalid_at"),
            "source": rel.get("source"),
            "source_file": rel.get("source_file"),
            "evidence": rel.get("evidence"),
            "confidence": rel.get("confidence"),
            "metadata": json.loads(rel.get("metadata_json") or "{}"),
            "uuid": rel.get("uuid"),
        }

    def _to_jsonable(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._to_jsonable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._to_jsonable(item) for item in value]
        if hasattr(value, "iso_format"):
            return value.iso_format()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value

    def _query_terms(self, query: str) -> List[str]:
        terms = {query}
        terms.update(token for token in re.split(r"\s+", query) if len(token) >= 2)
        for chunk in re.findall(r"[\w\u4e00-\u9fff]+", query):
            if len(chunk) >= 2:
                terms.add(chunk)
            if len(chunk) >= 5:
                max_size = min(12, len(chunk))
                for size in range(max_size, 3, -1):
                    for start in range(0, len(chunk) - size + 1):
                        terms.add(chunk[start : start + size])
        return sorted(terms, key=len, reverse=True)

    def _entity_uuid(self, run_id: str, normalized: str) -> str:
        return hashlib.sha256(f"{run_id}:entity:{normalized}".encode("utf-8")).hexdigest()

    def _episode_uuid(self, run_id: str, content: str, metadata_json: str) -> str:
        return hashlib.sha256(f"{run_id}:episode:{content}:{metadata_json}".encode("utf-8")).hexdigest()

    def _memory_uuid(self, run_id: str, agent_id: str) -> str:
        return hashlib.sha256(f"{run_id}:memory:{agent_id}".encode("utf-8")).hexdigest()

    def _clean_triple_payload(self, triple: Dict[str, Any]) -> Dict[str, Any]:
        return {key: triple.get(key) for key in GraphTriple.model_fields}

    def _triple_uuid(self, run_id: str, triple: Dict[str, Any] | GraphTriple) -> str:
        payload = triple.model_dump() if isinstance(triple, GraphTriple) else triple
        stable = json.dumps(
            {
                "subject": self.normalize_entity(payload["subject"]),
                "predicate": payload["predicate"],
                "object": self.normalize_entity(payload["object"]),
                "fact": payload["fact"],
                "valid_at": payload.get("valid_at"),
                "invalid_at": payload.get("invalid_at"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(f"{run_id}:triple:{stable}".encode("utf-8")).hexdigest()

    def _file_run(self, data: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        return data.setdefault("runs", {}).setdefault(
            run_id,
            {"entities": {}, "triples": {}, "episodes": [], "memory": {}},
        )

    def _read_file_store(self) -> Dict[str, Any]:
        if not self.store_path.exists():
            return {"runs": {}}
        return json.loads(self.store_path.read_text(encoding="utf-8"))

    def _write_file_store(self, data: Dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class GraphitiGraphProvider(GraphProvider):
    name = "graphiti"

    def __init__(self, store: Optional[GraphitiCompatibilityStore] = None, *, require_graphiti_package: bool = False):
        if require_graphiti_package and importlib.util.find_spec("graphiti_core") is None:
            raise GraphitiDependencyError(
                "graphiti_core is not installed. Install Graphiti or use the no-LLM compatibility store."
            )
        self.store = store or GraphitiCompatibilityStore()

    def add_episode(self, run_id: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.store.add_episode(run_id, content, metadata)

    def add_triples(self, run_id: str, triples: List[Dict[str, Any] | GraphTriple]) -> Dict[str, Any]:
        parsed = [triple if isinstance(triple, GraphTriple) else GraphTriple.model_validate(triple) for triple in triples]
        return self.store.add_triples(run_id, parsed)

    def search(self, run_id: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        facts = self.store.search_facts(run_id, query, limit)
        if len(facts) < limit:
            facts.extend({"node": node} for node in self.store.search_nodes(run_id, query, limit - len(facts)))
        return facts[:limit]

    def neighbors(self, run_id: str, entity: str, depth: int = 2) -> List[Dict[str, Any]]:
        return self.store.neighbors(run_id, entity, depth)

    def list_entities(self, run_id: str) -> List[Dict[str, Any]]:
        return self.store.list_entities(run_id)

    def get_entity(self, run_id: str, entity: str) -> Optional[Dict[str, Any]]:
        return self.store.get_entity(run_id, entity)

    def update_memory(self, run_id: str, agent_id: str, memory: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.update_memory(run_id, agent_id, memory)

    def get_agent_memory(self, run_id: str, agent_id: str) -> Dict[str, Any]:
        return self.store.get_agent_memory(run_id, agent_id)

    def write_agent_memory(self, run_id: str, agent_id: str, memory: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.write_agent_memory(run_id, agent_id, memory)

    def export_snapshot(self, run_id: str, output_path: str) -> Dict[str, Any]:
        return self.store.export_snapshot(run_id, output_path)

    def import_snapshot(self, run_id: str, input_path: str) -> Dict[str, Any]:
        return self.store.import_snapshot(run_id, input_path)

    def clear_run_graph(self, run_id: str) -> Dict[str, Any]:
        return self.store.clear_run_graph(run_id)

    def export_timeline(self, run_id: str, output_path: str) -> Dict[str, Any]:
        timeline = self.store.timeline(run_id)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"output_path": str(path), "events": len(timeline)}
