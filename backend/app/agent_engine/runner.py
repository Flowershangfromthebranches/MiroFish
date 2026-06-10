"""Shared run lifecycle service used by CLI and MCP."""

from __future__ import annotations

import importlib.util
import html
import json
import os
import re
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
from .contracts import (
    AGENT_QUESTION_OUTPUT_SCHEMA,
    AGENT_QUESTIONNAIRE_OUTPUT_SCHEMA,
    FOLLOWUP_OUTPUT_SCHEMA,
    QUESTIONNAIRE_SUMMARY_OUTPUT_SCHEMA,
    REPORT_QUESTION_OUTPUT_SCHEMA,
    STAGE_CONTRACTS,
)
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

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _resolve_agent_id(profile: Dict[str, Any], index: int = 0) -> str:
    """Derive a stable, non-empty agent_id from a profile dict.

    Priority:
    1. profile["agent_id"]  (if truthy)
    2. profile["user_id"]   (if truthy)
    3. slug of profile["name"]  (e.g. "Google China" -> "google_china")
    4. positional fallback "agent_{index+1}"
    """
    raw = profile.get("agent_id") or profile.get("user_id")
    if raw:
        return str(raw).strip() or f"agent_{index + 1}"
    name = profile.get("name")
    if name and isinstance(name, str) and name.strip():
        slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_")
        return slug or f"agent_{index + 1}"
    return f"agent_{index + 1}"


def _resolve_all_agent_ids(profiles: list) -> List[str]:
    """Resolve agent IDs for a full list of profiles with deduplication.

    Same base logic as _resolve_agent_id, but when two profiles produce
    the same ID the later ones get a ``_2``, ``_3``, … suffix so every
    ID in the returned list is unique.
    """
    resolved: List[str] = []
    seen: Dict[str, int] = {}
    for i, profile in enumerate(profiles):
        base_id = _resolve_agent_id(profile, i)
        if base_id in seen:
            seen[base_id] += 1
            unique_id = f"{base_id}_{seen[base_id]}"
            # Edge case: the suffixed ID might itself collide
            while unique_id in seen:
                seen[base_id] += 1
                unique_id = f"{base_id}_{seen[base_id]}"
            resolved.append(unique_id)
            seen[unique_id] = 1
        else:
            resolved.append(base_id)
            seen[base_id] = 1
    return resolved


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

    # ── Agent interaction methods ──────────────────────────────────────────

    def list_agents(self, run_dir: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        profiles_path = store.artifacts_dir / "profiles.json"
        if not profiles_path.exists():
            return {"status": "ok", "agents": [], "count": 0}
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
        resolved_ids = _resolve_all_agent_ids(profiles)
        agents = []
        for idx, profile in enumerate(profiles):
            agent_id = resolved_ids[idx]
            agents.append({
                "agent_id": agent_id,
                "name": profile.get("name", ""),
                "persona": profile.get("persona", ""),
                "profile": profile,
            })
        return {"status": "ok", "agents": agents, "count": len(agents)}

    def get_agent(self, run_dir: str, agent_id: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        profiles_path = store.artifacts_dir / "profiles.json"
        if not profiles_path.exists():
            return {"status": "error", "error": "profiles.json not found"}
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
        resolved_ids = _resolve_all_agent_ids(profiles)
        for idx, profile in enumerate(profiles):
            pid = resolved_ids[idx]
            if pid == agent_id:
                return {"status": "ok", "agent": {
                    "agent_id": pid,
                    "name": profile.get("name", ""),
                    "persona": profile.get("persona", ""),
                    "profile": profile,
                }}
        return {"status": "error", "error": f"agent not found: {agent_id}"}

    def ask_agent(self, run_dir: str, agent_id: str, question: str, limit: int = 20) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        # Verify agent exists
        agent_result = self.get_agent(run_dir, agent_id)
        if agent_result["status"] == "error":
            return agent_result
        provider = create_graph_provider(self._graph_provider_name(state))
        graph_results = provider.search(state.run_id, question, limit=limit)
        runtime = AgentRuntime(
            provider=create_llm_provider(self.llm_provider_name, run_dir=store.run_dir),
            run_dir=str(store.run_dir),
        )
        result = runtime.run_task(
            run_id=state.run_id,
            task_type="answer_agent_question",
            stage="interaction",
            expected_schema=AGENT_QUESTION_OUTPUT_SCHEMA,
            input_text=store.read_seed_text(),
            input_files=state.seed_files,
            structured_input={
                "agent_id": agent_id,
                "question": question,
                "requirement": state.requirement,
                "graph_results": graph_results,
                "agent_profile": agent_result["agent"]["profile"],
                "artifacts": self._artifact_context(store),
            },
            system_prompt=(
                f"You are agent '{agent_id}'. Answer the question from your persona's perspective "
                "using only run artifacts and GraphProvider retrieval context."
            ),
            user_prompt=question,
            validation_rules={"strict": True},
            retry_policy={"max_repair_attempts": 1},
            context_refs=self._context_refs(store),
            output_contract={"schema": AGENT_QUESTION_OUTPUT_SCHEMA},
        )
        if result.status == "need_agent_response":
            return result.to_dict() | {"stage": "interaction", "type": "answer_agent_question", "agent_id": agent_id}
        if result.status != "ok":
            return {"status": "failed", "stage": "interaction", "error": result.error}
        return self._persist_agent_question_answer(store, state.run_id, agent_id, f"mock_{uuid.uuid4().hex[:8]}", question, result.output or {})

    def get_agent_answer(self, run_dir: str, request_id: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        queue = AgentQueue(run_dir)
        request = queue.load_request(request_id)
        if request.type != "answer_agent_question" or request.stage != "interaction":
            return {"status": "error", "error": f"request {request_id} is not an agent question request"}
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
        agent_id = str(request.structured_input.get("agent_id", ""))
        question = str(request.structured_input.get("question", ""))
        return self._persist_agent_question_answer(store, state.run_id, agent_id, request_id, question, response.output)

    def send_questionnaire(self, run_dir: str, questions: List[Dict[str, Any]]) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        profiles_path = store.artifacts_dir / "profiles.json"
        if not profiles_path.exists():
            return {"status": "error", "error": "profiles.json not found; cannot send questionnaire"}
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
        resolved_ids = _resolve_all_agent_ids(profiles)
        agents = [
            {"agent_id": resolved_ids[i], "profile": p}
            for i, p in enumerate(profiles)
        ]
        questionnaire_id = f"questionnaire_{uuid.uuid4().hex[:8]}"
        provider = create_graph_provider(self._graph_provider_name(state))
        runtime = AgentRuntime(
            provider=create_llm_provider(self.llm_provider_name, run_dir=store.run_dir),
            run_dir=str(store.run_dir),
        )
        request_ids = []
        for question_item in questions:
            question_text = question_item.get("question", "")
            question_id = question_item.get("question_id", f"q_{uuid.uuid4().hex[:6]}")
            graph_results = provider.search(state.run_id, question_text, limit=10)
            result = runtime.run_task(
                run_id=state.run_id,
                task_type="answer_agent_questionnaire",
                stage="interaction",
                expected_schema=AGENT_QUESTIONNAIRE_OUTPUT_SCHEMA,
                input_text=store.read_seed_text(),
                input_files=state.seed_files,
                structured_input={
                    "questionnaire_id": questionnaire_id,
                    "question_id": question_id,
                    "questions": [{"question_id": question_id, "question": question_text}],
                    "agents": agents,
                    "requirement": state.requirement,
                    "graph_results": graph_results,
                    "artifacts": self._artifact_context(store),
                },
                system_prompt=(
                    "Answer the questionnaire on behalf of all agents. "
                    "Each agent should answer from their own persona's perspective."
                ),
                user_prompt=question_text,
                validation_rules={"strict": True},
                retry_policy={"max_repair_attempts": 1},
                context_refs=self._context_refs(store),
                output_contract={"schema": AGENT_QUESTIONNAIRE_OUTPUT_SCHEMA},
            )
            if result.status == "need_agent_response":
                request_ids.append(result.request_id)
            elif result.status == "ok":
                self._persist_questionnaire_answers(store, state.run_id, questionnaire_id, result.output or {})
        # Save questionnaire metadata
        self._persist_questionnaire_metadata(store, questionnaire_id, questions, request_ids)
        return {
            "status": "ok" if not request_ids else "need_agent_response",
            "questionnaire_id": questionnaire_id,
            "request_ids": request_ids,
            "question_count": len(questions),
            "agent_count": len(agents),
        }

    def get_questionnaire_result(self, run_dir: str, questionnaire_id: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        questionnaires_dir = store.artifacts_dir / "interactions" / "questionnaires"
        meta_path = questionnaires_dir / f"{questionnaire_id}_meta.json"
        if not meta_path.exists():
            return {"status": "error", "error": f"questionnaire not found: {questionnaire_id}"}
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        # Collect all answers
        answers = []
        answers_path = questionnaires_dir / f"{questionnaire_id}_answers.json"
        if answers_path.exists():
            answers = json.loads(answers_path.read_text(encoding="utf-8"))
        # Check pending requests
        pending_request_ids = meta.get("request_ids", [])
        queue = AgentQueue(run_dir)
        for req_id in list(pending_request_ids):
            response_path = store.responses_dir / f"{req_id}.json"
            if response_path.exists():
                try:
                    validation = queue.submit_response(response_path)
                    if validation.ok:
                        response = queue.load_response(req_id)
                        if response.status == "ok":
                            new_answers = self._extract_questionnaire_answers(response.output)
                            answers.extend(new_answers)
                            # Persist summary_markdown from response if present
                            resp_summary = response.output.get("summary_markdown", "")
                            if resp_summary:
                                meta["summary_markdown"] = resp_summary
                            pending_request_ids.remove(req_id)
                except Exception:
                    pass
        # Re-save answers if we collected new ones
        if answers:
            self._write_json(answers_path, answers)
        # Update metadata
        meta["request_ids"] = pending_request_ids
        meta["answer_count"] = len(answers)
        self._write_json(meta_path, meta)
        summary = meta.get("summary_markdown", "")
        return {
            "status": "ok" if not pending_request_ids else "partial",
            "questionnaire_id": questionnaire_id,
            "answers": answers,
            "answer_count": len(answers),
            "pending_request_ids": pending_request_ids,
            "summary_markdown": summary,
        }

    def ask_report_question(self, run_dir: str, question: str, limit: int = 20) -> Dict[str, Any]:
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
            task_type="ask_report_question",
            stage="interaction",
            expected_schema=REPORT_QUESTION_OUTPUT_SCHEMA,
            input_text=store.read_seed_text(),
            input_files=state.seed_files,
            structured_input={
                "question": question,
                "requirement": state.requirement,
                "graph_results": graph_results,
                "artifacts": self._artifact_context(store),
            },
            system_prompt="Answer a question about the prediction report using only run artifacts and GraphProvider retrieval context.",
            user_prompt=question,
            validation_rules={"strict": True},
            retry_policy={"max_repair_attempts": 1},
            context_refs=self._context_refs(store),
            output_contract={"schema": REPORT_QUESTION_OUTPUT_SCHEMA},
        )
        if result.status == "need_agent_response":
            return result.to_dict() | {"stage": "interaction", "type": "ask_report_question"}
        if result.status != "ok":
            return {"status": "failed", "stage": "interaction", "error": result.error}
        return self._persist_report_question_answer(store, state.run_id, f"mock_{uuid.uuid4().hex[:8]}", question, result.output or {})

    def generate_web_console(self, run_dir: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        web_dir = store.artifacts_dir / "web"
        web_dir.mkdir(parents=True, exist_ok=True)
        html_path = web_dir / "index.html"
        # Read all relevant artifacts for embedding
        artifact_data = self._collect_web_console_data(store)
        artifact_data["run_dir"] = str(store.run_dir)
        html_content = self._render_web_console_html(artifact_data)
        html_path.write_text(html_content, encoding="utf-8")
        return {
            "status": "ok",
            "path": str(html_path),
            "artifact_count": len(artifact_data),
        }

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
        selected_ids = _resolve_all_agent_ids(selected_profiles)
        round_id = f"round_{round_index}"
        return {
            "round_id": round_id,
            "round_index": round_index,
            "simulation_settings": settings,
            "actions": [
                {
                    "agent_id": selected_ids[index],
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
            "agent_ids": _resolve_all_agent_ids(profiles),
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

    # ── Interaction helper methods ─────────────────────────────────────────

    def _persist_agent_question_answer(
        self,
        store: RunStore,
        run_id: str,
        agent_id: str,
        request_id: str,
        question: str,
        output: Dict[str, Any],
    ) -> Dict[str, Any]:
        questions_dir = store.artifacts_dir / "interactions" / "agent_questions"
        questions_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = questions_dir / f"{request_id}.md"
        json_path = questions_dir / f"{request_id}.json"
        answer_md = output.get("answer_markdown", "")
        markdown_path.write_text(answer_md, encoding="utf-8")
        payload = {
            "run_id": run_id,
            "request_id": request_id,
            "agent_id": agent_id,
            "question": question,
            "answer": output,
        }
        self._write_json(json_path, payload)
        return {
            "status": "ok",
            "request_id": request_id,
            "agent_id": agent_id,
            "question": question,
            "answer": output,
            "artifacts": {
                "markdown": str(markdown_path),
                "json": str(json_path),
            },
        }

    def _persist_questionnaire_answers(
        self,
        store: RunStore,
        run_id: str,
        questionnaire_id: str,
        output: Dict[str, Any],
    ) -> None:
        questionnaires_dir = store.artifacts_dir / "interactions" / "questionnaires"
        questionnaires_dir.mkdir(parents=True, exist_ok=True)
        answers_path = questionnaires_dir / f"{questionnaire_id}_answers.json"
        existing = []
        if answers_path.exists():
            existing = json.loads(answers_path.read_text(encoding="utf-8"))
        new_answers = self._extract_questionnaire_answers(output)
        existing.extend(new_answers)
        self._write_json(answers_path, existing)

    def _extract_questionnaire_answers(self, output: Dict[str, Any]) -> List[Dict[str, Any]]:
        answers = output.get("answers", [])
        if isinstance(answers, list):
            return answers
        return []

    def _persist_questionnaire_metadata(
        self,
        store: RunStore,
        questionnaire_id: str,
        questions: List[Dict[str, Any]],
        request_ids: List[str],
    ) -> None:
        questionnaires_dir = store.artifacts_dir / "interactions" / "questionnaires"
        questionnaires_dir.mkdir(parents=True, exist_ok=True)
        meta_path = questionnaires_dir / f"{questionnaire_id}_meta.json"
        meta = {
            "questionnaire_id": questionnaire_id,
            "questions": questions,
            "request_ids": request_ids,
            "answer_count": 0,
            "summary_markdown": "",
        }
        self._write_json(meta_path, meta)

    def _persist_report_question_answer(
        self,
        store: RunStore,
        run_id: str,
        request_id: str,
        question: str,
        output: Dict[str, Any],
    ) -> Dict[str, Any]:
        rq_dir = store.artifacts_dir / "interactions" / "report_questions"
        rq_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = rq_dir / f"{request_id}.md"
        json_path = rq_dir / f"{request_id}.json"
        answer_md = output.get("answer_markdown", "")
        markdown_path.write_text(answer_md, encoding="utf-8")
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

    def get_report_question_answer(self, run_dir: str, request_id: str) -> Dict[str, Any]:
        store = RunStore(run_dir)
        state = store.load()
        queue = AgentQueue(run_dir)
        request = queue.load_request(request_id)
        if request.type != "ask_report_question" or request.stage != "interaction":
            return {"status": "error", "error": f"request {request_id} is not a report question request"}
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
        question = str(request.structured_input.get("question", ""))
        return self._persist_report_question_answer(store, state.run_id, request_id, question, response.output)

    def _collect_web_console_data(self, store: RunStore) -> Dict[str, Any]:
        artifacts = store.artifacts_dir
        data: Dict[str, Any] = {"run_id": "", "artifacts": {}}
        # Try to load state for run_id
        state_path = store.run_dir / "state.json"
        if state_path.exists():
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            data["run_id"] = state_data.get("run_id", "")
            data["requirement"] = state_data.get("requirement", "")
            data["simulation_settings"] = state_data.get("simulation_settings", {})
        # Read artifact files
        artifact_names = [
            "report.md",
            "verdict.json",
            "timeline.json",
            "graph_snapshot.json",
            "profiles.json",
            "simulation_config.json",
            "simulation_actions.json",
        ]
        for name in artifact_names:
            path = artifacts / name
            if path.exists():
                if name.endswith(".json"):
                    try:
                        data["artifacts"][name] = json.loads(path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        data["artifacts"][name] = None
                else:
                    data["artifacts"][name] = path.read_text(encoding="utf-8")
        # Read interaction data
        interactions: Dict[str, Any] = {"agent_questions": [], "questionnaires": []}
        aq_dir = artifacts / "interactions" / "agent_questions"
        if aq_dir.exists():
            for p in sorted(aq_dir.glob("*.json")):
                try:
                    interactions["agent_questions"].append(json.loads(p.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    pass
        q_dir = artifacts / "interactions" / "questionnaires"
        if q_dir.exists():
            for p in sorted(q_dir.glob("*_meta.json")):
                try:
                    meta = json.loads(p.read_text(encoding="utf-8"))
                    answers_path = p.parent / f"{meta['questionnaire_id']}_answers.json"
                    answers = []
                    if answers_path.exists():
                        answers = json.loads(answers_path.read_text(encoding="utf-8"))
                    meta["answers"] = answers
                    interactions["questionnaires"].append(meta)
                except json.JSONDecodeError:
                    pass
        data["interactions"] = interactions
        return data

    def _render_web_console_html(self, data: Dict[str, Any]) -> str:
        run_id = data.get("run_id", "unknown")
        requirement = data.get("requirement", "")
        artifacts = data.get("artifacts", {})
        interactions = data.get("interactions", {})
        report_md = artifacts.get("report.md", "# Report not yet generated")
        verdict = artifacts.get("verdict.json", {})
        timeline = artifacts.get("timeline.json", [])
        graph_snapshot = artifacts.get("graph_snapshot.json", {})
        profiles = artifacts.get("profiles.json", [])
        # Normalize profile IDs before embedding (mirrors JS _resolveId logic)
        if isinstance(profiles, list):
            _resolved = _resolve_all_agent_ids(profiles)
            for _pi, _pp in enumerate(profiles):
                if isinstance(_pp, dict):
                    _pp["agent_id"] = _resolved[_pi]
        sim_config = artifacts.get("simulation_config.json", {})
        sim_actions = artifacts.get("simulation_actions.json", [])
        agent_questions = interactions.get("agent_questions", [])
        questionnaires = interactions.get("questionnaires", [])
        # JSON-escape for embedding
        def json_embed(obj):
            return json.dumps(obj, ensure_ascii=False)
        return _WEB_CONSOLE_TEMPLATE.replace(
            "{{RUN_ID}}", html.escape(str(run_id), quote=True)
        ).replace(
            "{{RUN_ID_JSON}}", json_embed(run_id)
        ).replace(
            "{{REQUIREMENT_JSON}}", json_embed(requirement)
        ).replace(
            "{{REPORT_MD_JSON}}", json_embed(report_md)
        ).replace(
            "{{RUN_DIR_JSON}}", json_embed(data.get("run_dir", ""))
        ).replace(
            "{{VERDICT_JSON}}", json_embed(verdict)
        ).replace(
            "{{TIMELINE_JSON}}", json_embed(timeline)
        ).replace(
            "{{GRAPH_SNAPSHOT_JSON}}", json_embed(graph_snapshot)
        ).replace(
            "{{PROFILES_JSON}}", json_embed(profiles)
        ).replace(
            "{{SIM_CONFIG_JSON}}", json_embed(sim_config)
        ).replace(
            "{{SIM_ACTIONS_JSON}}", json_embed(sim_actions)
        ).replace(
            "{{AGENT_QUESTIONS_JSON}}", json_embed(agent_questions)
        ).replace(
            "{{QUESTIONNAIRES_JSON}}", json_embed(questionnaires)
        )


# ── Web Console HTML template ────────────────────────────────────────────

_WEB_CONSOLE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MiroFish Web Console — {{RUN_ID}}</title>
<style>
:root {
  --bg: #0f1117;
  --surface: #1a1d27;
  --surface2: #232733;
  --border: #2d3140;
  --text: #e2e4e9;
  --text2: #9ca0ad;
  --accent: #6c8cff;
  --accent2: #4a6adf;
  --success: #4caf50;
  --warn: #ff9800;
  --error: #f44336;
  --mono: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height:1.6; }
.app { display:flex; height:100vh; }
.sidebar { width:240px; background:var(--surface); border-right:1px solid var(--border); display:flex; flex-direction:column; flex-shrink:0; }
.sidebar-header { padding:16px 20px; border-bottom:1px solid var(--border); }
.sidebar-header h1 { font-size:16px; font-weight:600; color:var(--accent); }
.sidebar-header .run-id { font-size:11px; color:var(--text2); font-family:var(--mono); margin-top:4px; }
.nav { flex:1; padding:8px 0; overflow-y:auto; }
.nav-item { padding:10px 20px; cursor:pointer; color:var(--text2); font-size:13px; transition:all .15s; display:flex; align-items:center; gap:8px; }
.nav-item:hover { background:var(--surface2); color:var(--text); }
.nav-item.active { background:var(--surface2); color:var(--accent); border-right:2px solid var(--accent); }
.nav-icon { width:16px; text-align:center; font-size:14px; }
.api-status { padding:12px 20px; border-top:1px solid var(--border); font-size:11px; display:flex; align-items:center; gap:6px; }
.api-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
.api-dot.online { background:var(--success); }
.api-dot.offline { background:var(--error); }
.api-dot.checking { background:var(--warn); animation: pulse 1s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.main { flex:1; overflow-y:auto; }
.panel { display:none; padding:24px 32px; max-width:1000px; }
.panel.active { display:block; }
.panel h2 { font-size:20px; font-weight:600; margin-bottom:16px; color:var(--text); }
.panel h3 { font-size:15px; font-weight:600; margin:20px 0 8px; color:var(--text); }
.card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px 20px; margin-bottom:16px; }
.card-header { font-size:13px; font-weight:600; color:var(--text2); text-transform:uppercase; letter-spacing:.5px; margin-bottom:8px; }
.stat-row { display:flex; gap:16px; margin-bottom:16px; }
.stat { flex:1; background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:14px 16px; }
.stat-value { font-size:24px; font-weight:700; color:var(--accent); }
.stat-label { font-size:11px; color:var(--text2); text-transform:uppercase; letter-spacing:.5px; margin-top:2px; }
.badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }
.badge-ok { background:rgba(76,175,80,.15); color:var(--success); }
.badge-warn { background:rgba(255,152,0,.15); color:var(--warn); }
.badge-error { background:rgba(244,67,54,.15); color:var(--error); }
.badge-pending { background:rgba(108,140,255,.15); color:var(--accent); }
pre, code { font-family:var(--mono); font-size:12px; }
pre { background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:12px 16px; overflow-x:auto; white-space:pre-wrap; word-break:break-word; color:var(--text2); max-height:400px; overflow-y:auto; }
.report-content { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:24px; }
.report-content h1,.report-content h2,.report-content h3 { color:var(--accent); margin:16px 0 8px; }
.report-content p { margin-bottom:8px; color:var(--text); }
.agent-card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:12px; }
.agent-card .agent-name { font-weight:600; font-size:15px; color:var(--accent); }
.agent-card .agent-id { font-size:11px; color:var(--text2); font-family:var(--mono); }
.agent-card .agent-persona { font-size:13px; color:var(--text2); margin-top:4px; }
.timeline-item { display:flex; gap:12px; padding:8px 0; border-bottom:1px solid var(--border); }
.timeline-item:last-child { border-bottom:none; }
.timeline-round { font-weight:700; color:var(--accent); min-width:60px; font-size:13px; }
.timeline-detail { flex:1; font-size:13px; color:var(--text2); }
.graph-entity { display:inline-block; background:var(--surface2); border:1px solid var(--border); border-radius:4px; padding:4px 10px; margin:3px; font-size:12px; color:var(--text); font-family:var(--mono); }
.qa-item { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:12px; }
.qa-question { font-weight:600; color:var(--text); margin-bottom:8px; font-size:14px; }
.qa-answer { color:var(--text2); font-size:13px; white-space:pre-wrap; }
.qa-meta { font-size:11px; color:var(--text2); margin-top:8px; font-family:var(--mono); }
.empty-state { text-align:center; padding:48px; color:var(--text2); font-size:14px; }
.requirement-box { background:var(--surface2); border-left:3px solid var(--accent); padding:12px 16px; border-radius:0 6px 6px 0; margin-bottom:16px; font-size:14px; color:var(--text); }
.tab-bar { display:flex; gap:4px; margin-bottom:16px; }
.tab-btn { padding:6px 14px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--text2); cursor:pointer; font-size:12px; }
.tab-btn.active { background:var(--accent); color:#fff; border-color:var(--accent); }
.action-item { background:var(--surface2); border-radius:6px; padding:10px 14px; margin-bottom:8px; font-size:13px; }
.action-item .action-agent { color:var(--accent); font-weight:600; }
.action-item .action-type { color:var(--text2); font-size:11px; font-family:var(--mono); }
/* Interactive form styles */
.form-group { margin-bottom:14px; }
.form-group label { display:block; font-size:12px; font-weight:600; color:var(--text2); text-transform:uppercase; letter-spacing:.5px; margin-bottom:6px; }
.form-input, .form-select, .form-textarea { width:100%; background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:10px 14px; color:var(--text); font-size:13px; font-family:inherit; outline:none; transition:border-color .15s; }
.form-input:focus, .form-select:focus, .form-textarea:focus { border-color:var(--accent); }
.form-textarea { min-height:80px; resize:vertical; }
.form-select { appearance:none; background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%239ca0ad' d='M6 8L1 3h10z'/%3E%3C/svg%3E"); background-repeat:no-repeat; background-position:right 12px center; padding-right:32px; }
.form-select option { background:var(--surface); color:var(--text); }
.btn { display:inline-flex; align-items:center; gap:6px; padding:8px 18px; border-radius:6px; border:none; font-size:13px; font-weight:600; cursor:pointer; transition:all .15s; }
.btn-primary { background:var(--accent); color:#fff; }
.btn-primary:hover { background:var(--accent2); }
.btn-primary:disabled { opacity:.5; cursor:not-allowed; }
.btn-secondary { background:var(--surface2); color:var(--text); border:1px solid var(--border); }
.btn-secondary:hover { background:var(--border); }
.btn-sm { padding:5px 12px; font-size:12px; }
.btn-row { display:flex; gap:8px; align-items:center; margin-top:12px; }
.interaction-result { margin-top:16px; }
.spinner { display:inline-block; width:14px; height:14px; border:2px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation:spin .6s linear infinite; }
@keyframes spin { to{transform:rotate(360deg)} }
.toast { position:fixed; bottom:24px; right:24px; padding:12px 20px; border-radius:8px; font-size:13px; font-weight:500; z-index:9999; animation:slideUp .3s ease; }
.toast-success { background:rgba(76,175,80,.9); color:#fff; }
.toast-error { background:rgba(244,67,54,.9); color:#fff; }
@keyframes slideUp { from{transform:translateY(20px);opacity:0} to{transform:translateY(0);opacity:1} }
.question-row { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
.question-row .form-input { flex:1; }
.api-base-row { display:flex; gap:8px; align-items:center; margin-top:8px; }
.api-base-row .form-input { flex:1; font-size:11px; padding:6px 10px; }
.section-divider { border:none; border-top:1px solid var(--border); margin:20px 0; }
</style>
</head>
<body>
<div class="app">
  <div class="sidebar">
    <div class="sidebar-header">
      <h1>MiroFish Console</h1>
      <div class="run-id">{{RUN_ID}}</div>
    </div>
    <div class="nav" id="nav">
      <div class="nav-item active" data-panel="overview"><span class="nav-icon">&#9672;</span> Overview</div>
      <div class="nav-item" data-panel="report"><span class="nav-icon">&#9673;</span> Report</div>
      <div class="nav-item" data-panel="agents"><span class="nav-icon">&#9679;</span> Agents</div>
      <div class="nav-item" data-panel="timeline"><span class="nav-icon">&#9656;</span> Timeline</div>
      <div class="nav-item" data-panel="graph"><span class="nav-icon">&#9674;</span> Graph</div>
      <div class="nav-item" data-panel="simulation"><span class="nav-icon">&#9655;</span> Simulation</div>
      <div class="nav-item" data-panel="ask"><span class="nav-icon">&#9681;</span> Ask Agent</div>
      <div class="nav-item" data-panel="questionnaires"><span class="nav-icon">&#9683;</span> Questionnaires</div>
      <div class="nav-item" data-panel="report-q"><span class="nav-icon">?</span> Report Q&amp;A</div>
      <div class="nav-item" data-panel="history"><span class="nav-icon">&#9776;</span> History</div>
      <div class="nav-item" data-panel="raw"><span class="nav-icon">&lt;/&gt;</span> Raw Artifacts</div>
    </div>
    <div class="api-status" id="api-status">
      <span class="api-dot checking" id="api-dot"></span>
      <span id="api-status-text">Checking API...</span>
    </div>
    <div style="padding:0 20px 12px;">
      <div class="api-base-row">
        <input class="form-input" id="api-base-input" value="http://localhost:5001" placeholder="API Base URL" style="font-size:11px;">
        <button class="btn btn-sm btn-secondary" id="api-reconnect-btn">Connect</button>
      </div>
    </div>
  </div>
  <div class="main">
    <!-- Overview Panel -->
    <div class="panel active" id="panel-overview">
      <h2>Overview</h2>
      <div class="requirement-box" id="requirement-text"></div>
      <div class="stat-row" id="overview-stats"></div>
      <div class="card">
        <div class="card-header">Verdict</div>
        <div id="verdict-display"></div>
      </div>
    </div>
    <!-- Report Panel -->
    <div class="panel" id="panel-report">
      <h2>Report</h2>
      <div class="report-content" id="report-content"></div>
    </div>
    <!-- Agents Panel -->
    <div class="panel" id="panel-agents">
      <h2>Agents</h2>
      <div id="agents-list"></div>
    </div>
    <!-- Timeline Panel -->
    <div class="panel" id="panel-timeline">
      <h2>Timeline</h2>
      <div class="card" id="timeline-list"></div>
    </div>
    <!-- Graph Panel -->
    <div class="panel" id="panel-graph">
      <h2>Knowledge Graph</h2>
      <div class="stat-row" id="graph-stats"></div>
      <div class="card">
        <div class="card-header">Entities</div>
        <div id="graph-entities"></div>
      </div>
      <div class="card">
        <div class="card-header">Triples</div>
        <div id="graph-triples" style="max-height:400px;overflow-y:auto;"></div>
      </div>
    </div>
    <!-- Simulation Panel -->
    <div class="panel" id="panel-simulation">
      <h2>Simulation</h2>
      <div class="stat-row" id="sim-stats"></div>
      <div id="sim-actions"></div>
    </div>
    <!-- Ask Agent Panel (Interactive) -->
    <div class="panel" id="panel-ask">
      <h2>Ask an Agent</h2>
      <div class="card">
        <div class="card-header">New Question</div>
        <div class="form-group">
          <label>Select Agent</label>
          <select class="form-select" id="ask-agent-select">
            <option value="">-- Select an agent --</option>
          </select>
        </div>
        <div class="form-group">
          <label>Question</label>
          <textarea class="form-textarea" id="ask-question-input" placeholder="Type your question for this agent..."></textarea>
        </div>
        <div class="btn-row">
          <button class="btn btn-primary" id="ask-submit-btn" disabled>Submit Question</button>
          <span id="ask-status" style="font-size:12px;color:var(--text2);"></span>
        </div>
      </div>
      <div class="interaction-result" id="ask-result"></div>
      <hr class="section-divider">
      <h3>Previous Questions</h3>
      <div id="ask-history"></div>
    </div>
    <!-- Questionnaires Panel (Interactive) -->
    <div class="panel" id="panel-questionnaires">
      <h2>Questionnaires</h2>
      <div class="card">
        <div class="card-header">New Questionnaire</div>
        <div id="questionnaire-questions">
          <div class="question-row" data-qidx="0">
            <input class="form-input" placeholder="Question ID (e.g. q1)" style="max-width:140px;" value="q1">
            <input class="form-input" placeholder="Question text..." style="flex:1;">
            <button class="btn btn-sm btn-secondary" onclick="removeQuestionRow(this)">Remove</button>
          </div>
        </div>
        <div class="btn-row">
          <button class="btn btn-secondary btn-sm" id="add-question-btn">+ Add Question</button>
          <button class="btn btn-primary" id="questionnaire-submit-btn">Send Questionnaire</button>
          <span id="questionnaire-status" style="font-size:12px;color:var(--text2);"></span>
        </div>
      </div>
      <div class="interaction-result" id="questionnaire-result"></div>
      <hr class="section-divider">
      <h3>Previous Questionnaires</h3>
      <div id="questionnaire-history"></div>
    </div>
    <!-- Report Q&A Panel (Interactive) -->
    <div class="panel" id="panel-report-q">
      <h2>Report Q&amp;A</h2>
      <div class="card">
        <div class="card-header">Ask a question about the report</div>
        <div class="form-group">
          <label>Question</label>
          <textarea class="form-textarea" id="report-q-input" placeholder="Ask a follow-up question about the report findings..."></textarea>
        </div>
        <div class="btn-row">
          <button class="btn btn-primary" id="report-q-submit-btn">Submit Question</button>
          <span id="report-q-status" style="font-size:12px;color:var(--text2);"></span>
        </div>
      </div>
      <div class="interaction-result" id="report-q-result"></div>
      <hr class="section-divider">
      <h3>Previous Report Questions</h3>
      <div id="report-q-history"></div>
    </div>
    <!-- History Panel -->
    <div class="panel" id="panel-history">
      <h2>Interaction History</h2>
      <div id="all-interactions-list"></div>
    </div>
    <!-- Raw Artifacts Panel -->
    <div class="panel" id="panel-raw">
      <h2>Raw Artifacts</h2>
      <div class="tab-bar" id="raw-tabs"></div>
      <div id="raw-content"></div>
    </div>
  </div>
</div>
<script>
(function() {
  // ── Embedded data ──────────────────────────────────────────────────────
  const DATA = {
    runId: {{RUN_ID_JSON}},
    requirement: {{REQUIREMENT_JSON}},
    reportMd: {{REPORT_MD_JSON}},
    verdict: {{VERDICT_JSON}},
    timeline: {{TIMELINE_JSON}},
    graphSnapshot: {{GRAPH_SNAPSHOT_JSON}},
    profiles: {{PROFILES_JSON}},
    simConfig: {{SIM_CONFIG_JSON}},
    simActions: {{SIM_ACTIONS_JSON}},
    agentQuestions: {{AGENT_QUESTIONS_JSON}},
    questionnaires: {{QUESTIONNAIRES_JSON}},
    runDir: {{RUN_DIR_JSON}}
  };

  // ── Agent ID resolver (mirrors Python _resolve_agent_id) ──────────────
  function _resolveId(p, idx) {
    if (p.agent_id) return String(p.agent_id).trim() || _posFallback(idx);
    if (p.user_id) return String(p.user_id).trim() || _posFallback(idx);
    if (p.name && typeof p.name === "string" && p.name.trim()) {
      var slug = p.name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
      return slug || _posFallback(idx);
    }
    return _posFallback(idx);
  }
  function _posFallback(idx) { return "agent_" + ((idx || 0) + 1); }

  // Normalize embedded profiles so each has a guaranteed unique agent_id
  var _profiles = Array.isArray(DATA.profiles) ? DATA.profiles : [];
  var _seenIds = {};
  _profiles.forEach(function(p, i) {
    if (!p.agent_id) p.agent_id = _resolveId(p, i);
    if (_seenIds[p.agent_id]) {
      _seenIds[p.agent_id]++;
      var newId = p.agent_id + "_" + _seenIds[p.agent_id];
      while (_seenIds[newId]) { _seenIds[p.agent_id]++; newId = p.agent_id + "_" + _seenIds[p.agent_id]; }
      p.agent_id = newId;
      _seenIds[newId] = 1;
    } else {
      _seenIds[p.agent_id] = 1;
    }
  });

  // ── API Client ─────────────────────────────────────────────────────────
  let API_BASE = "http://localhost:5001";
  let API_ONLINE = false;
  let pollTimers = {};
  const POLL_INTERVAL = 3000;
  const POLL_MAX = 120000;

  function apiUrl(path) {
    const base = API_BASE.replace(/\/+$/, "");
    const sep = path.indexOf("?") >= 0 ? "&" : "?";
    return base + "/api/interaction" + path + sep + "run=" + encodeURIComponent(DATA.runDir);
  }

  async function apiGet(path) {
    const resp = await fetch(apiUrl(path), { method: "GET" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }

  async function apiPost(path, body) {
    const resp = await fetch(apiUrl(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }

  async function checkApiStatus() {
    const dot = document.getElementById("api-dot");
    const txt = document.getElementById("api-status-text");
    dot.className = "api-dot checking";
    txt.textContent = "Checking API...";
    try {
      const resp = await fetch(apiUrl("/agents"), { method: "GET", signal: AbortSignal.timeout(5000) });
      if (resp.ok) {
        API_ONLINE = true;
        dot.className = "api-dot online";
        txt.textContent = "API Connected";
        // Prefer API agent list (backend-normalized IDs) over embedded fallback
        _refreshDropdownFromApi(resp);
      } else {
        API_ONLINE = false;
        dot.className = "api-dot offline";
        txt.textContent = "API Error";
      }
    } catch (e) {
      API_ONLINE = false;
      dot.className = "api-dot offline";
      txt.textContent = "API Offline";
    }
  }

  async function _refreshDropdownFromApi(initialResp) {
    try {
      var body = initialResp ? await initialResp.json() : null;
      if (!body || !body.success) {
        var r = await apiGet("/agents");
        body = r;
      }
      if (!body || !body.success || !body.data) return;
      var apiAgents = body.data.agents || [];
      if (apiAgents.length === 0) return;
      // Rebuild dropdown from API data (backend IDs are authoritative)
      var sel = document.getElementById("ask-agent-select");
      sel.innerHTML = '<option value="">-- Select an agent --</option>';
      apiAgents.forEach(function(a) {
        var opt = document.createElement("option");
        opt.value = a.agent_id;
        opt.textContent = (a.name || a.agent_id) + " (" + a.agent_id + ")";
        sel.appendChild(opt);
      });
      // Also update the profiles array for agent cards display
      _profiles.length = 0;
      apiAgents.forEach(function(a, i) {
        var p = a.profile || { agent_id: a.agent_id, name: a.name, persona: a.persona };
        if (!p.agent_id) p.agent_id = a.agent_id;
        _profiles.push(p);
      });
      // Re-render agent cards
      _renderAgentCards();
    } catch (e) { /* API dropdown refresh is best-effort */ }
  }

  function _renderAgentCards() {
    var list = document.getElementById("agents-list");
    if (_profiles.length === 0) {
      list.innerHTML = '<div class="empty-state">No agent profiles generated yet.</div>';
    } else {
      list.innerHTML = _profiles.map(function(p, i) {
        var aid = p.agent_id;
        return '<div class="agent-card"><div class="agent-name">' + (p.name || aid) + '</div>' +
          '<div class="agent-id">' + aid + '</div>' +
          (p.persona ? '<div class="agent-persona">' + p.persona + '</div>' : '') +
          '<pre style="margin-top:8px;">' + JSON.stringify(p, null, 2) + '</pre></div>';
      }).join("");
    }
  }

  function pollForAnswer(requestId, callback) {
    const startTime = Date.now();
    const timerId = setInterval(async function() {
      if (Date.now() - startTime > POLL_MAX) {
        clearInterval(timerId);
        callback(null, "Timed out waiting for agent response");
        return;
      }
      try {
        const res = await apiGet("/requests/" + requestId + "/status");
        if (res.success && res.data.has_response) {
          clearInterval(timerId);
          callback(true, null);
        }
      } catch (e) {
        // keep polling
      }
    }, POLL_INTERVAL);
    pollTimers[requestId] = timerId;
  }

  function showToast(msg, type) {
    const el = document.createElement("div");
    el.className = "toast toast-" + (type || "success");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function() { el.remove(); }, 3500);
  }

  function renderAnswerCard(container, data, label) {
    if (!data || data.status === "error" || data.status === "missing" || data.status === "failed") {
      container.innerHTML = '<div class="card"><span class="badge badge-error">' + (data ? data.status : "error") + '</span> ' +
        (data && data.error ? data.error : "No response received") + '</div>';
      return;
    }
    const output = data.output || data;
    const ansMd = output.answer_markdown || output.summary_markdown || JSON.stringify(output, null, 2);
    const confidence = output.confidence != null ? (output.confidence * 100).toFixed(0) + "%" : "-";
    const agentId = output.agent_id || data.agent_id || label || "";
    container.innerHTML = '<div class="card">' +
      '<div class="card-header">Answer</div>' +
      '<div class="qa-answer">' + ansMd + '</div>' +
      '<div class="qa-meta">Agent: ' + agentId + ' | Confidence: ' + confidence + '</div>' +
      '</div>';
  }

  // ── Navigation ─────────────────────────────────────────────────────────
  document.querySelectorAll(".nav-item").forEach(function(item) {
    item.addEventListener("click", function() {
      document.querySelectorAll(".nav-item").forEach(function(n) { n.classList.remove("active"); });
      document.querySelectorAll(".panel").forEach(function(p) { p.classList.remove("active"); });
      this.classList.add("active");
      document.getElementById("panel-" + this.dataset.panel).classList.add("active");
    });
  });

  // ── API Base URL config ────────────────────────────────────────────────
  document.getElementById("api-reconnect-btn").addEventListener("click", function() {
    API_BASE = document.getElementById("api-base-input").value || "http://localhost:5001";
    checkApiStatus();
  });
  document.getElementById("api-base-input").addEventListener("keydown", function(e) {
    if (e.key === "Enter") { API_BASE = this.value || "http://localhost:5001"; checkApiStatus(); }
  });

  // ── Overview ───────────────────────────────────────────────────────────
  document.getElementById("requirement-text").textContent = DATA.requirement || "No requirement specified";
  var profiles = _profiles;
  var timeline = Array.isArray(DATA.timeline) ? DATA.timeline : [];
  var simActions = Array.isArray(DATA.simActions) ? DATA.simActions : [];
  var triples = Array.isArray(DATA.graphSnapshot) ? DATA.graphSnapshot : (DATA.graphSnapshot && DATA.graphSnapshot.triples ? DATA.graphSnapshot.triples : []);

  var statsHtml = [
    { value: profiles.length, label: "Agents" },
    { value: timeline.length, label: "Timeline Events" },
    { value: triples.length, label: "Graph Triples" },
    { value: simActions.length, label: "Sim Actions" }
  ].map(function(s) { return '<div class="stat"><div class="stat-value">' + s.value + '</div><div class="stat-label">' + s.label + '</div></div>'; }).join("");
  document.getElementById("overview-stats").innerHTML = statsHtml;

  var v = DATA.verdict || {};
  var verdictBadge = v.status === "ok" ? "badge-ok" : (v.status === "mock" ? "badge-warn" : "badge-error");
  document.getElementById("verdict-display").innerHTML =
    '<span class="badge ' + verdictBadge + '">' + (v.status || "unknown") + '</span>' +
    (v.confidence ? ' <span style="color:var(--text2);font-size:13px;">Confidence: ' + (v.confidence * 100).toFixed(0) + '%</span>' : '') +
    '<pre style="margin-top:12px;">' + JSON.stringify(v, null, 2) + '</pre>';

  // ── Report ─────────────────────────────────────────────────────────────
  function mdToHtml(md) {
    return md
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/`(.+?)`/g, '<code>$1</code>')
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      .replace(/\n\n/g, '</p><p>')
      .replace(/\n/g, '<br>');
  }
  document.getElementById("report-content").innerHTML = '<p>' + mdToHtml(DATA.reportMd) + '</p>';

  // ── Agents ─────────────────────────────────────────────────────────────
  if (profiles.length === 0) {
    document.getElementById("agents-list").innerHTML = '<div class="empty-state">No agent profiles generated yet.</div>';
  } else {
    document.getElementById("agents-list").innerHTML = profiles.map(function(p, i) {
      var aid = p.agent_id;
      return '<div class="agent-card"><div class="agent-name">' + (p.name || aid) + '</div>' +
        '<div class="agent-id">' + aid + '</div>' +
        (p.persona ? '<div class="agent-persona">' + p.persona + '</div>' : '') +
        '<pre style="margin-top:8px;">' + JSON.stringify(p, null, 2) + '</pre></div>';
    }).join("");
  }

  // ── Timeline ───────────────────────────────────────────────────────────
  if (timeline.length === 0) {
    document.getElementById("timeline-list").innerHTML = '<div class="empty-state">No timeline events.</div>';
  } else {
    document.getElementById("timeline-list").innerHTML = timeline.map(function(t, i) {
      return '<div class="timeline-item"><div class="timeline-round">R' + (t.round || i + 1) + '</div>' +
        '<div class="timeline-detail">' + (t.summary || t.fact || JSON.stringify(t)) + '</div></div>';
    }).join("");
  }

  // ── Graph ──────────────────────────────────────────────────────────────
  var entities = new Set();
  triples.forEach(function(t) { if(t.subject) entities.add(t.subject); if(t.object) entities.add(t.object); });
  document.getElementById("graph-stats").innerHTML =
    '<div class="stat"><div class="stat-value">' + entities.size + '</div><div class="stat-label">Entities</div></div>' +
    '<div class="stat"><div class="stat-value">' + triples.length + '</div><div class="stat-label">Triples</div></div>';
  document.getElementById("graph-entities").innerHTML = Array.from(entities).map(function(e) {
    return '<span class="graph-entity">' + e + '</span>';
  }).join("") || '<div class="empty-state">No entities.</div>';
  if (triples.length === 0) {
    document.getElementById("graph-triples").innerHTML = '<div class="empty-state">No triples.</div>';
  } else {
    document.getElementById("graph-triples").innerHTML = triples.slice(0, 50).map(function(t) {
      return '<div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:12px;">' +
        '<span style="color:var(--accent);">' + (t.subject||'') + '</span> ' +
        '<span style="color:var(--text2);">[' + (t.predicate||'') + ']</span> ' +
        '<span style="color:var(--success);">' + (t.object||'') + '</span>' +
        (t.confidence ? ' <span class="badge badge-ok">' + (t.confidence*100).toFixed(0) + '%</span>' : '') +
        '</div>';
    }).join("");
  }

  // ── Simulation ─────────────────────────────────────────────────────────
  var cfg = DATA.simConfig || {};
  document.getElementById("sim-stats").innerHTML =
    '<div class="stat"><div class="stat-value">' + (cfg.rounds || cfg.simulation_rounds || 0) + '</div><div class="stat-label">Rounds</div></div>' +
    '<div class="stat"><div class="stat-value">' + (cfg.round_unit || '-') + '</div><div class="stat-label">Round Unit</div></div>' +
    '<div class="stat"><div class="stat-value">' + simActions.length + '</div><div class="stat-label">Actions</div></div>';
  if (simActions.length === 0) {
    document.getElementById("sim-actions").innerHTML = '<div class="empty-state">No simulation actions.</div>';
  } else {
    document.getElementById("sim-actions").innerHTML = simActions.slice(0, 100).map(function(a) {
      return '<div class="action-item"><span class="action-agent">' + (a.agent_id||'') + '</span> ' +
        '<span class="action-type">' + (a.action_type||'') + '</span><br>' +
        '<span style="color:var(--text);">' + (a.content||'') + '</span></div>';
    }).join("");
  }

  // ── Ask Agent (Interactive) ────────────────────────────────────────────
  var agentSelect = document.getElementById("ask-agent-select");
  var askSubmitBtn = document.getElementById("ask-submit-btn");
  var askQuestionInput = document.getElementById("ask-question-input");
  var askResultDiv = document.getElementById("ask-result");
  var askStatusSpan = document.getElementById("ask-status");

  profiles.forEach(function(p, i) {
    var aid = p.agent_id;
    var opt = document.createElement("option");
    opt.value = aid;
    opt.textContent = (p.name || aid) + " (" + aid + ")";
    agentSelect.appendChild(opt);
  });

  agentSelect.addEventListener("change", function() {
    askSubmitBtn.disabled = !this.value || !askQuestionInput.value.trim();
  });
  askQuestionInput.addEventListener("input", function() {
    askSubmitBtn.disabled = !agentSelect.value || !this.value.trim();
  });

  askSubmitBtn.addEventListener("click", async function() {
    var agentId = agentSelect.value;
    var question = askQuestionInput.value.trim();
    if (!agentId || !question) return;
    if (!API_ONLINE) { showToast("API is offline. Start the backend server first.", "error"); return; }

    askSubmitBtn.disabled = true;
    askStatusSpan.innerHTML = '<span class="spinner"></span> Submitting...';
    askResultDiv.innerHTML = "";

    try {
      var res = await apiPost("/agents/" + encodeURIComponent(agentId) + "/ask", { question: question, limit: 20 });
      var data = res.data || {};
      var requestId = data.request_id;
      if (!requestId) {
        askStatusSpan.textContent = "";
        renderAnswerCard(askResultDiv, data, agentId);
        askSubmitBtn.disabled = false;
        return;
      }
      askStatusSpan.innerHTML = '<span class="spinner"></span> Waiting for agent response...';
      askResultDiv.innerHTML = '<div class="card"><span class="badge badge-pending">pending</span> Request ' + requestId + ' submitted. Polling for response...</div>';

      pollForAnswer(requestId, async function(ok, err) {
        if (!ok) {
          askStatusSpan.textContent = "";
          askResultDiv.innerHTML = '<div class="card"><span class="badge badge-error">timeout</span> ' + (err || "No response received") + '</div>';
          askSubmitBtn.disabled = false;
          return;
        }
        try {
          var ansRes = await apiPost("/agents/answer/" + encodeURIComponent(requestId), {});
          askStatusSpan.textContent = "";
          renderAnswerCard(askResultDiv, ansRes.data, agentId);
          showToast("Agent answered your question!");
        } catch (e2) {
          askStatusSpan.textContent = "";
          askResultDiv.innerHTML = '<div class="card"><span class="badge badge-error">error</span> Failed to retrieve answer: ' + e2.message + '</div>';
        }
        askSubmitBtn.disabled = false;
      });
    } catch (e) {
      askStatusSpan.textContent = "";
      askResultDiv.innerHTML = '<div class="card"><span class="badge badge-error">error</span> ' + e.message + '</div>';
      askSubmitBtn.disabled = false;
    }
  });

  // ── Questionnaires (Interactive) ───────────────────────────────────────
  var questionnaireQuestionsDiv = document.getElementById("questionnaire-questions");
  var addQuestionBtn = document.getElementById("add-question-btn");
  var questionnaireSubmitBtn = document.getElementById("questionnaire-submit-btn");
  var questionnaireResultDiv = document.getElementById("questionnaire-result");
  var questionnaireStatusSpan = document.getElementById("questionnaire-status");
  var questionCounter = 1;

  addQuestionBtn.addEventListener("click", function() {
    questionCounter++;
    var row = document.createElement("div");
    row.className = "question-row";
    row.dataset.qidx = questionCounter - 1;
    row.innerHTML = '<input class="form-input" placeholder="Question ID (e.g. q' + questionCounter + ')" style="max-width:140px;" value="q' + questionCounter + '">' +
      '<input class="form-input" placeholder="Question text..." style="flex:1;">' +
      '<button class="btn btn-sm btn-secondary" onclick="removeQuestionRow(this)">Remove</button>';
    questionnaireQuestionsDiv.appendChild(row);
  });

  window.removeQuestionRow = function(btn) {
    var rows = questionnaireQuestionsDiv.querySelectorAll(".question-row");
    if (rows.length <= 1) { showToast("Need at least one question", "error"); return; }
    btn.closest(".question-row").remove();
  };

  questionnaireSubmitBtn.addEventListener("click", async function() {
    if (!API_ONLINE) { showToast("API is offline. Start the backend server first.", "error"); return; }

    var rows = questionnaireQuestionsDiv.querySelectorAll(".question-row");
    var questions = [];
    rows.forEach(function(row) {
      var inputs = row.querySelectorAll("input");
      var qid = inputs[0].value.trim();
      var qtxt = inputs[1].value.trim();
      if (qid && qtxt) questions.push({ question_id: qid, question: qtxt });
    });
    if (questions.length === 0) { showToast("Add at least one question with ID and text", "error"); return; }

    questionnaireSubmitBtn.disabled = true;
    questionnaireStatusSpan.innerHTML = '<span class="spinner"></span> Sending...';
    questionnaireResultDiv.innerHTML = "";

    try {
      var res = await apiPost("/questionnaires", { questions: questions });
      var data = res.data || {};
      var questionnaireId = data.questionnaire_id;
      if (!questionnaireId) {
        questionnaireStatusSpan.textContent = "";
        questionnaireResultDiv.innerHTML = '<div class="card">' + JSON.stringify(data, null, 2) + '</div>';
        questionnaireSubmitBtn.disabled = false;
        return;
      }
      questionnaireStatusSpan.innerHTML = '<span class="spinner"></span> Questionnaire ' + questionnaireId + ' sent. Waiting for agent responses...';
      questionnaireResultDiv.innerHTML = '<div class="card"><span class="badge badge-pending">pending</span> Polling for responses...</div>';

      // Poll for questionnaire completion
      var startTime = Date.now();
      var pollTimer = setInterval(async function() {
        if (Date.now() - startTime > POLL_MAX) {
          clearInterval(pollTimer);
          questionnaireStatusSpan.textContent = "";
          questionnaireResultDiv.innerHTML = '<div class="card"><span class="badge badge-warn">timeout</span> Questionnaire timed out. Some agents may not have responded.</div>';
          questionnaireSubmitBtn.disabled = false;
          return;
        }
        try {
          var qRes = await apiGet("/questionnaires/" + encodeURIComponent(questionnaireId));
          var qData = qRes.data || {};
          var answers = qData.answers || [];
          var totalExpected = profiles.length * questions.length;
          if (answers.length >= totalExpected || (qData.status === "completed")) {
            clearInterval(pollTimer);
            questionnaireStatusSpan.textContent = "";
            var summaryMd = qData.summary_markdown || "";
            var answersHtml = answers.map(function(a) {
              return '<div class="qa-item"><div class="qa-question">' + (a.question_id||"") + " — " + (a.agent_id||"") + '</div>' +
                '<div class="qa-answer">' + (a.answer_markdown||"") + '</div>' +
                '<div class="qa-meta">Confidence: ' + ((a.confidence||0)*100).toFixed(0) + '%</div></div>';
            }).join("");
            questionnaireResultDiv.innerHTML = '<div class="card"><div class="card-header">Results</div>' +
              (summaryMd ? '<div class="qa-answer" style="margin-bottom:12px;">' + summaryMd + '</div>' : '') +
              answersHtml + '</div>';
            showToast("Questionnaire completed with " + answers.length + " answers!");
          } else {
            questionnaireStatusSpan.innerHTML = '<span class="spinner"></span> ' + answers.length + '/' + totalExpected + ' responses received...';
          }
        } catch (e) { /* keep polling */ }
      }, POLL_INTERVAL);
    } catch (e) {
      questionnaireStatusSpan.textContent = "";
      questionnaireResultDiv.innerHTML = '<div class="card"><span class="badge badge-error">error</span> ' + e.message + '</div>';
    }
    questionnaireSubmitBtn.disabled = false;
  });

  // ── Report Q&A (Interactive) ──────────────────────────────────────────
  var reportQInput = document.getElementById("report-q-input");
  var reportQSubmitBtn = document.getElementById("report-q-submit-btn");
  var reportQResultDiv = document.getElementById("report-q-result");
  var reportQStatusSpan = document.getElementById("report-q-status");

  reportQSubmitBtn.addEventListener("click", async function() {
    var question = reportQInput.value.trim();
    if (!question) return;
    if (!API_ONLINE) { showToast("API is offline. Start the backend server first.", "error"); return; }

    reportQSubmitBtn.disabled = true;
    reportQStatusSpan.innerHTML = '<span class="spinner"></span> Submitting...';
    reportQResultDiv.innerHTML = "";

    try {
      var res = await apiPost("/report-questions", { question: question, limit: 20 });
      var data = res.data || {};
      var requestId = data.request_id;
      if (!requestId) {
        reportQStatusSpan.textContent = "";
        renderAnswerCard(reportQResultDiv, data, "report");
        reportQSubmitBtn.disabled = false;
        return;
      }
      reportQStatusSpan.innerHTML = '<span class="spinner"></span> Waiting for response...';
      reportQResultDiv.innerHTML = '<div class="card"><span class="badge badge-pending">pending</span> Request ' + requestId + ' submitted. Polling...</div>';

      pollForAnswer(requestId, async function(ok, err) {
        if (!ok) {
          reportQStatusSpan.textContent = "";
          reportQResultDiv.innerHTML = '<div class="card"><span class="badge badge-error">timeout</span> ' + (err || "No response received") + '</div>';
          reportQSubmitBtn.disabled = false;
          return;
        }
        try {
          var ansRes = await apiPost("/report-questions/answer/" + encodeURIComponent(requestId), {});
          reportQStatusSpan.textContent = "";
          renderAnswerCard(reportQResultDiv, ansRes.data, "report");
          showToast("Report question answered!");
        } catch (e2) {
          reportQStatusSpan.textContent = "";
          reportQResultDiv.innerHTML = '<div class="card"><span class="badge badge-error">error</span> Failed to retrieve answer: ' + e2.message + '</div>';
        }
        reportQSubmitBtn.disabled = false;
      });
    } catch (e) {
      reportQStatusSpan.textContent = "";
      reportQResultDiv.innerHTML = '<div class="card"><span class="badge badge-error">error</span> ' + e.message + '</div>';
      reportQSubmitBtn.disabled = false;
    }
  });

  // ── History: render embedded Q&A and questionnaire data ────────────────
  var aqs = DATA.agentQuestions || [];
  var qs = DATA.questionnaires || [];

  function renderStaticQA() {
    var container = document.getElementById("ask-history");
    if (aqs.length === 0) {
      container.innerHTML = '<div class="empty-state" style="padding:24px;">No previous agent questions.</div>';
    } else {
      container.innerHTML = aqs.map(function(q) {
        var ans = q.answer || {};
        return '<div class="qa-item"><div class="qa-question">Q: ' + (q.question||'') + '</div>' +
          '<div class="qa-answer">' + (ans.answer_markdown || '') + '</div>' +
          '<div class="qa-meta">Agent: ' + (q.agent_id||'') + ' | Confidence: ' + ((ans.confidence||0)*100).toFixed(0) + '%</div></div>';
      }).join("");
    }
  }

  function renderStaticQuestionnaires() {
    var container = document.getElementById("questionnaire-history");
    if (qs.length === 0) {
      container.innerHTML = '<div class="empty-state" style="padding:24px;">No previous questionnaires.</div>';
    } else {
      container.innerHTML = qs.map(function(q) {
        var answers = q.answers || [];
        var qhtml = answers.map(function(a) {
          return '<div class="qa-item" style="margin-bottom:8px;"><div class="qa-question">' + (a.question_id||'') + ' — ' + (a.agent_id||'') + '</div>' +
            '<div class="qa-answer">' + (a.answer_markdown||'') + '</div>' +
            '<div class="qa-meta">Confidence: ' + ((a.confidence||0)*100).toFixed(0) + '%</div></div>';
        }).join("");
        return '<div class="card"><div class="card-header">Questionnaire: ' + (q.questionnaire_id||'') + '</div>' +
          '<div style="font-size:13px;color:var(--text2);margin-bottom:12px;">Questions: ' + (q.questions||[]).length + ' | Answers: ' + answers.length + '</div>' +
          (q.summary_markdown ? '<div class="qa-answer" style="margin-bottom:12px;">' + q.summary_markdown + '</div>' : '') +
          (qhtml || '<div class="empty-state">No answers yet.</div>') + '</div>';
      }).join("");
    }
  }

  function renderAllInteractions() {
    var container = document.getElementById("all-interactions-list");
    var html = "";
    if (aqs.length > 0) {
      html += "<h3>Agent Questions</h3>";
      html += aqs.map(function(q) {
        var ans = q.answer || {};
        return '<div class="qa-item"><div class="qa-question">Q: ' + (q.question||'') + '</div>' +
          '<div class="qa-answer">' + (ans.answer_markdown || '') + '</div>' +
          '<div class="qa-meta">Agent: ' + (q.agent_id||'') + ' | Confidence: ' + ((ans.confidence||0)*100).toFixed(0) + '%</div></div>';
      }).join("");
    }
    if (qs.length > 0) {
      html += "<h3>Questionnaires</h3>";
      html += qs.map(function(q) {
        var answers = q.answers || [];
        return '<div class="card"><div class="card-header">Questionnaire: ' + (q.questionnaire_id||'') + '</div>' +
          '<div style="font-size:13px;color:var(--text2);margin-bottom:8px;">Questions: ' + (q.questions||[]).length + ' | Answers: ' + answers.length + '</div></div>';
      }).join("");
    }
    if (!html) html = '<div class="empty-state">No interactions recorded yet.</div>';
    container.innerHTML = html;
  }

  renderStaticQA();
  renderStaticQuestionnaires();
  renderAllInteractions();

  // ── Raw Artifacts ──────────────────────────────────────────────────────
  var rawArtifacts = {
    "verdict.json": DATA.verdict,
    "timeline.json": DATA.timeline,
    "graph_snapshot.json": DATA.graphSnapshot,
    "profiles.json": DATA.profiles,
    "simulation_config.json": DATA.simConfig,
    "simulation_actions.json": DATA.simActions,
    "report.md": DATA.reportMd
  };
  var tabNames = Object.keys(rawArtifacts);
  document.getElementById("raw-tabs").innerHTML = tabNames.map(function(name, i) {
    return '<button class="tab-btn' + (i === 0 ? ' active' : '') + '" data-tab="' + name + '">' + name + '</button>';
  }).join("");
  function showRawTab(name) {
    var val = rawArtifacts[name];
    var content = typeof val === "string" ? val : JSON.stringify(val, null, 2);
    document.getElementById("raw-content").innerHTML = '<pre>' + (content || '(empty)') + '</pre>';
  }
  showRawTab(tabNames[0]);
  document.querySelectorAll("#raw-tabs .tab-btn").forEach(function(btn) {
    btn.addEventListener("click", function() {
      document.querySelectorAll("#raw-tabs .tab-btn").forEach(function(b) { b.classList.remove("active"); });
      btn.classList.add("active");
      showRawTab(btn.dataset.tab);
    });
  });

  // ── Init ───────────────────────────────────────────────────────────────
  checkApiStatus();
})();
</script>
</body>
</html>"""
