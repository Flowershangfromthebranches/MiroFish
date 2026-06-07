# QoderWork Staged Usage

QoderWork should use the MiroFish MCP server as a staged business workflow, not as a one-shot prompt wrapper.

## Run Sequence

1. Call `mirofish_doctor`.
2. Call `mirofish_create_run` with hard simulation settings:

```json
{
  "seed": "/absolute/path/seed.md",
  "requirement": "预测未来10年全球芯片能力格局变化",
  "output": "/Users/leaf/Documents/future/MiroFish/runs/chip-2036",
  "mode": "staged",
  "rounds": 10,
  "round_unit": "year",
  "minutes_per_round": 525600,
  "pause_each_round": false,
  "agent_count": 5,
  "simulation_name": "chip-2036"
}
```

3. Call `mirofish_get_current_stage` and present the summary to the user.
4. After the user confirms, call `mirofish_approve_stage`.
5. Call `mirofish_resume_run`.
6. If the result is `need_agent_response`, read `request_file`, generate the response JSON exactly against `expected_schema`, then call `mirofish_validate_response` and `mirofish_submit_response`.
7. If the result is `awaiting_user_confirmation`, show the stage summary and ask whether to approve, reject, update settings, or rerun.
8. Continue until the run returns `completed`.
9. Read artifacts with `mirofish_get_report` and `mirofish_list_artifacts`.
10. Ask follow-up questions with `mirofish_ask_followup_question`.

## Important Rule

Do not put the round count only in the natural-language requirement. Always pass `rounds` and `round_unit` through MCP fields so the simulation config, timeline, verdict, and report record the actual hard settings.
