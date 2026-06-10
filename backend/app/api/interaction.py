"""
Agent interaction API routes.

Provides REST endpoints for the Web Console to interact with agents,
send questionnaires, ask report questions, and poll request status.
All LLM work goes through AgentRuntime / agent_queue — never direct model calls.
"""

import json
import os
from pathlib import Path
from flask import Blueprint, request, jsonify

from . import interaction_bp
from ..agent_engine.runner import PredictionRunService
from ..utils.logger import get_logger

logger = get_logger("mirofish.api.interaction")


def _service():
    return PredictionRunService()


def _run_dir_from_param():
    """Resolve run_dir from query param ?run= or JSON body run field."""
    run = request.args.get("run") or ""
    if not run:
        body = request.get_json(silent=True) or {}
        run = body.get("run", "")
    if not run:
        return None, (jsonify({"success": False, "error": "missing required 'run' parameter"}), 400)
    return run, None


# ── Agents ────────────────────────────────────────────────────────────────

@interaction_bp.route("/agents", methods=["GET"])
def list_agents():
    run, err = _run_dir_from_param()
    if err:
        return err
    return jsonify({"success": True, "data": _service().list_agents(run)})


@interaction_bp.route("/agents/<agent_id>", methods=["GET"])
def get_agent(agent_id: str):
    run, err = _run_dir_from_param()
    if err:
        return err
    return jsonify({"success": True, "data": _service().get_agent(run, agent_id)})


@interaction_bp.route("/agents/<agent_id>/ask", methods=["POST"])
def ask_agent(agent_id: str):
    run, err = _run_dir_from_param()
    if err:
        return err
    body = request.get_json() or {}
    question = body.get("question", "")
    if not question:
        return jsonify({"success": False, "error": "missing required 'question' field"}), 400
    limit = body.get("limit", 20)
    result = _service().ask_agent(run, agent_id, question, limit)
    status_code = 202 if result.get("status") == "need_agent_response" else 200
    return jsonify({"success": True, "data": result}), status_code


@interaction_bp.route("/agents/answer/<request_id>", methods=["POST", "GET"])
def get_agent_answer(request_id: str):
    run, err = _run_dir_from_param()
    if err:
        return err
    return jsonify({"success": True, "data": _service().get_agent_answer(run, request_id)})


# ── Questionnaires ────────────────────────────────────────────────────────

@interaction_bp.route("/questionnaires", methods=["POST"])
def send_questionnaire():
    run, err = _run_dir_from_param()
    if err:
        return err
    body = request.get_json() or {}
    questions = body.get("questions", [])
    if not questions:
        return jsonify({"success": False, "error": "missing required 'questions' array"}), 400
    result = _service().send_questionnaire(run, questions)
    status_code = 202 if result.get("status") == "need_agent_response" else 200
    return jsonify({"success": True, "data": result}), status_code


@interaction_bp.route("/questionnaires/<questionnaire_id>", methods=["GET"])
def get_questionnaire_result(questionnaire_id: str):
    run, err = _run_dir_from_param()
    if err:
        return err
    return jsonify({"success": True, "data": _service().get_questionnaire_result(run, questionnaire_id)})


# ── Report Questions ──────────────────────────────────────────────────────

@interaction_bp.route("/report-questions", methods=["POST"])
def ask_report_question():
    run, err = _run_dir_from_param()
    if err:
        return err
    body = request.get_json() or {}
    question = body.get("question", "")
    if not question:
        return jsonify({"success": False, "error": "missing required 'question' field"}), 400
    limit = body.get("limit", 20)
    result = _service().ask_report_question(run, question, limit)
    status_code = 202 if result.get("status") == "need_agent_response" else 200
    return jsonify({"success": True, "data": result}), status_code


@interaction_bp.route("/report-questions/answer/<request_id>", methods=["POST", "GET"])
def get_report_question_answer(request_id: str):
    run, err = _run_dir_from_param()
    if err:
        return err
    return jsonify({"success": True, "data": _service().get_report_question_answer(run, request_id)})


# ── Request Polling & Response Submission ─────────────────────────────────

@interaction_bp.route("/requests/<request_id>", methods=["GET"])
def get_request(request_id: str):
    run, err = _run_dir_from_param()
    if err:
        return err
    return jsonify({"success": True, "data": _service().get_request(run, request_id)})


@interaction_bp.route("/requests/<request_id>/status", methods=["GET"])
def get_request_status(request_id: str):
    """Lightweight polling endpoint: returns whether a response exists yet."""
    run, err = _run_dir_from_param()
    if err:
        return err
    svc = _service()
    req_data = svc.get_request(run, request_id)
    if req_data.get("status") == "error":
        return jsonify({"success": False, "error": req_data.get("error")}), 404
    # Check if response file exists
    from ..agent_engine.state import RunStore
    store = RunStore(run)
    response_path = store.responses_dir / f"{request_id}.json"
    has_response = response_path.exists()
    return jsonify({
        "success": True,
        "data": {
            "request_id": request_id,
            "has_response": has_response,
            "status": "answered" if has_response else "pending",
        },
    })


@interaction_bp.route("/responses", methods=["POST"])
def submit_response():
    """Submit an agent response (validate + persist)."""
    run, err = _run_dir_from_param()
    if err:
        return err
    body = request.get_json() or {}
    response_path = body.get("response_path", "")
    if not response_path:
        return jsonify({"success": False, "error": "missing required 'response_path' field"}), 400
    # Path constraint: response_path must resolve inside run/responses/
    from ..agent_engine.state import RunStore
    store = RunStore(run)
    allowed_base = store.responses_dir.resolve()
    resolved = Path(response_path).resolve()
    if not str(resolved).startswith(str(allowed_base) + os.sep) and resolved != allowed_base:
        return jsonify({"success": False, "error": "forbidden: response_path must be inside run responses directory"}), 403
    result = _service().submit_response(run, response_path)
    return jsonify({"success": True, "data": result})


# ── Artifacts ─────────────────────────────────────────────────────────────

@interaction_bp.route("/artifacts", methods=["GET"])
def list_artifacts():
    run, err = _run_dir_from_param()
    if err:
        return err
    return jsonify({"success": True, "data": _service().list_artifacts(run)})


@interaction_bp.route("/artifact/<path:name>", methods=["GET"])
def get_artifact(name: str):
    """Return a single artifact's content as JSON or text."""
    run, err = _run_dir_from_param()
    if err:
        return err
    from ..agent_engine.state import RunStore
    store = RunStore(run)
    base = store.artifacts_dir.resolve()
    path = (base / name).resolve()
    # Path traversal guard: resolved path must stay inside artifacts_dir
    if not str(path).startswith(str(base) + os.sep) and path != base:
        return jsonify({"success": False, "error": "forbidden path"}), 403
    if not path.exists():
        return jsonify({"success": False, "error": f"artifact not found: {name}"}), 404
    if name.endswith(".json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return jsonify({"success": True, "data": data, "name": name})
        except json.JSONDecodeError:
            return jsonify({"success": False, "error": "invalid JSON"}), 500
    else:
        return jsonify({"success": True, "data": path.read_text(encoding="utf-8"), "name": name})
