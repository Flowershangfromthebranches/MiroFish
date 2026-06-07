#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
TMPDIR="$(mktemp -d)"
RUN_DIR="$TMPDIR/chip-2036"
SEED="$TMPDIR/seed.md"
OBSERVED_TYPES="$TMPDIR/observed_request_types.txt"

export MIROFISH_MODE=agent
export MIROFISH_LLM_PROVIDER=agent_queue
export MIROFISH_GRAPH_PROVIDER=graphiti
export MIROFISH_GRAPHITI_STORE=file
export MIROFISH_GRAPHITI_COMPAT_PATH="$TMPDIR/graphiti-store.json"
export MIROFISH_RUNS_DIR="$TMPDIR/runs"
unset LLM_API_KEY
unset OPENAI_API_KEY
unset ZEP_API_KEY

printf '%s\n' '美国商务部限制先进AI芯片出口。' > "$SEED"

cd "$BACKEND"
uv run mirofish-agent init --seed "$SEED" --requirement "预测未来10年全球芯片能力格局变化" --output "$RUN_DIR" --json >/tmp/mirofish_smoke_init.json

for _ in 1 2 3 4 5 6 7 8 9 10; do
  uv run mirofish-agent resume --run "$RUN_DIR" --json > /tmp/mirofish_smoke_resume.json
  STATUS="$(python -c 'import json; print(json.load(open("/tmp/mirofish_smoke_resume.json"))["status"])')"
  if [ "$STATUS" = "completed" ]; then
    test -f "$RUN_DIR/artifacts/report.md"
    test -f "$RUN_DIR/artifacts/verdict.json"
    test -f "$RUN_DIR/artifacts/timeline.json"
    test -f "$RUN_DIR/artifacts/graph_snapshot.json"
    uv run mirofish-agent followup ask --run "$RUN_DIR" --question "先进AI芯片出口限制有什么影响?" --json > /tmp/mirofish_smoke_followup.json
    FOLLOWUP_STATUS="$(python -c 'import json; print(json.load(open("/tmp/mirofish_smoke_followup.json"))["status"])')"
    test "$FOLLOWUP_STATUS" = "need_agent_response"
    FOLLOWUP_REQUEST_ID="$(python -c 'import json; print(json.load(open("/tmp/mirofish_smoke_followup.json"))["request_id"])')"
    FOLLOWUP_RESPONSE="$(python "$ROOT/scripts/write_mock_agent_response.py" --run "$RUN_DIR" --request-id "$FOLLOWUP_REQUEST_ID")"
    uv run mirofish-agent responses validate --run "$RUN_DIR" --response "$FOLLOWUP_RESPONSE" --json >/tmp/mirofish_smoke_followup_validate.json
    python -c 'import json,sys; data=json.load(open("/tmp/mirofish_smoke_followup_validate.json")); sys.exit(0 if data["ok"] else 1)'
    uv run mirofish-agent followup show --run "$RUN_DIR" --request-id "$FOLLOWUP_REQUEST_ID" --json >/tmp/mirofish_smoke_followup_show.json
    python -c 'import json,sys; data=json.load(open("/tmp/mirofish_smoke_followup_show.json")); sys.exit(0 if data["status"] == "ok" else 1)'
    test -f "$RUN_DIR/artifacts/followups/$FOLLOWUP_REQUEST_ID.md"
    for expected_type in generate_ontology extract_triples generate_oasis_profiles generate_simulation_config simulate_agent_action generate_report; do
      if ! grep -qx "$expected_type" "$OBSERVED_TYPES"; then
        echo "missing expected agent_queue request type: $expected_type"
        cat "$OBSERVED_TYPES" || true
        exit 1
      fi
    done
    echo "CLI full agent_queue smoke passed: $RUN_DIR"
    exit 0
  fi
  if [ "$STATUS" != "need_agent_response" ]; then
    cat /tmp/mirofish_smoke_resume.json
    exit 1
  fi
  REQUEST_ID="$(python -c 'import json; print(json.load(open("/tmp/mirofish_smoke_resume.json"))["request_id"])')"
  REQUEST_FILE="$(python -c 'import json; print(json.load(open("/tmp/mirofish_smoke_resume.json"))["request_file"])')"
  python -c 'import json,sys; print(json.load(open(sys.argv[1]))["type"])' "$REQUEST_FILE" >> "$OBSERVED_TYPES"
  RESPONSE="$(python "$ROOT/scripts/write_mock_agent_response.py" --run "$RUN_DIR" --request-id "$REQUEST_ID")"
  uv run mirofish-agent responses validate --run "$RUN_DIR" --response "$RESPONSE" --json >/tmp/mirofish_smoke_validate.json
  python -c 'import json,sys; data=json.load(open("/tmp/mirofish_smoke_validate.json")); sys.exit(0 if data["ok"] else 1)'
done

echo "CLI smoke did not complete within expected steps"
exit 1
