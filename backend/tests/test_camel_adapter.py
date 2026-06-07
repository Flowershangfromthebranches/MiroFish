from app.adapters.llm.camel_adapter import AgentModelBackendAdapter
from app.adapters.llm.agent_queue import AgentQueueLLMProvider
from app.adapters.llm.mock import MockLLMProvider
from app.adapters.llm.agent_runtime import AgentRuntime
from app.agent_engine.queue import AgentQueue
from camel.models import BaseModelBackend


def test_agent_model_backend_adapter_batches_actions(tmp_path):
    runtime = AgentRuntime(provider=MockLLMProvider(), run_dir=str(tmp_path))
    adapter = AgentModelBackendAdapter("run", str(tmp_path), runtime=runtime)
    result = adapter.run_batch_actions(
        "round_1",
        [
            {"agent_id": "a1", "action_id": "x1"},
            {"agent_id": "a2", "action_id": "x2"},
        ],
    )
    assert result["status"] == "ok"
    actions = result["output"]["actions"]
    assert {item["action_id"] for item in actions} == {"x1", "x2"}


def test_agent_model_backend_adapter_is_camel_backend_and_returns_tool_call(tmp_path):
    runtime = AgentRuntime(provider=MockLLMProvider(), run_dir=str(tmp_path))
    adapter = AgentModelBackendAdapter("run", str(tmp_path), runtime=runtime)
    assert isinstance(adapter, BaseModelBackend)

    response = adapter.run(
        [{"role": "user", "content": "Act now"}],
        tools=[{"type": "function", "function": {"name": "create_post", "parameters": {"type": "object"}}}],
    )
    tool_calls = response.choices[0].message.tool_calls
    assert tool_calls
    assert tool_calls[0].function.name == "create_post"


def test_agent_model_backend_adapter_agent_queue_generates_request_without_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ZEP_API_KEY", raising=False)

    runtime = AgentRuntime(provider=AgentQueueLLMProvider(run_dir=tmp_path), run_dir=str(tmp_path))
    adapter = AgentModelBackendAdapter("run", str(tmp_path), runtime=runtime)
    response = adapter.run(
        [{"role": "user", "content": "Act in the simulation round"}],
        tools=[{"type": "function", "function": {"name": "do_nothing", "parameters": {"type": "object"}}}],
    )
    assert response.choices[0].message.tool_calls[0].function.name == "do_nothing"
    requests = AgentQueue(tmp_path).list_requests()
    assert requests
    assert requests[0]["type"] == "simulate_agent_action"
    assert adapter.last_need_agent_response is not None
    assert adapter.last_need_agent_response["status"] == "need_agent_response"
    assert adapter.last_need_agent_response["request_id"] == requests[0]["request_id"]


def test_agent_model_backend_adapter_records_batch_need_agent_response(tmp_path):
    runtime = AgentRuntime(provider=AgentQueueLLMProvider(run_dir=tmp_path), run_dir=str(tmp_path))
    adapter = AgentModelBackendAdapter("run", str(tmp_path), runtime=runtime)

    result = adapter.run_batch_actions(
        "round_1",
        [
            {"agent_id": "a1", "action_id": "x1"},
            {"agent_id": "a2", "action_id": "x2"},
        ],
    )

    requests = AgentQueue(tmp_path).list_requests()
    assert result["status"] == "need_agent_response"
    assert adapter.last_need_agent_response is not None
    assert adapter.last_need_agent_response["request_id"] == result["request_id"]
    request = AgentQueue(tmp_path).load_request(result["request_id"])
    assert len(request.structured_input["actions"]) == 2
    assert {item["action_id"] for item in request.structured_input["actions"]} == {"x1", "x2"}
    assert requests[0]["type"] == "simulate_agent_action"
