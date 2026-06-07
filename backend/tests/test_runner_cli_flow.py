import json
from pathlib import Path

from app.adapters.graph.factory import create_graph_provider
from app.agent_engine.cli import build_parser
from app.agent_engine.queue import AgentQueue
from app.agent_engine.runner import PredictionRunService


def _write_response(run_dir: Path, payload: dict) -> None:
    requests = AgentQueue(run_dir).list_requests()
    request_id = requests[-1]["request_id"]
    response_path = run_dir / "responses" / f"{request_id}.json"
    response_path.write_text(
        json.dumps({"request_id": request_id, "status": "ok", "output": payload}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_full_agent_queue_runner_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "graph_store.json"))
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ZEP_API_KEY", raising=False)

    seed = tmp_path / "seed.md"
    seed.write_text("美国商务部限制先进AI芯片出口。", encoding="utf-8")
    run_dir = tmp_path / "chip-2036"
    service = PredictionRunService()
    created = service.create_run(str(seed), "预测未来10年全球芯片能力格局变化", str(run_dir))
    assert created["status"] == "created"

    need = service.run(str(run_dir))
    assert need["status"] == "need_agent_response"
    _write_response(run_dir, {"ontology": {"entity_types": [], "edge_types": []}})

    need = service.resume(str(run_dir))
    assert need["type"] == "extract_triples"
    _write_response(
        run_dir,
        {
            "triples": [
                {
                    "subject": "美国商务部",
                    "predicate": "限制",
                    "object": "先进AI芯片出口",
                    "fact": "美国商务部限制先进AI芯片出口。",
                    "valid_at": "2024-01-01",
                    "invalid_at": None,
                    "source": "现实种子",
                    "source_file": "seed.md",
                    "evidence": "美国商务部限制先进AI芯片出口。",
                    "confidence": 0.82,
                    "metadata": {},
                }
            ]
        },
    )

    need = service.resume(str(run_dir))
    assert need["type"] == "generate_oasis_profiles"
    _write_response(run_dir, {"profiles": [{"agent_id": "agent_1", "name": "Analyst", "persona": "Analyst"}]})

    need = service.resume(str(run_dir))
    assert need["type"] == "generate_simulation_config"
    _write_response(run_dir, {"config": {"rounds": 1}})

    need = service.resume(str(run_dir))
    assert need["type"] == "simulate_agent_action"
    request = AgentQueue(run_dir).load_request(need["request_id"])
    assert len(request.structured_input["actions"]) == 1
    _write_response(
        run_dir,
        {"actions": [{"agent_id": "agent_1", "action_id": "round_1_action_1", "action_type": "CREATE_POST", "content": "Chip export controls may shift supply chains."}]},
    )

    need = service.resume(str(run_dir))
    assert need["type"] == "generate_report"
    _write_response(
        run_dir,
        {
            "report_markdown": "# Report\n\nChip capacity may bifurcate.",
            "verdict": {"status": "ok", "confidence": 0.7},
            "timeline": [{"valid_at": "2024-01-01", "fact": "export controls"}],
        },
    )

    result = service.resume(str(run_dir))
    assert result["status"] == "completed"
    for artifact in ["report.md", "verdict.json", "timeline.json", "graph_snapshot.json"]:
        assert (run_dir / "artifacts" / artifact).exists()


def test_graph_build_provider_override_survives_agent_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "zep")
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "graph_store.json"))
    monkeypatch.delenv("ZEP_API_KEY", raising=False)

    seed = tmp_path / "seed.md"
    seed.write_text("A affects B.", encoding="utf-8")
    run_dir = tmp_path / "override-run"
    service = PredictionRunService()
    service.create_run(str(seed), "test graph override", str(run_dir))

    need = service.build_graph(str(run_dir), provider="graphiti")
    assert need["status"] == "need_agent_response"
    _write_response(
        run_dir,
        {
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
                    "confidence": 0.9,
                    "metadata": {},
                }
            ]
        },
    )

    next_step = service.resume(str(run_dir))
    assert next_step["status"] == "need_agent_response"
    assert next_step["type"] == "generate_oasis_profiles"
    assert (run_dir / "artifacts" / "graph_snapshot.json").exists()


def test_explicit_stage_commands_reuse_existing_waiting_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "graph_store.json"))

    service = PredictionRunService()

    for command_name, command in [
        ("graph", lambda run: service.build_graph(str(run), provider="graphiti")),
        ("simulation", lambda run: service.start_simulation(str(run))),
        ("report", lambda run: service.generate_report(str(run))),
    ]:
        seed = tmp_path / f"{command_name}.md"
        seed.write_text("A affects B.", encoding="utf-8")
        run_dir = tmp_path / f"{command_name}-run"
        service.create_run(str(seed), f"test {command_name}", str(run_dir))

        first = command(run_dir)
        second = command(run_dir)
        requests = AgentQueue(run_dir).list_requests()

        assert first["status"] == "need_agent_response"
        assert second["status"] == "need_agent_response"
        assert second["request_id"] == first["request_id"]
        assert len(requests) == 1


def test_resume_creates_repair_request_for_invalid_stage_response(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "graph_store.json"))

    seed = tmp_path / "seed.md"
    seed.write_text("A affects B.", encoding="utf-8")
    run_dir = tmp_path / "repair-run"
    service = PredictionRunService()
    service.create_run(str(seed), "test repair", str(run_dir))

    need = service.run(str(run_dir))
    _write_response(run_dir, {"ontology": {"entity_types": [], "edge_types": []}})

    need = service.resume(str(run_dir))
    assert need["type"] == "extract_triples"
    bad_response_path = run_dir / "responses" / f"{need['request_id']}.json"
    bad_response_path.write_text(
        json.dumps(
            {
                "request_id": need["request_id"],
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
                            "confidence": 2.0,
                            "metadata": {},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    repair = service.resume(str(run_dir))
    assert repair["status"] == "need_agent_response"
    assert repair["type"] == "repair_invalid_json"

    repair_request = AgentQueue(run_dir).load_request(repair["request_id"])
    assert repair_request.structured_input["validation_errors"]
    _write_response(
        run_dir,
        {
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
                    "confidence": 0.9,
                    "metadata": {},
                }
            ]
        },
    )

    next_step = service.resume(str(run_dir))
    assert next_step["status"] == "need_agent_response"
    assert next_step["type"] == "generate_oasis_profiles"


def test_response_submit_repair_request_is_attached_to_waiting_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "graph_store.json"))

    seed = tmp_path / "seed.md"
    seed.write_text("A affects B.", encoding="utf-8")
    run_dir = tmp_path / "submit-repair-run"
    service = PredictionRunService()
    service.create_run(str(seed), "test submit repair", str(run_dir))

    need = service.run(str(run_dir))
    _write_response(run_dir, {"ontology": {"entity_types": [], "edge_types": []}})

    need = service.resume(str(run_dir))
    assert need["type"] == "extract_triples"
    bad_response_path = run_dir / "responses" / f"{need['request_id']}.json"
    bad_response_path.write_text(
        json.dumps(
            {
                "request_id": need["request_id"],
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
                            "confidence": 2.0,
                            "metadata": {},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    submitted = service.submit_response(str(run_dir), str(bad_response_path))
    repair_id = submitted["repair_request"]["request_id"]
    resumed = service.resume(str(run_dir))

    assert submitted["ok"] is False
    assert resumed["request_id"] == repair_id
    assert len(AgentQueue(run_dir).list_requests()) == 3


def test_followup_question_uses_agent_queue_and_graph_context(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "graph_store.json"))
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ZEP_API_KEY", raising=False)

    seed = tmp_path / "seed.md"
    seed.write_text("美国商务部限制先进AI芯片出口。", encoding="utf-8")
    run_dir = tmp_path / "followup-run"
    service = PredictionRunService()
    created = service.create_run(str(seed), "预测未来10年全球芯片能力格局变化", str(run_dir))

    create_graph_provider("graphiti").add_triples(
        created["run_id"],
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
                "evidence": "美国商务部限制先进AI芯片出口。",
                "confidence": 0.82,
                "metadata": {},
            }
        ],
    )

    need = service.ask_followup_question(str(run_dir), "先进AI芯片出口限制有什么影响?", limit=5)
    assert need["status"] == "need_agent_response"
    assert need["type"] == "answer_followup_question"
    request = AgentQueue(run_dir).load_request(need["request_id"])
    assert request.structured_input["question"] == "先进AI芯片出口限制有什么影响?"
    assert request.structured_input["graph_results"]

    response_path = run_dir / "responses" / f"{need['request_id']}.json"
    response_path.write_text(
        json.dumps(
            {
                "request_id": need["request_id"],
                "status": "ok",
                "output": {
                    "answer_markdown": "出口限制可能推动供应链分化。",
                    "used_graph_results": request.structured_input["graph_results"],
                    "confidence": 0.8,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    answer = service.get_followup_answer(str(run_dir), need["request_id"])
    assert answer["status"] == "ok"
    assert (run_dir / "artifacts" / "followups" / f"{need['request_id']}.md").exists()
    assert (run_dir / "artifacts" / "followups" / f"{need['request_id']}.json").exists()


def test_create_run_persists_hard_round_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    seed = tmp_path / "seed.md"
    seed.write_text("A affects B.", encoding="utf-8")
    run_dir = tmp_path / "settings-run"

    created = PredictionRunService().create_run(
        str(seed),
        "predict hard settings",
        str(run_dir),
        mode="staged",
        rounds=12,
        round_unit="year",
        pause_each_round=True,
        agent_count=3,
        simulation_name="hard-settings",
    )

    settings = created["state"]["simulation_settings"]
    assert settings["rounds"] == 12
    assert settings["max_rounds"] == 12
    assert settings["simulation_rounds"] == 12
    assert settings["round_unit"] == "year"
    assert settings["minutes_per_round"] == 525600
    assert settings["pause_each_round"] is True
    assert settings["agent_count"] == 3
    assert created["state"]["workflow_mode"] == "staged"
    assert created["state"]["stages"]["seed_input"]["status"] == "awaiting_user_confirmation"


def test_rounds_below_minimum_fails_without_debug_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("MIROFISH_ALLOW_DEBUG_ROUNDS", raising=False)
    seed = tmp_path / "seed.md"
    seed.write_text("A affects B.", encoding="utf-8")

    try:
        PredictionRunService().create_run(str(seed), "too short", str(tmp_path / "bad"), rounds=3)
    except ValueError as exc:
        assert "rounds must be at least 10" in str(exc)
    else:
        raise AssertionError("rounds below 10 should fail")

    monkeypatch.setenv("MIROFISH_ALLOW_DEBUG_ROUNDS", "true")
    created = PredictionRunService().create_run(str(seed), "debug short", str(tmp_path / "debug"), rounds=3)
    assert created["state"]["simulation_settings"]["rounds"] == 3


def test_staged_mode_pauses_and_approve_advances(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "graph_store.json"))

    seed = tmp_path / "seed.md"
    seed.write_text("A affects B.", encoding="utf-8")
    run_dir = tmp_path / "staged-run"
    service = PredictionRunService()
    created = service.create_run(str(seed), "predict staged", str(run_dir), mode="staged", rounds=10)
    assert created["state"]["current_stage"] == "seed_input"
    assert created["state"]["stages"]["seed_input"]["status"] == "awaiting_user_confirmation"

    approved = service.approve_stage(str(run_dir))
    assert approved["next_stage"] == "prediction_requirement"
    status = service.status(str(run_dir))["state"]
    assert status["current_stage"] == "prediction_requirement"
    assert status["stages"]["prediction_requirement"]["status"] == "pending"

    paused = service.resume(str(run_dir))
    assert paused["status"] == "awaiting_user_confirmation"
    assert paused["stage"] == "prediction_requirement"


def test_update_settings_stales_downstream_and_config_uses_hard_rounds(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MODE", "agent")
    monkeypatch.setenv("MIROFISH_LLM_PROVIDER", "agent_queue")
    monkeypatch.setenv("MIROFISH_GRAPH_PROVIDER", "graphiti")
    monkeypatch.setenv("MIROFISH_GRAPHITI_STORE", "file")
    monkeypatch.setenv("MIROFISH_GRAPHITI_COMPAT_PATH", str(tmp_path / "graph_store.json"))

    seed = tmp_path / "seed.md"
    seed.write_text("A affects B.", encoding="utf-8")
    run_dir = tmp_path / "staged-config-run"
    service = PredictionRunService()
    service.create_run(str(seed), "predict staged config", str(run_dir), mode="staged", rounds=10)
    service.approve_stage(str(run_dir))
    service.resume(str(run_dir))
    service.approve_stage(str(run_dir))
    service.resume(str(run_dir))
    updated = service.update_simulation_settings(str(run_dir), rounds=12, round_unit="month")
    assert updated["simulation_settings"]["rounds"] == 12
    assert updated["state"]["stages"]["profile_and_config"]["stale"] is True

    service.approve_stage(str(run_dir))
    need = service.resume(str(run_dir))
    assert need["type"] == "generate_ontology"
    _write_response(run_dir, {"ontology": {"entity_types": [], "edge_types": []}})
    need = service.resume(str(run_dir))
    assert need["type"] == "extract_triples"
    _write_response(
        run_dir,
        {
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
                    "confidence": 0.9,
                    "metadata": {},
                }
            ]
        },
    )
    paused = service.resume(str(run_dir))
    assert paused["status"] == "awaiting_user_confirmation"
    assert paused["stage"] == "graph_build"
    service.approve_stage(str(run_dir))

    need = service.resume(str(run_dir))
    assert need["type"] == "generate_oasis_profiles"
    _write_response(run_dir, {"profiles": [{"agent_id": "agent_1", "name": "Agent"}]})
    need = service.resume(str(run_dir))
    assert need["type"] == "generate_simulation_config"
    _write_response(run_dir, {"config": {"rounds": 1, "simulation_rounds": 1}})
    paused = service.resume(str(run_dir))
    assert paused["status"] == "awaiting_user_confirmation"
    config = json.loads((run_dir / "artifacts" / "simulation_config.json").read_text(encoding="utf-8"))
    assert config["rounds"] == 12
    assert config["simulation_rounds"] == 12
    assert config["round_unit"] == "month"


def test_cli_parser_exposes_rounds_and_stage_commands():
    parser = build_parser()
    args = parser.parse_args(
        [
            "create-run",
            "--seed",
            "seed.md",
            "--requirement",
            "predict",
            "--output",
            "runs/demo",
            "--mode",
            "staged",
            "--rounds",
            "10",
            "--round-unit",
            "year",
        ]
    )
    assert args.command == "create-run"
    assert args.rounds == 10
    assert args.mode == "staged"

    stage_args = parser.parse_args(["stage", "update-settings", "--run", "runs/demo", "--rounds", "12"])
    assert stage_args.stage_command == "update-settings"
    assert stage_args.rounds == 12
