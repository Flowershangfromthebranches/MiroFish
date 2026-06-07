"""Filesystem queue for external desktop agents."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .json_schema import validate_json_schema
from .schemas import AgentNeedResponse, AgentRequest, AgentResponse, ValidationResult
from .state import RunStore


class AgentQueueError(ValueError):
    pass


class AgentQueue:
    def __init__(self, run_dir: str | Path):
        self.store = RunStore(run_dir)
        self.store.ensure_layout()

    def create_request(
        self,
        *,
        run_id: str,
        task_type: str,
        stage: str,
        expected_schema: Dict[str, Any],
        input_text: Optional[str] = None,
        input_files: Optional[List[str]] = None,
        structured_input: Optional[Dict[str, Any]] = None,
        system_prompt: str = "",
        user_prompt: str = "",
        validation_rules: Optional[Dict[str, Any]] = None,
        retry_policy: Optional[Dict[str, Any]] = None,
        context_refs: Optional[List[str]] = None,
        output_contract: Optional[Dict[str, Any]] = None,
    ) -> AgentNeedResponse:
        request_id = self.store.next_request_id()
        request = AgentRequest(
            request_id=request_id,
            run_id=run_id,
            type=task_type,
            stage=stage,
            input_text=input_text,
            input_files=input_files or [],
            structured_input=structured_input or {},
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            expected_schema=expected_schema,
            validation_rules=validation_rules or {},
            retry_policy=retry_policy or {},
            context_refs=context_refs or [],
            output_contract=output_contract or {},
        )
        request_file = self.store.requests_dir / f"{request_id}.json"
        request_file.write_text(request.model_dump_json(indent=2), encoding="utf-8")
        return AgentNeedResponse(
            request_id=request_id,
            request_file=str(request_file),
            expected_response_file=str(self.store.responses_dir / f"{request_id}.json"),
            stage=stage,
            type=task_type,
        )

    def list_requests(self) -> List[Dict[str, Any]]:
        requests = []
        for path in sorted(self.store.requests_dir.glob("req_*.json")):
            request = self.load_request(path.stem)
            response_path = self.store.responses_dir / path.name
            requests.append(
                {
                    "request_id": request.request_id,
                    "type": request.type,
                    "stage": request.stage,
                    "request_file": str(path),
                    "expected_response_file": str(response_path),
                    "has_response": response_path.exists(),
                }
            )
        return requests

    def load_request(self, request_id: str) -> AgentRequest:
        path = self.store.requests_dir / f"{request_id}.json"
        if not path.exists():
            raise AgentQueueError(f"request not found: {request_id}")
        return AgentRequest.model_validate_json(path.read_text(encoding="utf-8"))

    def save_request(self, request: AgentRequest) -> None:
        path = self.store.requests_dir / f"{request.request_id}.json"
        path.write_text(request.model_dump_json(indent=2), encoding="utf-8")

    def load_response(self, request_id: str) -> AgentResponse:
        path = self.store.responses_dir / f"{request_id}.json"
        if not path.exists():
            raise AgentQueueError(f"response not found: {request_id}")
        return AgentResponse.model_validate_json(path.read_text(encoding="utf-8"))

    def validate_response_file(
        self,
        response_path: str | Path,
        *,
        request_id: Optional[str] = None,
    ) -> ValidationResult:
        errors: List[str] = []
        path = Path(response_path)
        try:
            response = AgentResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ValidationResult(ok=False, errors=[f"response file not found: {path}"])
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            return ValidationResult(ok=False, errors=[f"invalid response schema: {exc}"])

        if request_id and response.request_id != request_id:
            errors.append(f"response request_id {response.request_id} does not match {request_id}")
        if path.stem.startswith("req_") and response.request_id != path.stem:
            errors.append(f"response request_id {response.request_id} does not match response file name {path.name}")

        try:
            request = self.load_request(response.request_id)
        except AgentQueueError as exc:
            errors.append(str(exc))
            return ValidationResult(ok=False, errors=errors)

        if response.status in {"ok", "skipped"}:
            errors.extend(validate_json_schema(response.output, request.expected_schema))
        elif response.status == "error" and not response.error:
            errors.append("error response must include error")

        return ValidationResult(ok=not errors, errors=errors)

    def submit_response(self, response_path: str | Path) -> ValidationResult:
        result = self.validate_response_file(response_path)
        if not result.ok:
            repair = self._maybe_create_repair_request(response_path, result.errors)
            result.repair_request = repair
            return result

        source = Path(response_path)
        response = AgentResponse.model_validate_json(source.read_text(encoding="utf-8"))
        destination = self.store.responses_dir / f"{response.request_id}.json"
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        return result

    def _maybe_create_repair_request(
        self,
        response_path: str | Path,
        errors: List[str],
    ) -> Optional[AgentNeedResponse]:
        try:
            raw = json.loads(Path(response_path).read_text(encoding="utf-8"))
            request_id = raw.get("request_id")
            request = self.load_request(request_id)
        except Exception:
            return None

        policy = request.retry_policy
        if policy.repair_attempts_used >= policy.max_repair_attempts:
            return None

        policy.repair_attempts_used += 1
        request.retry_policy = policy
        self.save_request(request)
        return self.create_request(
            run_id=request.run_id,
            task_type="repair_invalid_json",
            stage=request.stage,
            expected_schema=request.expected_schema,
            structured_input={
                "original_request": request.model_dump(),
                "invalid_response": raw,
                "validation_errors": errors,
            },
            system_prompt="Repair the invalid agent output so it strictly matches expected_schema.",
            user_prompt="Return only the repaired output object. Do not wrap it in an AgentResponse envelope.",
            retry_policy=policy.model_dump(),
            output_contract={"repairs_request_id": request.request_id, "return_shape": "output_only"},
        )
