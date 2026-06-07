import json
from pathlib import Path

from app.adapters.llm.base import LLMTask
from app.adapters.llm.mock import MockLLMProvider
from app.agent_engine.contracts import TASK_OUTPUT_SCHEMAS
from app.agent_engine.schemas import AGENT_TASK_TYPES
from app.agent_engine.json_schema import TRIPLE_SCHEMA, object_schema, validate_json_schema
from app.agent_engine.queue import AgentQueue
from app.agent_engine.state import RunStore


def _init_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    store = RunStore(run_dir)
    store.init_state("run", "test requirement", [])
    return run_dir


def test_agent_queue_strict_response_lifecycle(tmp_path):
    run_dir = _init_run(tmp_path)
    queue = AgentQueue(run_dir)
    need = queue.create_request(
        run_id="run",
        task_type="extract_triples",
        stage="graph",
        expected_schema=object_schema({"triples": {"type": "array", "items": TRIPLE_SCHEMA}}, ["triples"]),
    )

    response_path = Path(need.expected_response_file)
    response_path.write_text(
        json.dumps(
            {
                "request_id": need.request_id,
                "status": "ok",
                "output": {
                    "triples": [
                        {
                            "subject": "A",
                            "predicate": "affects",
                            "object": "B",
                            "fact": "A affects B.",
                            "valid_at": None,
                            "invalid_at": None,
                            "source": "seed",
                            "source_file": "seed.md",
                            "evidence": "A affects B.",
                            "confidence": 0.8,
                            "metadata": {},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = queue.validate_response_file(response_path)
    assert result.ok, result.errors


def test_agent_queue_rejects_missing_fields_and_bad_confidence(tmp_path):
    run_dir = _init_run(tmp_path)
    queue = AgentQueue(run_dir)
    need = queue.create_request(
        run_id="run",
        task_type="extract_triples",
        stage="graph",
        expected_schema=object_schema({"triples": {"type": "array", "items": TRIPLE_SCHEMA}}, ["triples"]),
    )
    bad = Path(need.expected_response_file)
    bad.write_text(
        json.dumps(
            {
                "request_id": need.request_id,
                "status": "ok",
                "output": {
                    "triples": [
                        {
                            "subject": "A",
                            "predicate": "affects",
                            "object": "B",
                            "fact": "A affects B.",
                            "valid_at": None,
                            "invalid_at": None,
                            "source": "seed",
                            "source_file": "seed.md",
                            "evidence": "A affects B.",
                            "confidence": 1.2,
                            "metadata": {},
                            "extra": "not allowed",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = queue.submit_response(bad)
    assert not result.ok
    assert any("confidence" in error for error in result.errors)
    assert any("extra field" in error for error in result.errors)
    assert result.repair_request is not None


def test_agent_queue_persists_repair_attempts(tmp_path):
    run_dir = _init_run(tmp_path)
    queue = AgentQueue(run_dir)
    need = queue.create_request(
        run_id="run",
        task_type="generate_report",
        stage="report",
        expected_schema=object_schema({"report_markdown": {"type": "string"}}, ["report_markdown"]),
        retry_policy={"max_repair_attempts": 1},
    )
    bad = Path(need.expected_response_file)
    bad.write_text(
        json.dumps(
            {
                "request_id": need.request_id,
                "status": "ok",
                "output": {},
            }
        ),
        encoding="utf-8",
    )

    first = queue.submit_response(bad)
    second = queue.submit_response(bad)

    assert not first.ok
    assert first.repair_request is not None
    assert not second.ok
    assert second.repair_request is None
    assert queue.load_request(need.request_id).retry_policy.repair_attempts_used == 1


def test_agent_queue_requires_filename_request_id_match(tmp_path):
    run_dir = _init_run(tmp_path)
    queue = AgentQueue(run_dir)
    first = queue.create_request(
        run_id="run",
        task_type="generate_report",
        stage="report",
        expected_schema=object_schema({"report_markdown": {"type": "string"}}, ["report_markdown"]),
    )
    second = queue.create_request(
        run_id="run",
        task_type="generate_report",
        stage="report",
        expected_schema=object_schema({"report_markdown": {"type": "string"}}, ["report_markdown"]),
    )
    response_path = run_dir / "responses" / f"{second.request_id}.json"
    response_path.write_text(
        json.dumps(
            {
                "request_id": first.request_id,
                "status": "ok",
                "output": {"report_markdown": "ok"},
            }
        ),
        encoding="utf-8",
    )

    result = queue.validate_response_file(response_path)

    assert not result.ok
    assert any("does not match response file name" in error for error in result.errors)


def test_agent_queue_validates_skipped_output_against_expected_schema(tmp_path):
    run_dir = _init_run(tmp_path)
    queue = AgentQueue(run_dir)
    need = queue.create_request(
        run_id="run",
        task_type="generate_report",
        stage="report",
        expected_schema=object_schema({"report_markdown": {"type": "string"}}, ["report_markdown"]),
    )
    response_path = Path(need.expected_response_file)
    response_path.write_text(
        json.dumps(
            {
                "request_id": need.request_id,
                "status": "skipped",
                "output": {},
            }
        ),
        encoding="utf-8",
    )

    result = queue.validate_response_file(response_path)

    assert not result.ok
    assert any("missing required field" in error for error in result.errors)


def test_agent_queue_supports_all_declared_task_types(tmp_path):
    run_dir = _init_run(tmp_path)
    queue = AgentQueue(run_dir)
    schema = object_schema({"result": {"type": "object"}}, ["result"])

    created = []
    for task_type in sorted(AGENT_TASK_TYPES):
        need = queue.create_request(
            run_id="run",
            task_type=task_type,
            stage=task_type,
            expected_schema=schema,
            structured_input={"task_type": task_type},
        )
        created.append(need.request_id)

    assert len(created) == len(AGENT_TASK_TYPES)
    assert len(queue.list_requests()) == len(AGENT_TASK_TYPES)


def test_all_task_types_have_strict_schema_and_mock_output():
    assert set(TASK_OUTPUT_SCHEMAS) == AGENT_TASK_TYPES
    provider = MockLLMProvider()

    for task_type in sorted(AGENT_TASK_TYPES):
        schema = TASK_OUTPUT_SCHEMAS[task_type]
        result = provider.run_task(
            LLMTask(
                run_id="run",
                task_type=task_type,
                stage=task_type,
                expected_schema=schema,
                structured_input={
                    "actions": [{"agent_id": "agent_1", "action_id": "action_1"}],
                    "candidate": {},
                    "invalid_response": {"output": {}},
                },
            )
        )

        assert result.status == "ok"
        assert not validate_json_schema(result.output, schema), task_type
