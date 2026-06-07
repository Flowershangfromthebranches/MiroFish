"""Shared run lifecycle service used by CLI and MCP."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..adapters.graph.factory import create_graph_provider
from ..adapters.llm.agent_runtime import AgentRuntime
from ..adapters.llm.factory import create_llm_provider
from .contracts import FOLLOWUP_OUTPUT_SCHEMA, STAGE_CONTRACTS
from .queue import AgentQueue
from .schemas import AgentResponse, StageStatus
from .state import RUN_STAGES, STAGED_RUN_STAGES, RunStore


ROUND_UNIT_MINUTES = {
    "year": 525_600,
    "month": 43_200,
    "day": 1_440,
    "step": 60,
}

STAGED_INTERNAL_START = {
    "graph_build": "ontology",
    "profile_and_config": "profiles",
    "simulation_run": "simulation",
    "report_generation": "report",
}

STAGED_INTERNAL_NEXT = {
    ("graph_build", "ontology"): "graph",
    ("profile_and_config", "profiles"): "config",
}

STAGED_DOWNSTREAM = {
    "seed_input": ["prediction_requirement", "simulation_settings", "graph_build", "profile_and_config", "simulation_run", "report_generation"],
    "prediction_requirement": ["simulation_settings", "graph_build", "profile_and_config", "simulation_run", "report_generation"],
    "simulation_settings": ["profile_and_config", "simulation_run", "report_generation"],
    "graph_build": ["profile_and_config", "simulation_run", "report_generation"],
    "profile_and_config": ["simulation_run", "report_generation"],
    "simulation_run": ["report_generation"],
    "report_generation": [],
    "followup_question": [],
}


class PredictionRunService:
    def __init__(
        self,
        *,
        llm_provider: Optional[str] = None,
        graph_provider: Optional[str] = None,
    ):
        self.llm_provider_name = llm_provider
        self.graph_provider_name = graph_provider

    def create_run(
        self,
        seed: str,
        requirement: str,
        output: str,
        *,
        mode: str = "auto",
        rounds: int = 10,
        round_unit: str = "year",
        minutes_per_round: Optional[int] = None,
        pause_each_round: bool = False,
        agent_count: Optional[int] = None,
        simulation_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if mode not in {"auto", "staged"}:
            raise ValueError("mode must be auto or staged")
        run_dir = Path(output).resolve()
        run_id = run_dir.name or f"run-{uuid.uuid4().hex[:8]}"
        store = RunStore(run_dir)
        store.ensure_layout()
        simulation_settings = self._canonical_simulation_settings(
            rounds=rounds,
            round_unit=round_unit,
            minutes_per_round=minutes_per_round,
            pause_each_round=pause_each_round,
            agent_count=agent_count,
            simulation_name=simulation_name,
            output_directory=str(run_dir),
        )

        seed_path = Path(seed).resolve()
        if not seed_path.exists():
            raise FileNotFoundError(f"seed file not found: {seed}")
        target_seed = run_dir / seed_path.name
        if seed_path != target_seed:
            shutil.copyfile(seed_path, target_seed)

        state = store.init_state(
            run_id=run_id,
            requirement=requirement,
            seed_files=[target_seed.name],
            mode=os.environ.get("MIROFISH_MODE", "agent"),
            workflow_mode=mode,
            simulation_settings=simulation_settings,
            seed_path=str(target_seed),
            metadata={
                "llm_provider": self.llm_provider_name or os.environ.get("MIROFISH_LLM_PROVIDER", "agent_queue"),
                "graph_provider": self.graph_provider_name or os.environ.get("MIROFISH_GRAPH_PROVIDER", "graphiti"),
            },
        )
        if mode == "staged":
            self._complete_static_staged_stage(store, state, "seed_input")
        return {"status": "created", "run_id": run_id, "run_dir": str(run_dir), "state": state.model_dump()}

    def run(self, run_dir: str) -> Dict[str, Any]:
        return self.resume(run_dir)

    def resume(self, run_dir: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        if state.workflow_mode == "staged":
            return self._resume_staged(store, state)
        return self._resume_auto(store, state)

    def _resume_auto(self, store: RunStore, state) -> Dict[str, Any]:
        run_dir = str(store.run_dir)
        stage = state.current_stage
        checkpoint = state.stages[stage]

        if checkpoint.status == StageStatus.COMPLETED:
            next_stage = self._next_stage(stage)
            if not next_stage:
                return {"status": "completed", "run_id": state.run_id, "artifacts": self.list_artifacts(run_dir)["artifacts"]}
            stage = next_stage
            checkpoint = state.stages[stage]
            state.current_stage = stage
            store.save(state)

        if checkpoint.status == StageStatus.WAITING_AGENT:
            request_id = checkpoint.request_ids[-1]
            response_path = store.responses_dir / f"{request_id}.json"
            if not response_path.exists():
                request_path = store.requests_dir / f"{request_id}.json"
                request = AgentQueue(run_dir).load_request(request_id)
                return {
                    "status": "need_agent_response",
                    "request_id": request_id,
                    "request_file": str(request_path),
                    "expected_response_file": str(response_path),
                    "stage": stage,
                    "type": request.type,
                }
            queue = AgentQueue(run_dir)
            validation = queue.submit_response(response_path)
            if not validation.ok:
                if validation.repair_request:
                    store.add_stage_request(state, stage, validation.repair_request.request_id)
                    return validation.repair_request.model_dump()
                store.set_stage(state, stage, StageStatus.FAILED, "; ".join(validation.errors))
                return {"status": "failed", "stage": stage, "errors": validation.errors}
            response = queue.load_response(request_id)
            if response.status == "error":
                store.set_stage(state, stage, StageStatus.FAILED, response.error or "agent returned error")
                return {"status": "failed", "stage": stage, "error": response.error or "agent returned error"}
            self._process_stage_output(store, state, stage, response.output)
            store.set_stage(state, stage, StageStatus.COMPLETED)
            next_stage = self._next_stage(stage)
            if not next_stage:
                return {"status": "completed", "run_id": state.run_id, "artifacts": self.list_artifacts(run_dir)["artifacts"]}
            state.current_stage = next_stage
            store.save(state)
            return self._start_stage(store, state, next_stage)

        if checkpoint.status in {StageStatus.PENDING, StageStatus.RUNNING}:
            return self._start_stage(store, state, stage)

        if checkpoint.status == StageStatus.FAILED:
            return {"status": "failed", "stage": stage, "error": checkpoint.error}

        return self.status(run_dir)

    def _resume_staged(self, store: RunStore, state) -> Dict[str, Any]:
        stage = state.current_stage
        checkpoint = state.stages[stage]

        if checkpoint.status == StageStatus.AWAITING_USER_CONFIRMATION:
            return self._awaiting_confirmation_payload(store, state, stage)

        if checkpoint.status == StageStatus.COMPLETED:
            next_stage = self._next_staged_stage(stage)
            if not next_stage:
                return {"status": "completed", "run_id": state.run_id, "artifacts": self.list_artifacts(str(store.run_dir))["artifacts"]}
            state.current_stage = next_stage
            store.save(state)
            return self._resume_staged(store, state)

        if checkpoint.status == StageStatus.WAITING_AGENT:
            request_id = checkpoint.request_ids[-1]
            response_path = store.responses_dir / f"{request_id}.json"
            if not response_path.exists():
                request_path = store.requests_dir / f"{request_id}.json"
                request = AgentQueue(store.run_dir).load_request(request_id)
                return {
                    "status": "need_agent_response",
                    "request_id": request_id,
                    "request_file": str(request_path),
                    "expected_response_file": str(response_path),
                    "stage": stage,
                    "type": request.type,
                }
            queue = AgentQueue(store.run_dir)
            validation = queue.submit_response(response_path)
            if not validation.ok:
                if validation.repair_request:
                    store.add_stage_request(state, stage, validation.repair_request.request_id)
                    return validation.repair_request.model_dump()
                store.set_stage(state, stage, StageStatus.FAILED, "; ".join(validation.errors))
                return {"status": "failed", "stage": stage, "errors": validation.errors}
            response = queue.load_response(request_id)
            if response.status == "error":
                store.set_stage(state, stage, StageStatus.FAILED, response.error or "agent returned error")
                return {"status": "failed", "stage": stage, "error": response.error or "agent returned error"}
            return self._handle_staged_response(store, state, stage, response)

        if checkpoint.status in {StageStatus.PENDING, StageStatus.RUNNING}:
            return self._start_staged_stage(store, state, stage)

        if checkpoint.status == StageStatus.FAILED:
            return {"status": "failed", "stage": stage, "error": checkpoint.error}

        return self.status(str(store.run_dir))

    def status(self, run_dir: str) -> Dict[str, Any]:
        return {"status": "ok", "state": RunStore(run_dir).as_status()}

    def get_current_stage(self, run_dir: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        return {"status": "ok", "stage": self._stage_detail(store, state, state.current_stage)}

    def approve_stage(self, run_dir: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        stage = state.current_stage
        checkpoint = state.stages[stage]
        if checkpoint.status != StageStatus.AWAITING_USER_CONFIRMATION:
            return {"status": "error", "error": f"stage {stage} is not awaiting user confirmation", "stage": stage}
        checkpoint.status = StageStatus.COMPLETED
        checkpoint.error = None
        checkpoint.stale = False
        checkpoint.stale_reason = None
        next_stage = self._next_staged_stage(stage) if state.workflow_mode == "staged" else self._next_stage(stage)
        if next_stage:
            state.current_stage = next_stage
        store.save(state)
        return {"status": "ok", "approved_stage": stage, "next_stage": next_stage, "state": state.model_dump()}

    def reject_stage(self, run_dir: str, reason: str = "") -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        stage = state.current_stage
        checkpoint = state.stages[stage]
        checkpoint.status = StageStatus.FAILED
        checkpoint.error = reason or "stage rejected by user"
        checkpoint.metadata["rejection_reason"] = checkpoint.error
        store.save(state)
        return {"status": "failed", "stage": stage, "reason": checkpoint.error}

    def update_simulation_settings(
        self,
        run_dir: str,
        *,
        rounds: Optional[int] = None,
        round_unit: Optional[str] = None,
        minutes_per_round: Optional[int] = None,
        pause_each_round: Optional[bool] = None,
        agent_count: Optional[int] = None,
        simulation_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        current = state.simulation_settings or {}
        settings = self._canonical_simulation_settings(
            rounds=rounds if rounds is not None else int(current.get("rounds", 10)),
            round_unit=round_unit or str(current.get("round_unit", "year")),
            minutes_per_round=minutes_per_round if minutes_per_round is not None else current.get("minutes_per_round"),
            pause_each_round=pause_each_round if pause_each_round is not None else bool(current.get("pause_each_round", False)),
            agent_count=agent_count if agent_count is not None else current.get("agent_count"),
            simulation_name=simulation_name if simulation_name is not None else current.get("simulation_name"),
            output_directory=current.get("output_directory") or str(store.run_dir),
        )
        state.simulation_settings = settings
        self._write_json(store.artifacts_dir / "simulation_settings.json", settings)
        self._mark_downstream_pending(state, "simulation_settings", "simulation settings changed")
        if "simulation_settings" in state.stages:
            checkpoint = state.stages["simulation_settings"]
            checkpoint.status = StageStatus.AWAITING_USER_CONFIRMATION
            checkpoint.stale = False
            checkpoint.stale_reason = None
            state.current_stage = "simulation_settings"
        store.save(state)
        return {"status": "ok", "simulation_settings": settings, "state": state.model_dump()}

    def rerun_stage(self, run_dir: str, stage: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        stage = self._normalize_stage_name(state, stage)
        if stage not in state.stages:
            return {"status": "error", "error": f"unknown stage: {stage}"}
        checkpoint = state.stages[stage]
        checkpoint.status = StageStatus.PENDING
        checkpoint.request_ids = []
        checkpoint.error = None
        checkpoint.stale = False
        checkpoint.stale_reason = None
        checkpoint.metadata = {}
        self._mark_downstream_pending(state, stage, f"upstream stage {stage} rerun")
        state.current_stage = stage
        store.save(state)
        return {"status": "ok", "stage": stage, "state": state.model_dump()}

    def list_requests(self, run_dir: str) -> Dict[str, Any]:
        return {"status": "ok", "requests": AgentQueue(run_dir).list_requests()}

    def get_request(self, run_dir: str, request_id: str) -> Dict[str, Any]:
        request = AgentQueue(run_dir).load_request(request_id)
        return {"status": "ok", "request": request.model_dump()}

    def validate_response(self, run_dir: str, response: str) -> Dict[str, Any]:
        result = AgentQueue(run_dir).validate_response_file(response)
        return result.model_dump()

    def submit_response(self, run_dir: str, response: str) -> Dict[str, Any]:
        queue = AgentQueue(run_dir)
        result = queue.submit_response(response)
        if result.repair_request:
            self._attach_repair_request_to_waiting_stage(run_dir, queue, result.repair_request.request_id)
        return result.model_dump()

    def build_graph(self, run_dir: str, provider: Optional[str] = None, mode: str = "agent-triples") -> Dict[str, Any]:
        if mode != "agent-triples":
            raise ValueError("only agent-triples graph build mode is supported in agent engine")
        store = RunStore(run_dir)
        state = store.load()
        if provider:
            state.metadata["graph_provider"] = provider
        if state.workflow_mode == "staged":
            state.current_stage = "graph_build"
            if state.stages["graph_build"].status == StageStatus.COMPLETED:
                state.stages["graph_build"].status = StageStatus.PENDING
            store.save(state)
            waiting = self._existing_agent_wait(store, state, "graph_build")
            if waiting:
                return waiting
            return self._start_staged_stage(store, state, "graph_build")
        state.current_stage = "graph"
        if state.stages["graph"].status == StageStatus.COMPLETED:
            state.stages["graph"].status = StageStatus.PENDING
        store.save(state)
        waiting = self._existing_agent_wait(store, state, "graph")
        if waiting:
            return waiting
        return self._start_stage(store, state, "graph", graph_provider_override=provider)

    def search_graph(self, run_dir: str, query: str, limit: int = 20) -> Dict[str, Any]:
        state = RunStore(run_dir).load()
        provider = create_graph_provider(self._graph_provider_name(state))
        return {"status": "ok", "results": provider.search(state.run_id, query, limit)}

    def export_graph(self, run_dir: str, output: Optional[str] = None) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        path = output or str(store.artifacts_dir / "graph_snapshot.json")
        provider = create_graph_provider(self._graph_provider_name(state))
        result = provider.export_snapshot(state.run_id, path)
        return {"status": "ok", "result": result}

    def start_simulation(self, run_dir: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        if state.workflow_mode == "staged":
            state.current_stage = "simulation_run"
            store.save(state)
            waiting = self._existing_agent_wait(store, state, "simulation_run")
            if waiting:
                return waiting
            return self._start_staged_stage(store, state, "simulation_run")
        state.current_stage = "simulation"
        store.save(state)
        waiting = self._existing_agent_wait(store, state, "simulation")
        if waiting:
            return waiting
        return self._start_stage(store, state, "simulation")

    def simulation_status(self, run_dir: str) -> Dict[str, Any]:
        state = RunStore(run_dir).load()
        if state.workflow_mode == "staged":
            return {"status": "ok", "simulation": state.stages["simulation_run"].model_dump(), "progress": state.simulation_progress}
        return {"status": "ok", "simulation": state.stages["simulation"].model_dump()}

    def generate_report(self, run_dir: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        if state.workflow_mode == "staged":
            state.current_stage = "report_generation"
            store.save(state)
            waiting = self._existing_agent_wait(store, state, "report_generation")
            if waiting:
                return waiting
            return self._start_staged_stage(store, state, "report_generation")
        state.current_stage = "report"
        store.save(state)
        waiting = self._existing_agent_wait(store, state, "report")
        if waiting:
            return waiting
        return self._start_stage(store, state, "report")

    def get_report(self, run_dir: str) -> Dict[str, Any]:
        path = RunStore(run_dir).artifacts_dir / "report.md"
        if not path.exists():
            return {"status": "missing", "report": None, "path": str(path)}
        return {"status": "ok", "path": str(path), "report": path.read_text(encoding="utf-8")}

    def ask_followup_question(self, run_dir: str, question: str, limit: int = 20) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        provider = create_graph_provider(self._graph_provider_name(state))
        graph_results = provider.search(state.run_id, question, limit=limit)
        runtime = AgentRuntime(
            provider=create_llm_provider(self.llm_provider_name, run_dir=store.run_dir),
            run_dir=str(store.run_dir),
        )
        result = runtime.run_task(
            run_id=state.run_id,
            task_type="answer_followup_question",
            stage="followup",
            expected_schema=FOLLOWUP_OUTPUT_SCHEMA,
            input_text=store.read_seed_text(),
            input_files=state.seed_files,
            structured_input={
                "question": question,
                "requirement": state.requirement,
                "graph_results": graph_results,
                "artifacts": self._artifact_context(store),
            },
            system_prompt="Answer a follow-up question using only run artifacts and GraphProvider retrieval context.",
            user_prompt=question,
            validation_rules={"strict": True},
            retry_policy={"max_repair_attempts": 1},
            context_refs=self._context_refs(store),
            output_contract={"schema": FOLLOWUP_OUTPUT_SCHEMA},
        )
        if result.status == "need_agent_response":
            return result.to_dict() | {"stage": "followup", "type": "answer_followup_question"}
        if result.status != "ok":
            return {"status": "failed", "stage": "followup", "error": result.error}
        return self._persist_followup_answer(store, state.run_id, f"mock_{uuid.uuid4().hex[:8]}", question, result.output or {})

    def get_followup_answer(self, run_dir: str, request_id: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        queue = AgentQueue(run_dir)
        request = queue.load_request(request_id)
        if request.type not in {"answer_followup_question", "repair_invalid_json"} or request.stage != "followup":
            return {"status": "error", "error": f"request {request_id} is not a follow-up answer request"}
        response_path = store.responses_dir / f"{request_id}.json"
        if not response_path.exists():
            return {"status": "missing", "request_id": request_id, "expected_response_file": str(response_path)}
        validation = queue.submit_response(response_path)
        if not validation.ok:
            if validation.repair_request:
                return validation.repair_request.model_dump()
            return {"status": "failed", "request_id": request_id, "errors": validation.errors}
        response = queue.load_response(request_id)
        if response.status == "error":
            return {"status": "failed", "request_id": request_id, "error": response.error or "agent returned error"}
        return self._persist_followup_answer(
            store,
            state.run_id,
            request_id,
            self._followup_question_from_request(request),
            response.output,
        )

    def list_artifacts(self, run_dir: str) -> Dict[str, Any]:
        artifacts_dir = RunStore(run_dir).artifacts_dir
        artifacts = []
        if artifacts_dir.exists():
            for path in sorted(artifacts_dir.rglob("*")):
                if path.is_file():
                    artifacts.append(
                        {
                            "name": str(path.relative_to(artifacts_dir)),
                            "path": str(path),
                            "bytes": path.stat().st_size,
                        }
                    )
        return {"status": "ok", "artifacts": artifacts}

    def doctor(self, runs_dir: Optional[str] = None) -> Dict[str, Any]:
        checks = []
        mode = os.environ.get("MIROFISH_MODE", "agent")
        llm_provider = os.environ.get("MIROFISH_LLM_PROVIDER", "agent_queue" if mode == "agent" else "openai_compatible")
        graph_provider = os.environ.get("MIROFISH_GRAPH_PROVIDER", "graphiti" if mode == "agent" else "zep")
        graphiti_store = os.environ.get("MIROFISH_GRAPHITI_STORE", "auto").lower()
        graph_search_mode = os.environ.get("MIROFISH_GRAPH_SEARCH_MODE", "fulltext").lower()
        embedding_provider = os.environ.get("MIROFISH_EMBEDDING_PROVIDER", "none").lower()
        agent_graphiti = mode == "agent" and graph_provider == "graphiti"
        production_graphiti = agent_graphiti and graphiti_store != "file"
        require_ollama = production_graphiti and graph_search_mode in {"semantic", "hybrid"} and embedding_provider == "ollama"

        def add_check(name: str, ok: bool, value: Any, *, required: bool = True) -> None:
            checks.append({"name": name, "ok": ok, "value": value, "required": required})

        def parse_neo4j_version(value: str) -> tuple[int, int]:
            base = value.split("-", 1)[0]
            parts = base.split(".")
            major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
            minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            return major, minor

        add_check("mode", mode in {"agent", "legacy"}, mode, required=False)
        add_check("llm_provider", llm_provider in {"agent_queue", "mock", "openai_compatible"}, llm_provider, required=False)
        add_check("graph_provider", graph_provider in {"graphiti", "zep"}, graph_provider, required=False)
        add_check("llm_key_not_required_in_agent", mode != "agent" or True, "agent mode skips LLM_API_KEY validation", required=False)
        add_check("zep_key_not_required_in_agent", mode != "agent" or True, "agent mode skips ZEP_API_KEY validation", required=False)
        docker_path = shutil.which("docker")
        add_check("docker", docker_path is not None, docker_path or "Docker optional, skipped", required=False)
        if docker_path:
            try:
                result = subprocess.run(
                    ["docker", "compose", "version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                compose_value = (result.stdout or result.stderr).strip() or "docker compose"
                add_check("docker_compose", result.returncode == 0, compose_value, required=False)
            except Exception as exc:
                add_check("docker_compose", False, str(exc), required=False)
        else:
            add_check("docker_compose", False, "Docker optional, skipped", required=False)
        add_check("mcp_package", importlib.util.find_spec("mcp") is not None, "mcp", required=False)
        add_check(
            "graphiti_package",
            importlib.util.find_spec("graphiti_core") is not None,
            "graphiti_core",
            required=production_graphiti,
        )
        neo4j_package_ok = importlib.util.find_spec("neo4j") is not None
        neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_PASSWORD", "password")
        neo4j_database = os.environ.get("NEO4J_DATABASE", "neo4j")
        add_check("neo4j_package", neo4j_package_ok, "neo4j", required=production_graphiti)
        add_check(
            "neo4j_uri",
            bool(neo4j_uri),
            neo4j_uri,
            required=False,
        )
        if production_graphiti:
            if neo4j_package_ok:
                try:
                    from neo4j import GraphDatabase

                    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
                    with driver.session(database=neo4j_database) as session:
                        record = session.run(
                            "CALL dbms.components() YIELD name, versions RETURN name, versions LIMIT 1"
                        ).single()
                    driver.close()
                    versions = record["versions"] if record else []
                    neo4j_version = versions[0] if versions else "unknown"
                    major, minor = parse_neo4j_version(neo4j_version)
                    version_supported = major > 5 or (major == 5 and minor >= 26)
                    add_check("neo4j_connectable", True, neo4j_uri, required=True)
                    add_check("neo4j_version_supported", version_supported, neo4j_version, required=True)
                except Exception as exc:
                    add_check("neo4j_connectable", False, f"{neo4j_uri}: {exc}", required=True)
                    add_check("neo4j_version_supported", False, "not checked", required=True)
            else:
                add_check("neo4j_connectable", False, "neo4j package is not installed", required=True)
                add_check("neo4j_version_supported", False, "not checked", required=True)
        else:
            add_check(
                "neo4j_connectable",
                False,
                "not required unless agent graphiti mode uses Neo4j store",
                required=False,
            )
            add_check(
                "neo4j_version_supported",
                False,
                "not checked",
                required=False,
            )

        add_check(
            "openai_key_not_required_by_graphiti_provider",
            graph_provider != "graphiti" or True,
            "GraphitiGraphProvider add_triples/search/export_snapshot do not require OPENAI_API_KEY",
            required=False,
        )
        ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        ollama_model = os.environ.get("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
        add_check("graph_search_mode", graph_search_mode in {"fulltext", "semantic", "hybrid"}, graph_search_mode, required=False)
        add_check("embedding_provider", embedding_provider in {"none", "ollama", "openai_compatible", "local"}, embedding_provider, required=False)
        add_check(
            "ollama_base_url",
            bool(ollama_base_url),
            ollama_base_url,
            required=False,
        )
        if require_ollama:
            try:
                with urllib.request.urlopen(f"{ollama_base_url}/api/tags", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                model_names = [item.get("name", "") for item in payload.get("models", [])]
                model_found = any(name == ollama_model or name.startswith(f"{ollama_model}:") for name in model_names)
                add_check("ollama_connectable", True, ollama_base_url, required=True)
                add_check("ollama_embedding_model", model_found, ollama_model, required=True)
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                add_check("ollama_connectable", False, f"{ollama_base_url}: {exc}", required=True)
                add_check("ollama_embedding_model", False, ollama_model, required=True)
        else:
            ollama_reason = (
                "semantic graph search disabled; fulltext search does not require Ollama"
                if graph_search_mode == "fulltext" or embedding_provider == "none"
                else "not required unless MIROFISH_GRAPH_SEARCH_MODE=semantic/hybrid and MIROFISH_EMBEDDING_PROVIDER=ollama"
            )
            add_check(
                "ollama_connectable",
                False,
                ollama_reason,
                required=False,
            )
            add_check("ollama_embedding_model", False, ollama_model, required=False)

        writable_dir = Path(runs_dir or os.environ.get("MIROFISH_RUNS_DIR", "./runs"))
        try:
            writable_dir.mkdir(parents=True, exist_ok=True)
            probe = writable_dir / ".mirofish_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            writable = True
        except Exception as exc:
            writable = False
            add_check("runs_dir_error", False, str(exc))
        add_check("runs_dir_writable", writable, str(writable_dir), required=False)

        hard_failures = [check for check in checks if check["required"] and not check["ok"]]
        warnings = [check for check in checks if not check["required"] and not check["ok"]]
        return {
            "status": "ok" if not hard_failures else "failed",
            "checks": checks,
            "warnings": warnings,
            "hard_failures": hard_failures,
        }

    def _start_staged_stage(self, store: RunStore, state, stage: str) -> Dict[str, Any]:
        if stage in {"seed_input", "prediction_requirement", "simulation_settings"}:
            self._complete_static_staged_stage(store, state, stage)
            return self._awaiting_confirmation_payload(store, state, stage)
        if stage == "graph_build":
            internal_stage = state.stages[stage].metadata.get("internal_stage") or STAGED_INTERNAL_START[stage]
            return self._start_staged_contract_stage(store, state, stage, internal_stage)
        if stage == "profile_and_config":
            internal_stage = state.stages[stage].metadata.get("internal_stage") or STAGED_INTERNAL_START[stage]
            return self._start_staged_contract_stage(store, state, stage, internal_stage)
        if stage == "simulation_run":
            return self._start_staged_simulation_round(store, state)
        if stage == "report_generation":
            return self._start_staged_contract_stage(store, state, stage, "report")
        if stage == "followup_question":
            return {"status": "ok", "stage": stage, "message": "Use followup ask or mirofish_ask_followup_question."}
        return {"status": "error", "error": f"unsupported staged stage: {stage}"}

    def _start_staged_contract_stage(self, store: RunStore, state, stage: str, internal_stage: str) -> Dict[str, Any]:
        contract = STAGE_CONTRACTS[internal_stage]
        checkpoint = state.stages[stage]
        checkpoint.metadata["internal_stage"] = internal_stage
        checkpoint.status = StageStatus.RUNNING
        state.current_stage = stage
        store.save(state)
        runtime = AgentRuntime(
            provider=create_llm_provider(self.llm_provider_name, run_dir=store.run_dir),
            run_dir=str(store.run_dir),
        )
        result = runtime.run_task(
            run_id=state.run_id,
            task_type=contract["task_type"],
            stage=stage,
            expected_schema=contract["schema"],
            input_text=store.read_seed_text(),
            input_files=state.seed_files,
            structured_input=self._stage_structured_input(store, state, internal_stage) | {
                "workflow_stage": stage,
                "internal_stage": internal_stage,
            },
            system_prompt=contract["system_prompt"],
            user_prompt=contract["user_prompt"],
            validation_rules={"strict": True},
            retry_policy={"max_repair_attempts": 1},
            context_refs=self._context_refs(store),
            output_contract={"schema": contract["schema"]},
        )
        if result.status == "need_agent_response":
            store.add_stage_request(state, stage, result.request_id)
            return result.to_dict() | {"stage": stage, "internal_stage": internal_stage, "type": contract["task_type"]}
        if result.status != "ok":
            store.set_stage(state, stage, StageStatus.FAILED, result.error)
            return {"status": "failed", "stage": stage, "error": result.error}
        return self._handle_staged_response(
            store,
            state,
            stage,
            AgentResponse(request_id=f"req_{uuid.uuid4().hex[:6]}", status="ok", output=result.output or {}),
        )

    def _handle_staged_response(self, store: RunStore, state, stage: str, response: AgentResponse) -> Dict[str, Any]:
        checkpoint = state.stages[stage]
        internal_stage = checkpoint.metadata.get("internal_stage")
        if not internal_stage:
            internal_stage = self._internal_stage_from_response_type(response, stage)
        if not internal_stage:
            store.set_stage(state, stage, StageStatus.FAILED, "cannot determine staged internal stage")
            return {"status": "failed", "stage": stage, "error": "cannot determine staged internal stage"}

        if stage == "simulation_run":
            self._process_staged_simulation_output(store, state, response.output)
            return self._after_staged_simulation_round(store, state)

        self._process_stage_output(store, state, internal_stage, response.output)
        next_internal = STAGED_INTERNAL_NEXT.get((stage, internal_stage))
        if next_internal:
            checkpoint.metadata["internal_stage"] = next_internal
            checkpoint.status = StageStatus.PENDING
            store.save(state)
            return self._start_staged_contract_stage(store, state, stage, next_internal)

        if stage == "graph_build":
            state.graph_summary = self._graph_summary(store)
        elif stage == "profile_and_config":
            state.profiles_summary = self._profiles_summary(store)
            state.config_summary = self._config_summary(store, state)
        elif stage == "report_generation":
            self._ensure_final_artifacts(store, state)
            state.report_artifacts = self._report_artifacts(store)
            checkpoint.status = StageStatus.COMPLETED
            checkpoint.metadata["summary"] = state.report_artifacts
            state.current_stage = "followup_question" if "followup_question" in state.stages else stage
            store.save(state)
            return {"status": "completed", "run_id": state.run_id, "artifacts": self.list_artifacts(str(store.run_dir))["artifacts"]}

        checkpoint.status = StageStatus.AWAITING_USER_CONFIRMATION
        checkpoint.error = None
        checkpoint.stale = False
        checkpoint.stale_reason = None
        checkpoint.metadata["summary"] = self._stage_summary(store, state, stage)
        state.current_stage = stage
        store.save(state)
        return self._awaiting_confirmation_payload(store, state, stage)

    def _start_staged_simulation_round(self, store: RunStore, state) -> Dict[str, Any]:
        progress = state.simulation_progress or {}
        settings = self._ensure_state_simulation_settings(state, store)
        total_rounds = int(settings["rounds"])
        completed_rounds = int(progress.get("completed_rounds", 0))
        if completed_rounds >= total_rounds:
            state.stages["simulation_run"].status = StageStatus.AWAITING_USER_CONFIRMATION
            state.simulation_progress = self._simulation_progress_payload(state, completed_rounds)
            store.save(state)
            return self._awaiting_confirmation_payload(store, state, "simulation_run")

        round_index = completed_rounds + 1
        checkpoint = state.stages["simulation_run"]
        checkpoint.metadata["internal_stage"] = "simulation"
        checkpoint.metadata["round_index"] = round_index
        checkpoint.status = StageStatus.RUNNING
        state.current_stage = "simulation_run"
        state.simulation_progress = self._simulation_progress_payload(state, completed_rounds)
        store.save(state)

        contract = STAGE_CONTRACTS["simulation"]
        runtime = AgentRuntime(
            provider=create_llm_provider(self.llm_provider_name, run_dir=store.run_dir),
            run_dir=str(store.run_dir),
        )
        result = runtime.run_task(
            run_id=state.run_id,
            task_type=contract["task_type"],
            stage="simulation_run",
            expected_schema=contract["schema"],
            input_text=store.read_seed_text(),
            input_files=state.seed_files,
            structured_input=self._simulation_round_structured_input(store, state, round_index),
            system_prompt=contract["system_prompt"],
            user_prompt=contract["user_prompt"],
            validation_rules={"strict": True},
            retry_policy={"max_repair_attempts": 1},
            context_refs=self._context_refs(store),
            output_contract={"schema": contract["schema"]},
        )
        if result.status == "need_agent_response":
            store.add_stage_request(state, "simulation_run", result.request_id)
            return result.to_dict() | {"stage": "simulation_run", "round_index": round_index, "type": contract["task_type"]}
        if result.status != "ok":
            store.set_stage(state, "simulation_run", StageStatus.FAILED, result.error)
            return {"status": "failed", "stage": "simulation_run", "error": result.error}
        self._process_staged_simulation_output(store, state, result.output or {})
        return self._after_staged_simulation_round(store, state)

    def _after_staged_simulation_round(self, store: RunStore, state) -> Dict[str, Any]:
        settings = self._ensure_state_simulation_settings(state, store)
        progress = state.simulation_progress
        completed_rounds = int(progress.get("completed_rounds", 0))
        total_rounds = int(settings["rounds"])
        checkpoint = state.stages["simulation_run"]
        if completed_rounds >= total_rounds:
            checkpoint.status = StageStatus.AWAITING_USER_CONFIRMATION
            checkpoint.metadata["summary"] = state.simulation_progress
            store.save(state)
            return self._awaiting_confirmation_payload(store, state, "simulation_run")
        if settings.get("pause_each_round"):
            checkpoint.status = StageStatus.AWAITING_USER_CONFIRMATION
            checkpoint.metadata["summary"] = state.simulation_progress
            checkpoint.metadata["awaiting_round_approval"] = completed_rounds
            store.save(state)
            return self._awaiting_confirmation_payload(store, state, "simulation_run")
        checkpoint.status = StageStatus.PENDING
        store.save(state)
        return self._start_staged_simulation_round(store, state)

    def _complete_static_staged_stage(self, store: RunStore, state, stage: str) -> None:
        artifacts = store.artifacts_dir
        artifacts.mkdir(parents=True, exist_ok=True)
        checkpoint = state.stages[stage]
        if stage == "seed_input":
            seed_text = store.read_seed_text()
            summary = {
                "seed_path": state.seed_path or (state.seed_files[0] if state.seed_files else None),
                "seed_files": state.seed_files,
                "character_count": len(seed_text),
                "line_count": len(seed_text.splitlines()),
                "valid": bool(seed_text.strip()),
                "preview": seed_text[:500],
            }
            self._write_json(artifacts / "seed_summary.json", summary)
            state.metadata["seed_summary"] = summary
            store.add_stage_artifact(state, stage, str(artifacts / "seed_summary.json"))
        elif stage == "prediction_requirement":
            summary = {
                "requirement": state.requirement,
                "character_count": len(state.requirement),
                "valid": bool(state.requirement.strip()),
            }
            self._write_json(artifacts / "requirement_summary.json", summary)
            state.metadata["requirement_summary"] = summary
            store.add_stage_artifact(state, stage, str(artifacts / "requirement_summary.json"))
        elif stage == "simulation_settings":
            state.simulation_settings = self._canonical_simulation_settings(**self._settings_kwargs(state, store))
            self._write_json(artifacts / "simulation_settings.json", state.simulation_settings)
            checkpoint.metadata["summary"] = state.simulation_settings
            store.add_stage_artifact(state, stage, str(artifacts / "simulation_settings.json"))
        checkpoint.status = StageStatus.AWAITING_USER_CONFIRMATION
        checkpoint.error = None
        checkpoint.stale = False
        checkpoint.stale_reason = None
        checkpoint.metadata["summary"] = self._stage_summary(store, state, stage)
        state.current_stage = stage
        store.save(state)

    def _start_stage(
        self,
        store: RunStore,
        state,
        stage: str,
        *,
        graph_provider_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        if stage == "artifacts":
            self._ensure_final_artifacts(store, state)
            store.set_stage(state, stage, StageStatus.COMPLETED)
            return {"status": "completed", "run_id": state.run_id, "artifacts": self.list_artifacts(str(store.run_dir))["artifacts"]}

        contract = STAGE_CONTRACTS[stage]
        store.set_stage(state, stage, StageStatus.RUNNING)
        runtime = AgentRuntime(
            provider=create_llm_provider(self.llm_provider_name, run_dir=store.run_dir),
            run_dir=str(store.run_dir),
        )
        result = runtime.run_task(
            run_id=state.run_id,
            task_type=contract["task_type"],
            stage=stage,
            expected_schema=contract["schema"],
            input_text=store.read_seed_text(),
            input_files=state.seed_files,
            structured_input=self._stage_structured_input(store, state, stage),
            system_prompt=contract["system_prompt"],
            user_prompt=contract["user_prompt"],
            validation_rules={"strict": True},
            retry_policy={"max_repair_attempts": 1},
            context_refs=self._context_refs(store),
            output_contract={"schema": contract["schema"]},
        )
        if result.status == "need_agent_response":
            store.add_stage_request(state, stage, result.request_id)
            return result.to_dict() | {"stage": stage, "type": contract["task_type"]}
        if result.status != "ok":
            store.set_stage(state, stage, StageStatus.FAILED, result.error)
            return {"status": "failed", "stage": stage, "error": result.error}
        self._process_stage_output(store, state, stage, result.output or {}, graph_provider_override=graph_provider_override)
        store.set_stage(state, stage, StageStatus.COMPLETED)
        return self.resume(str(store.run_dir))

    def _process_stage_output(
        self,
        store: RunStore,
        state,
        stage: str,
        output: Dict[str, Any],
        *,
        graph_provider_override: Optional[str] = None,
    ) -> None:
        artifacts = store.artifacts_dir
        artifacts.mkdir(parents=True, exist_ok=True)
        if stage == "ontology":
            self._write_json(artifacts / "ontology.json", output["ontology"])
            store.add_stage_artifact(state, stage, str(artifacts / "ontology.json"))
        elif stage == "graph":
            triples = output["triples"]
            self._write_json(artifacts / "triples.json", triples)
            provider = create_graph_provider(graph_provider_override or self._graph_provider_name(state))
            provider.add_triples(state.run_id, triples)
            provider.export_snapshot(state.run_id, str(artifacts / "graph_snapshot.json"))
            if hasattr(provider, "export_timeline"):
                provider.export_timeline(state.run_id, str(artifacts / "timeline.json"))
            else:
                self._write_json(artifacts / "timeline.json", [])
            store.add_stage_artifact(state, stage, str(artifacts / "graph_snapshot.json"))
            store.add_stage_artifact(state, stage, str(artifacts / "timeline.json"))
        elif stage == "profiles":
            self._write_json(artifacts / "profiles.json", output["profiles"])
            store.add_stage_artifact(state, stage, str(artifacts / "profiles.json"))
        elif stage == "config":
            config = self._config_with_settings(output["config"], state)
            self._write_json(artifacts / "simulation_config.json", config)
            store.add_stage_artifact(state, stage, str(artifacts / "simulation_config.json"))
        elif stage == "simulation":
            self._write_json(artifacts / "simulation_actions.json", output["actions"])
            store.add_stage_artifact(state, stage, str(artifacts / "simulation_actions.json"))
        elif stage == "report":
            settings = self._settings_for_output(state)
            (artifacts / "report.md").write_text(output["report_markdown"], encoding="utf-8")
            verdict = output["verdict"] | {"simulation_settings": settings, "rounds": settings.get("rounds")}
            self._write_json(artifacts / "verdict.json", verdict)
            self._write_json(artifacts / "timeline.json", self._timeline_with_rounds(output.get("timeline", []), state))
            store.add_stage_artifact(state, stage, str(artifacts / "report.md"))
            store.add_stage_artifact(state, stage, str(artifacts / "verdict.json"))
            store.add_stage_artifact(state, stage, str(artifacts / "timeline.json"))

    def _stage_structured_input(self, store: RunStore, state, stage: str) -> Dict[str, Any]:
        artifacts = store.artifacts_dir
        data: Dict[str, Any] = {"requirement": state.requirement, "simulation_settings": self._ensure_state_simulation_settings(state, store)}
        for name in ["ontology", "triples", "profiles", "simulation_config", "simulation_actions"]:
            path = artifacts / f"{name}.json"
            if path.exists():
                data[name] = json.loads(path.read_text(encoding="utf-8"))
        if stage == "simulation":
            data |= self._simulation_round_structured_input(store, state, 1)
        return data

    def _context_refs(self, store: RunStore) -> List[str]:
        if not store.artifacts_dir.exists():
            return []
        return [str(path) for path in sorted(store.artifacts_dir.glob("*.json"))]

    def _artifact_context(self, store: RunStore) -> Dict[str, Any]:
        context: Dict[str, Any] = {}
        if not store.artifacts_dir.exists():
            return context
        for path in sorted(store.artifacts_dir.glob("*.json")):
            try:
                context[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                context[path.stem] = {"path": str(path), "error": "invalid json"}
        report_path = store.artifacts_dir / "report.md"
        if report_path.exists():
            context["report_markdown"] = report_path.read_text(encoding="utf-8")[:50_000]
        return context

    def _persist_followup_answer(
        self,
        store: RunStore,
        run_id: str,
        request_id: str,
        question: str,
        output: Dict[str, Any],
    ) -> Dict[str, Any]:
        followups_dir = store.artifacts_dir / "followups"
        followups_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = followups_dir / f"{request_id}.md"
        json_path = followups_dir / f"{request_id}.json"
        markdown_path.write_text(output["answer_markdown"], encoding="utf-8")
        payload = {
            "run_id": run_id,
            "request_id": request_id,
            "question": question,
            "answer": output,
        }
        self._write_json(json_path, payload)
        return {
            "status": "ok",
            "request_id": request_id,
            "question": question,
            "answer": output,
            "artifacts": {
                "markdown": str(markdown_path),
                "json": str(json_path),
            },
        }

    def _followup_question_from_request(self, request) -> str:
        if request.type == "answer_followup_question":
            return str(request.structured_input.get("question", ""))
        original = request.structured_input.get("original_request", {})
        if isinstance(original, dict):
            structured_input = original.get("structured_input", {})
            if isinstance(structured_input, dict):
                return str(structured_input.get("question", ""))
        return ""

    def _graph_provider_name(self, state) -> Optional[str]:
        return self.graph_provider_name or state.metadata.get("graph_provider")

    def _ensure_final_artifacts(self, store: RunStore, state) -> None:
        artifacts = store.artifacts_dir
        artifacts.mkdir(parents=True, exist_ok=True)
        if not (artifacts / "report.md").exists():
            (artifacts / "report.md").write_text("# MiroFish Report\n\nReport generation has not completed.", encoding="utf-8")
        if not (artifacts / "verdict.json").exists():
            self._write_json(artifacts / "verdict.json", {"status": "incomplete"})
        if not (artifacts / "timeline.json").exists():
            self._write_json(artifacts / "timeline.json", [])
        if not (artifacts / "graph_snapshot.json").exists():
            create_graph_provider(self.graph_provider_name).export_snapshot(state.run_id, str(artifacts / "graph_snapshot.json"))

    def _next_stage(self, stage: str) -> Optional[str]:
        try:
            index = RUN_STAGES.index(stage)
        except ValueError:
            return None
        if index + 1 >= len(RUN_STAGES):
            return None
        return RUN_STAGES[index + 1]

    def _existing_agent_wait(self, store: RunStore, state, stage: str) -> Optional[Dict[str, Any]]:
        checkpoint = state.stages[stage]
        if checkpoint.status != StageStatus.WAITING_AGENT or not checkpoint.request_ids:
            return None
        request_id = checkpoint.request_ids[-1]
        request = AgentQueue(store.run_dir).load_request(request_id)
        return {
            "status": "need_agent_response",
            "request_id": request_id,
            "request_file": str(store.requests_dir / f"{request_id}.json"),
            "expected_response_file": str(store.responses_dir / f"{request_id}.json"),
            "stage": stage,
            "type": request.type,
        }

    def _attach_repair_request_to_waiting_stage(self, run_dir: str, queue: AgentQueue, repair_request_id: str) -> None:
        try:
            store = RunStore(run_dir)
            state = store.load()
            repair_request = queue.load_request(repair_request_id)
            original = repair_request.structured_input.get("original_request", {})
            original_request_id = original.get("request_id") if isinstance(original, dict) else None
            if not original_request_id:
                return
            for stage, checkpoint in state.stages.items():
                if checkpoint.status == StageStatus.WAITING_AGENT and checkpoint.request_ids[-1:] == [original_request_id]:
                    store.add_stage_request(state, stage, repair_request_id)
                    return
        except Exception:
            return

    def _canonical_simulation_settings(
        self,
        *,
        rounds: int,
        round_unit: str,
        minutes_per_round: Optional[int],
        pause_each_round: bool,
        agent_count: Optional[int] = None,
        simulation_name: Optional[str] = None,
        output_directory: Optional[str] = None,
    ) -> Dict[str, Any]:
        if round_unit not in ROUND_UNIT_MINUTES:
            raise ValueError(f"round_unit must be one of {sorted(ROUND_UNIT_MINUTES)}")
        rounds = int(rounds)
        allow_debug_rounds = os.environ.get("MIROFISH_ALLOW_DEBUG_ROUNDS", "").lower() in {"1", "true", "yes"}
        if rounds < 10 and not allow_debug_rounds:
            raise ValueError("rounds must be at least 10 unless MIROFISH_ALLOW_DEBUG_ROUNDS=true")
        canonical_minutes = int(minutes_per_round) if minutes_per_round is not None else ROUND_UNIT_MINUTES[round_unit]
        settings: Dict[str, Any] = {
            "rounds": rounds,
            "max_rounds": rounds,
            "simulation_rounds": rounds,
            "round_unit": round_unit,
            "minutes_per_round": canonical_minutes,
            "pause_each_round": bool(pause_each_round),
        }
        if agent_count is not None:
            settings["agent_count"] = int(agent_count)
        if simulation_name:
            settings["simulation_name"] = simulation_name
        if output_directory:
            settings["output_directory"] = output_directory
        return settings

    def _settings_kwargs(self, state, store: RunStore) -> Dict[str, Any]:
        current = state.simulation_settings or {}
        return {
            "rounds": int(current.get("rounds", 10)),
            "round_unit": str(current.get("round_unit", "year")),
            "minutes_per_round": current.get("minutes_per_round"),
            "pause_each_round": bool(current.get("pause_each_round", False)),
            "agent_count": current.get("agent_count"),
            "simulation_name": current.get("simulation_name"),
            "output_directory": current.get("output_directory") or str(store.run_dir),
        }

    def _ensure_state_simulation_settings(self, state, store: RunStore) -> Dict[str, Any]:
        if not state.simulation_settings:
            state.simulation_settings = self._canonical_simulation_settings(
                rounds=10,
                round_unit="year",
                minutes_per_round=None,
                pause_each_round=False,
                output_directory=str(store.run_dir),
            )
        return state.simulation_settings

    def _settings_for_output(self, state) -> Dict[str, Any]:
        settings = dict(state.simulation_settings or {})
        rounds = int(settings.get("rounds", 10))
        settings["rounds"] = rounds
        settings["max_rounds"] = rounds
        settings["simulation_rounds"] = rounds
        return settings

    def _config_with_settings(self, config: Dict[str, Any], state) -> Dict[str, Any]:
        settings = self._settings_for_output(state)
        merged = dict(config or {})
        merged["rounds"] = settings["rounds"]
        merged["max_rounds"] = settings["rounds"]
        merged["simulation_rounds"] = settings["rounds"]
        merged["round_unit"] = settings.get("round_unit")
        merged["minutes_per_round"] = settings.get("minutes_per_round")
        merged["pause_each_round"] = settings.get("pause_each_round", False)
        if "agent_count" in settings:
            merged["agent_count"] = settings["agent_count"]
        if "simulation_name" in settings:
            merged["simulation_name"] = settings["simulation_name"]
        merged["simulation_settings"] = settings
        return merged

    def _simulation_round_structured_input(self, store: RunStore, state, round_index: int) -> Dict[str, Any]:
        artifacts = store.artifacts_dir
        profiles_path = artifacts / "profiles.json"
        profiles = json.loads(profiles_path.read_text(encoding="utf-8")) if profiles_path.exists() else [{"agent_id": "agent_1"}]
        settings = self._ensure_state_simulation_settings(state, store)
        agent_limit = int(settings.get("agent_count") or min(len(profiles), 5) or 1)
        selected_profiles = profiles[:agent_limit]
        round_id = f"round_{round_index}"
        return {
            "round_id": round_id,
            "round_index": round_index,
            "simulation_settings": settings,
            "actions": [
                {
                    "agent_id": str(profile.get("agent_id") or profile.get("user_id") or index + 1),
                    "action_id": f"{round_id}_action_{index + 1}",
                    "round_id": round_id,
                }
                for index, profile in enumerate(selected_profiles)
            ],
            "simulation_progress": state.simulation_progress or {},
        }

    def _process_staged_simulation_output(self, store: RunStore, state, output: Dict[str, Any]) -> None:
        artifacts = store.artifacts_dir
        rounds_dir = artifacts / "simulation_rounds"
        rounds_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = state.stages["simulation_run"]
        round_index = int(checkpoint.metadata.get("round_index", 1))
        actions = output.get("actions", [])
        round_payload = {
            "round_index": round_index,
            "round_id": f"round_{round_index}",
            "round_unit": state.simulation_settings.get("round_unit", "year"),
            "actions": actions,
        }
        round_path = rounds_dir / f"round_{round_index:03d}.json"
        self._write_json(round_path, round_payload)
        all_actions_path = artifacts / "simulation_actions.json"
        all_actions: List[Dict[str, Any]] = []
        if all_actions_path.exists():
            all_actions = json.loads(all_actions_path.read_text(encoding="utf-8"))
        all_actions.extend(actions)
        self._write_json(all_actions_path, all_actions)
        store.add_stage_artifact(state, "simulation_run", str(round_path))
        store.add_stage_artifact(state, "simulation_run", str(all_actions_path))
        state.simulation_progress = self._simulation_progress_payload(state, round_index)

    def _simulation_progress_payload(self, state, completed_rounds: int) -> Dict[str, Any]:
        settings = self._settings_for_output(state)
        total_rounds = int(settings["rounds"])
        return {
            "completed_rounds": completed_rounds,
            "total_rounds": total_rounds,
            "current_round": min(completed_rounds + 1, total_rounds),
            "round_unit": settings.get("round_unit", "year"),
            "minutes_per_round": settings.get("minutes_per_round"),
            "pause_each_round": settings.get("pause_each_round", False),
        }

    def _timeline_with_rounds(self, timeline: List[Dict[str, Any]], state) -> List[Dict[str, Any]]:
        settings = self._settings_for_output(state)
        rounds = int(settings["rounds"])
        existing = list(timeline or [])
        normalized = []
        for index in range(1, rounds + 1):
            item = existing[index - 1] if index - 1 < len(existing) and isinstance(existing[index - 1], dict) else {}
            normalized.append(
                item
                | {
                    "round": index,
                    "round_unit": settings.get("round_unit", "year"),
                    "minutes_per_round": settings.get("minutes_per_round"),
                }
            )
        return normalized

    def _stage_summary(self, store: RunStore, state, stage: str) -> Dict[str, Any]:
        if stage == "seed_input":
            return state.metadata.get("seed_summary", {})
        if stage == "prediction_requirement":
            return state.metadata.get("requirement_summary", {})
        if stage == "simulation_settings":
            return state.simulation_settings
        if stage == "graph_build":
            return state.graph_summary
        if stage == "profile_and_config":
            return {"profiles_summary": state.profiles_summary, "config_summary": state.config_summary}
        if stage == "simulation_run":
            return state.simulation_progress
        if stage == "report_generation":
            return state.report_artifacts
        return {}

    def _stage_detail(self, store: RunStore, state, stage: str) -> Dict[str, Any]:
        checkpoint = state.stages[stage]
        return {
            "run_id": state.run_id,
            "workflow_mode": state.workflow_mode,
            "current_stage": stage,
            "checkpoint": checkpoint.model_dump(),
            "summary": self._stage_summary(store, state, stage),
            "simulation_settings": state.simulation_settings,
            "artifacts": checkpoint.artifact_paths,
        }

    def _awaiting_confirmation_payload(self, store: RunStore, state, stage: str) -> Dict[str, Any]:
        return {
            "status": "awaiting_user_confirmation",
            "run_id": state.run_id,
            "stage": stage,
            "current_stage": stage,
            "stage_detail": self._stage_detail(store, state, stage),
        }

    def _graph_summary(self, store: RunStore) -> Dict[str, Any]:
        triples_path = store.artifacts_dir / "triples.json"
        triples = json.loads(triples_path.read_text(encoding="utf-8")) if triples_path.exists() else []
        entities: Dict[str, int] = {}
        for triple in triples:
            for key in ("subject", "object"):
                value = str(triple.get(key, "")).strip()
                if value:
                    entities[value] = entities.get(value, 0) + 1
        return {
            "entity_count": len(entities),
            "triple_count": len(triples),
            "top_entities": sorted(entities, key=entities.get, reverse=True)[:10],
            "warnings": [],
        }

    def _profiles_summary(self, store: RunStore) -> Dict[str, Any]:
        path = store.artifacts_dir / "profiles.json"
        profiles = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return {
            "profile_count": len(profiles),
            "agent_ids": [str(profile.get("agent_id") or profile.get("user_id") or "") for profile in profiles],
        }

    def _config_summary(self, store: RunStore, state) -> Dict[str, Any]:
        path = store.artifacts_dir / "simulation_config.json"
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return {
            "rounds": config.get("rounds"),
            "round_unit": config.get("round_unit"),
            "minutes_per_round": config.get("minutes_per_round"),
            "agent_count": config.get("agent_count", state.simulation_settings.get("agent_count")),
        }

    def _report_artifacts(self, store: RunStore) -> Dict[str, Any]:
        names = ["report.md", "verdict.json", "timeline.json", "graph_snapshot.json"]
        return {name: str(store.artifacts_dir / name) for name in names if (store.artifacts_dir / name).exists()}

    def _mark_downstream_pending(self, state, stage: str, reason: str) -> None:
        for downstream in STAGED_DOWNSTREAM.get(stage, []):
            if downstream not in state.stages:
                continue
            checkpoint = state.stages[downstream]
            checkpoint.status = StageStatus.PENDING
            checkpoint.request_ids = []
            checkpoint.error = None
            checkpoint.stale = True
            checkpoint.stale_reason = reason
            checkpoint.metadata = {}
        state.graph_summary = state.graph_summary if stage not in {"seed_input", "prediction_requirement"} else {}
        if stage in {"seed_input", "prediction_requirement", "simulation_settings", "graph_build"}:
            state.profiles_summary = {}
            state.config_summary = {}
            state.simulation_progress = {}
            state.report_artifacts = {}

    def _normalize_stage_name(self, state, stage: str) -> str:
        if stage in state.stages:
            return stage
        aliases = {
            "graph": "graph_build",
            "profiles": "profile_and_config",
            "config": "profile_and_config",
            "simulation": "simulation_run",
            "report": "report_generation",
        }
        return aliases.get(stage, stage)

    def _next_staged_stage(self, stage: str) -> Optional[str]:
        try:
            index = STAGED_RUN_STAGES.index(stage)
        except ValueError:
            return None
        if index + 1 >= len(STAGED_RUN_STAGES):
            return None
        return STAGED_RUN_STAGES[index + 1]

    def _internal_stage_from_response_type(self, response: AgentResponse, stage: str) -> Optional[str]:
        task_map = {
            "generate_ontology": "ontology",
            "extract_triples": "graph",
            "generate_oasis_profiles": "profiles",
            "generate_simulation_config": "config",
            "simulate_agent_action": "simulation",
            "generate_report": "report",
        }
        return task_map.get(getattr(response, "type", ""), STAGED_INTERNAL_START.get(stage))

    def _write_json(self, path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
