"""Agent-friendly MiroFish CLI."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from .runner import PredictionRunService


def emit(result: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    status = result.get("status")
    if status == "need_agent_response":
        print(f"need_agent_response: {result['request_id']}")
        print(f"request_file: {result['request_file']}")
        print(f"expected_response_file: {result['expected_response_file']}")
    elif status == "created":
        print(f"created run: {result['run_id']}")
        print(f"run_dir: {result['run_dir']}")
    elif status == "awaiting_user_confirmation":
        print(f"awaiting_user_confirmation: {result['stage']}")
    elif status == "ok":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif status == "completed":
        print("completed")
        print(json.dumps(result.get("artifacts", []), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output")


def add_create_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", required=True)
    parser.add_argument("--requirement", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=["auto", "staged"], default="auto")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--round-unit", choices=["year", "month", "day", "step"], default="year")
    parser.add_argument("--minutes-per-round", type=int, default=None)
    parser.add_argument("--pause-each-round", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--agent-count", type=int, default=None)
    parser.add_argument("--simulation-name", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mirofish-agent")
    add_json(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    add_json(init)
    add_create_run_args(init)

    create_run = sub.add_parser("create-run")
    add_json(create_run)
    add_create_run_args(create_run)

    run = sub.add_parser("run")
    add_json(run)
    run.add_argument("--run", required=True)

    resume = sub.add_parser("resume")
    add_json(resume)
    resume.add_argument("--run", required=True)

    status = sub.add_parser("status")
    add_json(status)
    status.add_argument("--run", required=True)

    stage = sub.add_parser("stage")
    stage_sub = stage.add_subparsers(dest="stage_command", required=True)
    stage_status = stage_sub.add_parser("status")
    add_json(stage_status)
    stage_status.add_argument("--run", required=True)
    stage_next = stage_sub.add_parser("next")
    add_json(stage_next)
    stage_next.add_argument("--run", required=True)
    stage_approve = stage_sub.add_parser("approve")
    add_json(stage_approve)
    stage_approve.add_argument("--run", required=True)
    stage_reject = stage_sub.add_parser("reject")
    add_json(stage_reject)
    stage_reject.add_argument("--run", required=True)
    stage_reject.add_argument("--reason", default="")
    stage_update = stage_sub.add_parser("update-settings")
    add_json(stage_update)
    stage_update.add_argument("--run", required=True)
    stage_update.add_argument("--rounds", type=int, default=None)
    stage_update.add_argument("--round-unit", choices=["year", "month", "day", "step"], default=None)
    stage_update.add_argument("--minutes-per-round", type=int, default=None)
    stage_update.add_argument("--pause-each-round", action=argparse.BooleanOptionalAction, default=None)
    stage_update.add_argument("--agent-count", type=int, default=None)
    stage_update.add_argument("--simulation-name", default=None)
    stage_rerun = stage_sub.add_parser("rerun")
    add_json(stage_rerun)
    stage_rerun.add_argument("--run", required=True)
    stage_rerun.add_argument("--stage", required=True)

    requests = sub.add_parser("requests")
    req_sub = requests.add_subparsers(dest="requests_command", required=True)
    req_list = req_sub.add_parser("list")
    add_json(req_list)
    req_list.add_argument("--run", required=True)
    req_show = req_sub.add_parser("show")
    add_json(req_show)
    req_show.add_argument("--run", required=True)
    req_show.add_argument("--request-id", required=True)

    responses = sub.add_parser("responses")
    resp_sub = responses.add_subparsers(dest="responses_command", required=True)
    resp_validate = resp_sub.add_parser("validate")
    add_json(resp_validate)
    resp_validate.add_argument("--run", required=True)
    resp_validate.add_argument("--response", required=True)
    resp_submit = resp_sub.add_parser("submit")
    add_json(resp_submit)
    resp_submit.add_argument("--run", required=True)
    resp_submit.add_argument("--response", required=True)

    graph = sub.add_parser("graph")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    graph_build = graph_sub.add_parser("build")
    add_json(graph_build)
    graph_build.add_argument("--run", required=True)
    graph_build.add_argument("--provider", default=None)
    graph_build.add_argument("--mode", default="agent-triples")
    graph_search = graph_sub.add_parser("search")
    add_json(graph_search)
    graph_search.add_argument("--run", required=True)
    graph_search.add_argument("--query", required=True)
    graph_search.add_argument("--limit", type=int, default=20)
    graph_export = graph_sub.add_parser("export")
    add_json(graph_export)
    graph_export.add_argument("--run", required=True)
    graph_export.add_argument("--output", default=None)

    simulate = sub.add_parser("simulate")
    sim_sub = simulate.add_subparsers(dest="simulate_command", required=True)
    sim_start = sim_sub.add_parser("start")
    add_json(sim_start)
    sim_start.add_argument("--run", required=True)
    sim_resume = sim_sub.add_parser("resume")
    add_json(sim_resume)
    sim_resume.add_argument("--run", required=True)
    sim_status = sim_sub.add_parser("status")
    add_json(sim_status)
    sim_status.add_argument("--run", required=True)

    report = sub.add_parser("report")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_generate = report_sub.add_parser("generate")
    add_json(report_generate)
    report_generate.add_argument("--run", required=True)
    report_show = report_sub.add_parser("show")
    add_json(report_show)
    report_show.add_argument("--run", required=True)

    followup = sub.add_parser("followup")
    followup_sub = followup.add_subparsers(dest="followup_command", required=True)
    followup_ask = followup_sub.add_parser("ask")
    add_json(followup_ask)
    followup_ask.add_argument("--run", required=True)
    followup_ask.add_argument("--question", required=True)
    followup_ask.add_argument("--limit", type=int, default=20)
    followup_show = followup_sub.add_parser("show")
    add_json(followup_show)
    followup_show.add_argument("--run", required=True)
    followup_show.add_argument("--request-id", required=True)

    artifacts = sub.add_parser("artifacts")
    artifacts_sub = artifacts.add_subparsers(dest="artifacts_command", required=True)
    artifacts_list = artifacts_sub.add_parser("list")
    add_json(artifacts_list)
    artifacts_list.add_argument("--run", required=True)

    doctor = sub.add_parser("doctor")
    add_json(doctor)
    doctor.add_argument("--runs-dir", default=None)

    return parser


def dispatch(args: argparse.Namespace) -> Dict[str, Any]:
    service = PredictionRunService()
    if args.command in {"init", "create-run"}:
        return service.create_run(
            args.seed,
            args.requirement,
            args.output,
            mode=args.mode,
            rounds=args.rounds,
            round_unit=args.round_unit,
            minutes_per_round=args.minutes_per_round,
            pause_each_round=args.pause_each_round,
            agent_count=args.agent_count,
            simulation_name=args.simulation_name,
        )
    if args.command == "run":
        return service.run(args.run)
    if args.command == "resume":
        return service.resume(args.run)
    if args.command == "status":
        return service.status(args.run)
    if args.command == "stage":
        if args.stage_command == "status":
            return service.get_current_stage(args.run)
        if args.stage_command == "next":
            return service.resume(args.run)
        if args.stage_command == "approve":
            return service.approve_stage(args.run)
        if args.stage_command == "reject":
            return service.reject_stage(args.run, args.reason)
        if args.stage_command == "update-settings":
            return service.update_simulation_settings(
                args.run,
                rounds=args.rounds,
                round_unit=args.round_unit,
                minutes_per_round=args.minutes_per_round,
                pause_each_round=args.pause_each_round,
                agent_count=args.agent_count,
                simulation_name=args.simulation_name,
            )
        return service.rerun_stage(args.run, args.stage)
    if args.command == "requests":
        if args.requests_command == "list":
            return service.list_requests(args.run)
        return service.get_request(args.run, args.request_id)
    if args.command == "responses":
        if args.responses_command == "validate":
            return service.validate_response(args.run, args.response)
        return service.submit_response(args.run, args.response)
    if args.command == "graph":
        if args.graph_command == "build":
            return service.build_graph(args.run, provider=args.provider, mode=args.mode)
        if args.graph_command == "search":
            return service.search_graph(args.run, args.query, args.limit)
        return service.export_graph(args.run, args.output)
    if args.command == "simulate":
        if args.simulate_command == "start":
            return service.start_simulation(args.run)
        if args.simulate_command == "resume":
            return service.resume(args.run)
        return service.simulation_status(args.run)
    if args.command == "report":
        if args.report_command == "generate":
            return service.generate_report(args.run)
        return service.get_report(args.run)
    if args.command == "followup":
        if args.followup_command == "ask":
            return service.ask_followup_question(args.run, args.question, args.limit)
        return service.get_followup_answer(args.run, args.request_id)
    if args.command == "artifacts":
        return service.list_artifacts(args.run)
    if args.command == "doctor":
        return service.doctor(args.runs_dir)
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        result = dispatch(args)
    except Exception as exc:
        result = {"status": "error", "error": str(exc), "error_type": exc.__class__.__name__}
        emit(result, as_json)
        return 1
    emit(result, as_json)
    return 0 if result.get("status") not in {"failed", "error"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
