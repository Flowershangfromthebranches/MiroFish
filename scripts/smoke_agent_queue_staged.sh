#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
TMPDIR="$(mktemp -d)"
RUN_DIR="$TMPDIR/staged-chip-2036"
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
uv run mirofish-agent create-run \
  --seed "$SEED" \
  --requirement "预测未来10年全球芯片能力格局变化" \
  --output "$RUN_DIR" \
  --mode staged \
  --rounds 10 \
  --round-unit year \
  --json >/tmp/mirofish_staged_init.json

for _ in $(seq 1 80); do
  uv run mirofish-agent resume --run "$RUN_DIR" --json > /tmp/mirofish_staged_resume.json
  STATUS="$(python -c 'import json; print(json.load(open("/tmp/mirofish_staged_resume.json"))["status"])')"
  if [ "$STATUS" = "completed" ]; then
    test -f "$RUN_DIR/artifacts/report.md"
    test -f "$RUN_DIR/artifacts/verdict.json"
    test -f "$RUN_DIR/artifacts/timeline.json"
    test -f "$RUN_DIR/artifacts/graph_snapshot.json"
    python - "$RUN_DIR" <<'PY'
import json, sys
run = sys.argv[1]
verdict = json.load(open(f"{run}/artifacts/verdict.json"))
timeline = json.load(open(f"{run}/artifacts/timeline.json"))
assert verdict["rounds"] == 10, verdict
assert verdict["simulation_settings"]["round_unit"] == "year", verdict
assert len(timeline) == 10, timeline
PY
    for expected_type in generate_ontology extract_triples generate_oasis_profiles generate_simulation_config simulate_agent_action generate_report; do
      if ! grep -qx "$expected_type" "$OBSERVED_TYPES"; then
        echo "missing expected staged request type: $expected_type"
        cat "$OBSERVED_TYPES" || true
        exit 1
      fi
    done
    echo "CLI staged agent_queue smoke passed: $RUN_DIR"
    exit 0
  fi
  if [ "$STATUS" = "awaiting_user_confirmation" ]; then
    uv run mirofish-agent stage approve --run "$RUN_DIR" --json >/tmp/mirofish_staged_approve.json
    continue
  fi
  if [ "$STATUS" != "need_agent_response" ]; then
    cat /tmp/mirofish_staged_resume.json
    exit 1
  fi
  REQUEST_ID="$(python -c 'import json; print(json.load(open("/tmp/mirofish_staged_resume.json"))["request_id"])')"
  REQUEST_FILE="$(python -c 'import json; print(json.load(open("/tmp/mirofish_staged_resume.json"))["request_file"])')"
  python -c 'import json,sys; print(json.load(open(sys.argv[1]))["type"])' "$REQUEST_FILE" >> "$OBSERVED_TYPES"
  RESPONSE="$(python "$ROOT/scripts/write_mock_agent_response.py" --run "$RUN_DIR" --request-id "$REQUEST_ID")"
  uv run mirofish-agent responses validate --run "$RUN_DIR" --response "$RESPONSE" --json >/tmp/mirofish_staged_validate.json
  python -c 'import json,sys; data=json.load(open("/tmp/mirofish_staged_validate.json")); sys.exit(0 if data["ok"] else 1)'
done

echo "CLI staged smoke did not complete within expected steps"
exit 1
