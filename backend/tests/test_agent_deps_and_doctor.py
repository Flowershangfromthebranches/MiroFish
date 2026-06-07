import importlib.util
import json
import sys
import types
from pathlib import Path

import tomllib

from app.agent_engine.runner import PredictionRunService


def test_agent_optional_dependencies_declared():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    agent_deps = set(pyproject["project"]["optional-dependencies"]["agent"])
    assert {"graphiti-core", "neo4j", "mcp"}.issubset(agent_deps)


class _FakeResult:
    def __init__(self, version="5.26.0"):
        self.version = version

    def single(self):
        return {"versions": [self.version]}


class _FakeSession:
    def __init__(self, version="5.26.0"):
        self.version = version

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def run(self, query):
        assert "CALL dbms.components()" in query
        return _FakeResult(self.version)


class _FakeDriver:
    def __init__(self, version="5.26.0"):
        self.version = version

    def session(self, database):
        assert database == "neo4j"
        return _FakeSession(self.version)

    def close(self):
        return None


def _fake_graph_database(version="5.26.0"):
    class FakeGraphDatabase:
        @staticmethod
        def driver(uri, auth):
            assert uri == "bolt://localhost:7687"
            assert auth == ("neo4j", "password")
            return _FakeDriver(version)

    return FakeGraphDatabase


class _FakeUrlResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _set_agent_env(monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.delenv("MIROFISH_GRAPHITI_STORE", raising=False)
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "password")
    monkeypatch.setenv("NEO4J_DATABASE", "neo4j")
    monkeypatch.setenv("MIROFISH_GRAPH_SEARCH_MODE", "semantic")
    monkeypatch.setenv("MIROFISH_EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")


def _mock_agent_deps(monkeypatch, ollama_payload, *, neo4j_version="5.26.0"):
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name):
        if name in {"graphiti_core", "neo4j", "mcp"}:
            return object()
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setitem(sys.modules, "neo4j", types.SimpleNamespace(GraphDatabase=_fake_graph_database(neo4j_version)))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout: _FakeUrlResponse(ollama_payload),
    )


def test_doctor_agent_graphiti_checks_neo4j_and_ollama(monkeypatch, tmp_path):
    _set_agent_env(monkeypatch)
    _mock_agent_deps(monkeypatch, {"models": [{"name": "nomic-embed-text:latest"}]})

    result = PredictionRunService().doctor(str(tmp_path))

    assert result["status"] == "ok"
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["graphiti_package"]["ok"] is True
    assert checks["neo4j_connectable"]["ok"] is True
    assert checks["neo4j_version_supported"]["ok"] is True
    assert checks["ollama_connectable"]["ok"] is True
    assert checks["ollama_embedding_model"]["ok"] is True
    assert checks["docker"]["required"] is False
    assert checks["docker_compose"]["required"] is False


def test_doctor_agent_graphiti_fails_when_ollama_model_missing(monkeypatch, tmp_path):
    _set_agent_env(monkeypatch)
    _mock_agent_deps(monkeypatch, {"models": []})

    result = PredictionRunService().doctor(str(tmp_path))

    assert result["status"] == "failed"
    hard_failures = {check["name"] for check in result["hard_failures"]}
    assert "ollama_embedding_model" in hard_failures


def test_doctor_does_not_require_ollama_when_embedding_provider_is_not_ollama(monkeypatch, tmp_path):
    _set_agent_env(monkeypatch)
    monkeypatch.setenv("MIROFISH_EMBEDDING_PROVIDER", "none")
    _mock_agent_deps(monkeypatch, {"models": []})

    result = PredictionRunService().doctor(str(tmp_path))

    assert result["status"] == "ok"
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["ollama_connectable"]["required"] is False
    assert checks["ollama_embedding_model"]["required"] is False


def test_doctor_does_not_require_ollama_for_fulltext_search(monkeypatch, tmp_path):
    _set_agent_env(monkeypatch)
    monkeypatch.setenv("MIROFISH_GRAPH_SEARCH_MODE", "fulltext")
    monkeypatch.setenv("MIROFISH_EMBEDDING_PROVIDER", "ollama")
    _mock_agent_deps(monkeypatch, {"models": []})

    result = PredictionRunService().doctor(str(tmp_path))

    assert result["status"] == "ok"
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["graph_search_mode"]["value"] == "fulltext"
    assert checks["ollama_connectable"]["required"] is False
    assert "fulltext search does not require Ollama" in checks["ollama_connectable"]["value"]


def test_doctor_requires_neo4j_526_or_newer(monkeypatch, tmp_path):
    _set_agent_env(monkeypatch)
    monkeypatch.setenv("MIROFISH_EMBEDDING_PROVIDER", "none")
    _mock_agent_deps(monkeypatch, {"models": []}, neo4j_version="5.25.0")

    result = PredictionRunService().doctor(str(tmp_path))

    assert result["status"] == "failed"
    hard_failures = {check["name"] for check in result["hard_failures"]}
    assert "neo4j_version_supported" in hard_failures


def test_doctor_hard_failures_are_limited_to_dependency_and_service_gates(monkeypatch, tmp_path):
    _set_agent_env(monkeypatch)
    monkeypatch.setenv("MIROFISH_GRAPH_SEARCH_MODE", "not-a-mode")
    monkeypatch.setenv("MIROFISH_EMBEDDING_PROVIDER", "not-a-provider")
    _mock_agent_deps(monkeypatch, {"models": []})

    result = PredictionRunService().doctor(str(tmp_path))

    allowed = {
        "graphiti_package",
        "neo4j_package",
        "neo4j_connectable",
        "neo4j_version_supported",
        "ollama_connectable",
        "ollama_embedding_model",
    }
    assert {check["name"] for check in result["hard_failures"]}.issubset(allowed)
