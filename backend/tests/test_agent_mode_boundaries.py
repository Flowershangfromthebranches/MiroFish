import importlib.util
import tomllib
from pathlib import Path

from app.config import Config


def test_agent_mode_config_does_not_require_model_or_zep_keys(monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    assert Config.validate() == []


def test_legacy_llm_client_mock_mode_does_not_require_api_key(tmp_path, monkeypatch):
    from app.utils.llm_client import LLMClient

    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "mock")
    monkeypatch.setattr(Config, "MIROFISH_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    client = LLMClient()
    result = client.chat([{"role": "user", "content": "hello"}])

    assert result == "Mock text response generated without model APIs."


def test_flask_api_returns_need_agent_response_as_structured_json():
    from app import create_app
    from app.adapters.llm.agent_runtime import NeedAgentResponse
    from app.adapters.llm.base import LLMProviderResult

    app = create_app()

    @app.route("/_test_need_agent_response")
    def _test_need_agent_response():
        raise NeedAgentResponse(
            LLMProviderResult(
                status="need_agent_response",
                request_id="req_000001",
                request_file="/tmp/req_000001.json",
                expected_response_file="/tmp/resp_000001.json",
            )
        )

    response = app.test_client().get("/_test_need_agent_response")

    assert response.status_code == 202
    assert response.get_json() == {
        "status": "need_agent_response",
        "request_id": "req_000001",
        "request_file": "/tmp/req_000001.json",
        "expected_response_file": "/tmp/resp_000001.json",
    }


def test_flask_api_zep_guard_is_provider_aware(monkeypatch):
    from app.api.graph import _legacy_zep_config_errors
    from app.api.simulation import _legacy_zep_required

    monkeypatch.setattr(Config, "ZEP_API_KEY", None)
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    assert _legacy_zep_config_errors() == []
    assert _legacy_zep_required() is False

    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "zep")
    assert _legacy_zep_config_errors()
    assert _legacy_zep_required() is True


def test_provider_boundary_checker_catches_legacy_adapter_and_schema_leaks(tmp_path):
    checker_path = Path(__file__).resolve().parents[2] / "scripts" / "check_provider_boundaries.py"
    spec = importlib.util.spec_from_file_location("check_provider_boundaries", checker_path)
    checker = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(checker)

    business = tmp_path / "backend" / "app" / "services" / "bad_business.py"
    business.parent.mkdir(parents=True)
    business.write_text(
        "from app.adapters.graph.zep import ZepGraphProvider\n"
        "QUERY = 'MATCH (n:MiroFishEntity) RETURN n'\n",
        encoding="utf-8",
    )

    violations = checker.collect_violations(
        [tmp_path / "backend" / "app"],
        root=tmp_path,
        allowed=set(),
        allowed_legacy_adapter_imports=set(),
        allowed_graphiti_schema=set(),
    )
    assert any("imports legacy provider adapter directly" in violation for violation in violations)
    assert any("Graphiti/Neo4j schema assumption" in violation for violation in violations)


def test_legacy_sdks_are_optional_dependencies_only():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    default_dependencies = set(data["project"]["dependencies"])
    legacy_dependencies = set(data["project"]["optional-dependencies"]["legacy"])

    assert not any(dependency.startswith("openai") for dependency in default_dependencies)
    assert not any(dependency.startswith("zep-cloud") for dependency in default_dependencies)
    assert any(dependency.startswith("openai") for dependency in legacy_dependencies)
    assert any(dependency.startswith("zep-cloud") for dependency in legacy_dependencies)
