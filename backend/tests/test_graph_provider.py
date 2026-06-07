import json
from pathlib import Path

import pytest

from app.adapters.graph.graphiti import (
    GraphitiCompatibilityStore,
    GraphitiDependencyError,
    GraphitiGraphProvider,
)
from app.adapters.graph.base import GraphTriple


def test_graphiti_provider_add_search_export_without_openai_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "store.json"))
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")

    provider = GraphitiGraphProvider()
    provider.add_triples(
        "run",
        [
            {
                "subject": "美国商务部",
                "predicate": "限制",
                "object": "先进AI芯片出口",
                "fact": "美国商务部限制先进AI芯片出口。",
                "valid_at": "2024-01-01",
                "invalid_at": None,
                "source": "现实种子",
                "source_file": "seed.md",
                "evidence": "限制先进AI芯片出口",
                "confidence": 0.82,
                "metadata": {},
            }
        ],
    )
    results = provider.search("run", "AI芯片", limit=5)
    assert results
    assert results[0]["fact"] == "美国商务部限制先进AI芯片出口。"

    out = tmp_path / "snapshot.json"
    provider.export_snapshot("run", str(out))
    snapshot = json.loads(out.read_text(encoding="utf-8"))
    assert snapshot["run_id"] == "run"
    assert len(snapshot["triples"]) == 1


def test_graphiti_missing_dependency_error_is_clear(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None if name == "graphiti_core" else object())
    with pytest.raises(GraphitiDependencyError, match="graphiti_core is not installed"):
        GraphitiGraphProvider(require_graphiti_package=True)


def test_graphiti_auto_store_requires_neo4j_driver(monkeypatch):
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "auto")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None if name == "neo4j" else object())

    with pytest.raises(GraphitiDependencyError, match="neo4j Python package is required"):
        GraphitiCompatibilityStore()


def test_graphiti_file_store_explicitly_skips_neo4j_dependency(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "store.json"))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None if name == "neo4j" else object())

    store = GraphitiCompatibilityStore()

    assert store.driver is None
    assert (tmp_path / "store.json").exists()


def test_graphiti_compatibility_store_encapsulates_schema_assumptions():
    assert hasattr(GraphitiCompatibilityStore, "_add_triplet_neo4j")
    assert not hasattr(GraphitiGraphProvider, "_add_triplet_neo4j")


def test_graphiti_snapshot_uses_neo4j_branch_when_driver_present():
    store = GraphitiCompatibilityStore.__new__(GraphitiCompatibilityStore)
    store.driver = object()
    store.list_entities = lambda run_id: [{"name": "A"}]
    store._list_facts_neo4j = lambda run_id: [{"subject": "A", "object": "B", "uuid": "internal"}]
    store._list_episodes_neo4j = lambda run_id: [{"content": "episode"}]
    store._list_memory_neo4j = lambda run_id: {"agent_1": {"belief": "x"}}

    snapshot = store.snapshot("run")

    assert snapshot["store"] == "neo4j"
    assert snapshot["entities"] == [{"name": "A"}]
    assert snapshot["triples"][0]["uuid"] == "internal"
    assert snapshot["memory"]["agent_1"]["belief"] == "x"


def test_graphiti_neo4j_import_snapshot_cleans_internal_uuid(tmp_path):
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "entities": [{"name": "A", "labels": ["Entity"]}],
                "triples": [
                    {
                        "uuid": "internal",
                        "subject": "A",
                        "predicate": "affects",
                        "object": "B",
                        "fact": "A affects B.",
                        "valid_at": None,
                        "invalid_at": None,
                        "source": "seed",
                        "source_file": "seed.md",
                        "evidence": "A affects B.",
                        "confidence": 0.9,
                        "metadata": {},
                    }
                ],
                "episodes": [{"content": "episode", "metadata": {"source": "seed"}}],
                "memory": {"agent_1": {"belief": "x"}},
            }
        ),
        encoding="utf-8",
    )
    imported = {}
    store = GraphitiCompatibilityStore.__new__(GraphitiCompatibilityStore)
    store.driver = object()
    store.clear_run_graph = lambda run_id: imported.setdefault("cleared", run_id)
    store.get_or_create_entity_node = lambda run_id, name, labels=None: imported.setdefault("entity", (run_id, name, labels))
    store.add_episode = lambda run_id, content, metadata=None: imported.setdefault("episode", (run_id, content, metadata))
    store.write_agent_memory = lambda run_id, agent_id, memory: imported.setdefault("memory", (run_id, agent_id, memory))

    def add_triples(run_id, triples):
        imported["triples"] = triples
        return {"triples_added": len(triples)}

    store.add_triples = add_triples

    result = store.import_snapshot("run", str(snapshot_path))

    assert result["imported"] is True
    assert isinstance(imported["triples"][0], GraphTriple)
    assert imported["triples"][0].subject == "A"


def test_graphiti_neo4j_neighbors_respects_depth():
    captured = {}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, query, **params):
            captured["query"] = query
            captured["params"] = params
            return []

    class FakeDriver:
        def session(self, database=None):
            captured["database"] = database
            return FakeSession()

    store = GraphitiCompatibilityStore.__new__(GraphitiCompatibilityStore)
    store.driver = FakeDriver()
    store.neo4j_database = "neo4j"

    assert store.neighbors("run", "A", depth=4) == []

    assert "[*1..4]" in captured["query"]
    assert captured["params"] == {"run_id": "run", "normalized": "a"}


def test_graphiti_neo4j_search_does_not_shadow_driver_query_argument():
    captured = {}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, cypher, **params):
            captured["cypher"] = cypher
            captured["params"] = params
            return []

    class FakeDriver:
        def session(self, database=None):
            return FakeSession()

    store = GraphitiCompatibilityStore.__new__(GraphitiCompatibilityStore)
    store.driver = FakeDriver()
    store.neo4j_database = "neo4j"

    assert store.search_facts("run", "AI芯片", limit=5) == []

    assert "$search_query" in captured["cypher"]
    assert captured["params"] == {"run_id": "run", "search_query": "AI芯片", "limit": 5}


def test_graphiti_neo4j_values_are_jsonable():
    class FakeDateTime:
        def iso_format(self):
            return "2026-06-06T00:00:00Z"

    store = GraphitiCompatibilityStore.__new__(GraphitiCompatibilityStore)

    assert store._to_jsonable({"created_at": FakeDateTime(), "items": [FakeDateTime()]}) == {
        "created_at": "2026-06-06T00:00:00Z",
        "items": ["2026-06-06T00:00:00Z"],
    }
