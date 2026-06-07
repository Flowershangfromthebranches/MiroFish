"""FastMCP server exposing MiroFish run lifecycle tools."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..agent_engine.runner import PredictionRunService


def create_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "MCP Python SDK is not installed. Install package 'mcp' to run the MiroFish MCP server."
        ) from exc

    mcp = FastMCP("mirofish-agent-engine")
    service = PredictionRunService()

    @mcp.tool()
    def mirofish_create_run(
        seed: str,
        requirement: str,
        output: str,
        mode: str = "auto",
        rounds: int = 10,
        round_unit: str = "year",
        minutes_per_round: Optional[int] = None,
        pause_each_round: bool = False,
        agent_count: Optional[int] = None,
        simulation_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        return service.create_run(
            seed,
            requirement,
            output,
            mode=mode,
            rounds=rounds,
            round_unit=round_unit,
            minutes_per_round=minutes_per_round,
            pause_each_round=pause_each_round,
            agent_count=agent_count,
            simulation_name=simulation_name,
        )

    @mcp.tool()
    def mirofish_run(run: str) -> Dict[str, Any]:
        return service.run(run)

    @mcp.tool()
    def mirofish_resume_run(run: str) -> Dict[str, Any]:
        return service.resume(run)

    @mcp.tool()
    def mirofish_get_status(run: str) -> Dict[str, Any]:
        return service.status(run)

    @mcp.tool()
    def mirofish_get_current_stage(run: str) -> Dict[str, Any]:
        return service.get_current_stage(run)

    @mcp.tool()
    def mirofish_update_simulation_settings(
        run: str,
        rounds: Optional[int] = None,
        round_unit: Optional[str] = None,
        minutes_per_round: Optional[int] = None,
        pause_each_round: Optional[bool] = None,
        agent_count: Optional[int] = None,
        simulation_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        return service.update_simulation_settings(
            run,
            rounds=rounds,
            round_unit=round_unit,
            minutes_per_round=minutes_per_round,
            pause_each_round=pause_each_round,
            agent_count=agent_count,
            simulation_name=simulation_name,
        )

    @mcp.tool()
    def mirofish_approve_stage(run: str) -> Dict[str, Any]:
        return service.approve_stage(run)

    @mcp.tool()
    def mirofish_reject_stage(run: str, reason: str = "") -> Dict[str, Any]:
        return service.reject_stage(run, reason)

    @mcp.tool()
    def mirofish_rerun_stage(run: str, stage: str) -> Dict[str, Any]:
        return service.rerun_stage(run, stage)

    @mcp.tool()
    def mirofish_list_requests(run: str) -> Dict[str, Any]:
        return service.list_requests(run)

    @mcp.tool()
    def mirofish_get_request(run: str, request_id: str) -> Dict[str, Any]:
        return service.get_request(run, request_id)

    @mcp.tool()
    def mirofish_submit_response(run: str, response: str) -> Dict[str, Any]:
        return service.submit_response(run, response)

    @mcp.tool()
    def mirofish_validate_response(run: str, response: str) -> Dict[str, Any]:
        return service.validate_response(run, response)

    @mcp.tool()
    def mirofish_build_graph(run: str, provider: Optional[str] = None, mode: str = "agent-triples") -> Dict[str, Any]:
        return service.build_graph(run, provider=provider, mode=mode)

    @mcp.tool()
    def mirofish_search_graph(run: str, query: str, limit: int = 20) -> Dict[str, Any]:
        return service.search_graph(run, query, limit)

    @mcp.tool()
    def mirofish_export_graph(run: str, output: Optional[str] = None) -> Dict[str, Any]:
        return service.export_graph(run, output)

    @mcp.tool()
    def mirofish_start_simulation(run: str) -> Dict[str, Any]:
        return service.start_simulation(run)

    @mcp.tool()
    def mirofish_resume_simulation(run: str) -> Dict[str, Any]:
        return service.resume(run)

    @mcp.tool()
    def mirofish_generate_report(run: str) -> Dict[str, Any]:
        return service.generate_report(run)

    @mcp.tool()
    def mirofish_get_report(run: str) -> Dict[str, Any]:
        return service.get_report(run)

    @mcp.tool()
    def mirofish_ask_followup_question(run: str, question: str, limit: int = 20) -> Dict[str, Any]:
        return service.ask_followup_question(run, question, limit)

    @mcp.tool()
    def mirofish_get_followup_answer(run: str, request_id: str) -> Dict[str, Any]:
        return service.get_followup_answer(run, request_id)

    @mcp.tool()
    def mirofish_list_artifacts(run: str) -> Dict[str, Any]:
        return service.list_artifacts(run)

    @mcp.tool()
    def mirofish_doctor(runs_dir: Optional[str] = None) -> Dict[str, Any]:
        return service.doctor(runs_dir)

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
