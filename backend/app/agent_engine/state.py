"""Persistent run state and directory layout."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional

from .schemas import RunState, StageCheckpoint, StageStatus


RUN_STAGES = [
    "ontology",
    "graph",
    "profiles",
    "config",
    "simulation",
    "report",
    "artifacts",
]

STAGED_RUN_STAGES = [
    "seed_input",
    "prediction_requirement",
    "simulation_settings",
    "graph_build",
    "profile_and_config",
    "simulation_run",
    "report_generation",
    "followup_question",
]


def utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


class RunStore:
    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.requests_dir = self.run_dir / "requests"
        self.responses_dir = self.run_dir / "responses"
        self.artifacts_dir = self.run_dir / "artifacts"
        self.state_path = self.run_dir / "state.json"

    def ensure_layout(self) -> None:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def init_state(
        self,
        run_id: str,
        requirement: str,
        seed_files: Iterable[str],
        mode: str = "agent",
        workflow_mode: str = "auto",
        simulation_settings: Optional[Dict] = None,
        seed_path: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> RunState:
        self.ensure_layout()
        stage_names = STAGED_RUN_STAGES if workflow_mode == "staged" else RUN_STAGES
        stages = {name: StageCheckpoint(name=name) for name in stage_names}
        state = RunState(
            run_id=run_id,
            run_dir=str(self.run_dir),
            requirement=requirement,
            seed_files=list(seed_files),
            mode=mode,
            workflow_mode=workflow_mode,
            current_stage=stage_names[0],
            seed_path=seed_path,
            simulation_settings=simulation_settings or {},
            stages=stages,
            metadata=metadata or {},
        )
        self.save(state)
        return state

    def load(self) -> RunState:
        with self.state_path.open("r", encoding="utf-8") as handle:
            return RunState.model_validate_json(handle.read())

    def save(self, state: RunState) -> None:
        self.ensure_layout()
        state.updated_at = utc_now()
        with self.state_path.open("w", encoding="utf-8") as handle:
            handle.write(state.model_dump_json(indent=2))

    def set_stage(
        self,
        state: RunState,
        stage: str,
        status: StageStatus,
        error: Optional[str] = None,
    ) -> None:
        checkpoint = state.stages[stage]
        checkpoint.status = status
        checkpoint.error = error
        checkpoint.updated_at = utc_now()
        state.current_stage = stage
        self.save(state)

    def add_stage_request(self, state: RunState, stage: str, request_id: str) -> None:
        checkpoint = state.stages[stage]
        if request_id not in checkpoint.request_ids:
            checkpoint.request_ids.append(request_id)
        checkpoint.status = StageStatus.WAITING_AGENT
        checkpoint.updated_at = utc_now()
        state.current_stage = stage
        self.save(state)

    def add_stage_artifact(self, state: RunState, stage: str, artifact_path: str) -> None:
        checkpoint = state.stages.get(stage) or state.stages[state.current_stage]
        if artifact_path not in checkpoint.artifact_paths:
            checkpoint.artifact_paths.append(artifact_path)
        checkpoint.updated_at = utc_now()
        self.save(state)

    def next_request_id(self) -> str:
        self.ensure_layout()
        max_seen = 0
        for folder in (self.requests_dir, self.responses_dir):
            for path in folder.glob("req_*.json"):
                try:
                    max_seen = max(max_seen, int(path.stem.split("_", 1)[1]))
                except (IndexError, ValueError):
                    continue
        return f"req_{max_seen + 1:06d}"

    def read_seed_text(self, max_chars: int = 200_000) -> str:
        state = self.load()
        parts = []
        for seed_file in state.seed_files:
            path = Path(seed_file)
            if not path.is_absolute():
                path = self.run_dir / path
            if path.exists():
                parts.append(path.read_text(encoding="utf-8")[:max_chars])
        return "\n\n".join(parts)

    def as_status(self) -> Dict:
        state = self.load()
        return json.loads(state.model_dump_json())


def default_runs_dir() -> str:
    return os.environ.get("MIROFISH_RUNS_DIR", "./runs")
