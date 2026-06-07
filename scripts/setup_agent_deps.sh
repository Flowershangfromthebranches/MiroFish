#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
NEO4J_MODE="existing"
START_DOCKER="prompt"
FAILURES=0

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.env"
  set +a
fi

usage() {
  cat <<'EOF'
Usage: scripts/setup_agent_deps.sh [options]

Options:
  --neo4j desktop|native|docker|existing
      Choose the Neo4j setup path to explain/check. Default: existing.
      desktop  = Neo4j Desktop-managed local database.
      native   = Homebrew or other local host installation.
      docker   = optional Docker Compose path.
      existing = already-running local or remote Neo4j instance.

  --start-docker
      Start the optional Docker Compose Neo4j service if Docker is available.

  --skip-services
      Do not try to start Docker services. Still checks Neo4j connectivity.

  --start-services
      Backward-compatible alias for --start-docker.

Installs MiroFish agent-mode Python dependencies and checks required local
services. Graphiti is installed as a Python dependency; its source is not
vendored into this repository. Docker is optional.
EOF
}

ok() {
  printf '[ok] %s\n' "$1"
}

warn() {
  printf '[warn] %s\n' "$1"
}

fail() {
  printf '[fail] %s\n' "$1"
  FAILURES=$((FAILURES + 1))
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --neo4j)
      shift
      if [ "$#" -eq 0 ]; then
        fail "--neo4j requires one of: desktop, native, docker, existing"
        usage
        exit 2
      fi
      case "$1" in
        desktop|native|docker|existing)
          NEO4J_MODE="$1"
          ;;
        *)
          fail "unsupported --neo4j mode: $1"
          usage
          exit 2
          ;;
      esac
      ;;
    --start-docker|--start-services)
      START_DOCKER="yes"
      ;;
    --skip-services)
      START_DOCKER="no"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      usage
      exit 2
      ;;
  esac
  shift
done

install_agent_deps() {
  if command -v uv >/dev/null 2>&1; then
    ok "installing backend agent extras with uv"
    if (cd "$BACKEND_DIR" && uv sync --extra agent --group dev); then
      ok "Python agent dependencies installed"
    else
      fail "uv sync --extra agent --group dev failed"
    fi
  else
    warn "uv is not installed; install dependencies manually from backend:"
    warn "python -m pip install -e '.[agent]'"
  fi
}

explain_neo4j_mode() {
  case "$NEO4J_MODE" in
    desktop)
      warn "Neo4j Desktop path selected"
      warn "Create/start a Neo4j 5.26+ DBMS in Neo4j Desktop, then export NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD/NEO4J_DATABASE."
      ;;
    native)
      warn "Native Neo4j path selected"
      if command -v brew >/dev/null 2>&1; then
        warn "Homebrew detected. Typical install/start: brew install neo4j && brew services start neo4j"
      else
        warn "Homebrew not detected. Install Neo4j 5.26+ with your local package manager and start it on NEO4J_URI."
      fi
      ;;
    docker)
      warn "Optional Docker Compose Neo4j path selected"
      warn "Compose file: docker-compose.agent.yml"
      ;;
    existing)
      warn "Existing Neo4j path selected"
      warn "Set NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD/NEO4J_DATABASE for a reachable Neo4j 5.26+ instance."
      ;;
  esac
}

check_docker_optional() {
  if command -v docker >/dev/null 2>&1; then
    ok "$(docker --version)"
  else
    warn "Docker optional, skipped"
    return 1
  fi

  if docker compose version >/dev/null 2>&1; then
    ok "$(docker compose version)"
  else
    warn "Docker Compose optional, skipped"
    return 1
  fi
  return 0
}

maybe_start_docker_neo4j() {
  if [ "$NEO4J_MODE" != "docker" ]; then
    return
  fi

  if ! check_docker_optional; then
    return
  fi

  if [ "$START_DOCKER" = "prompt" ]; then
    if [ -t 0 ]; then
      printf 'Start optional Neo4j Docker Compose service now? [y/N] '
      read -r answer
      case "$answer" in
        y|Y|yes|YES)
          START_DOCKER="yes"
          ;;
        *)
          START_DOCKER="no"
          ;;
      esac
    else
      START_DOCKER="no"
      warn "non-interactive shell; skipping optional Docker startup prompt"
    fi
  fi

  if [ "$START_DOCKER" = "yes" ]; then
    if docker compose -f "$ROOT_DIR/docker-compose.agent.yml" up -d neo4j; then
      ok "optional Neo4j compose service requested"
    else
      warn "optional Docker Compose startup failed; Neo4j connectivity check will report actual readiness"
    fi
  else
    warn "optional Docker startup skipped"
    warn "manual Docker command: docker compose -f docker-compose.agent.yml up -d neo4j"
  fi
}

run_backend_python() {
  if command -v uv >/dev/null 2>&1; then
    (cd "$BACKEND_DIR" && uv run python "$@")
  else
    python3 "$@"
  fi
}

check_neo4j() {
  run_backend_python - <<'PY'
import os
import sys

uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
user = os.environ.get("NEO4J_USER", "neo4j")
password = os.environ.get("NEO4J_PASSWORD", "password")
database = os.environ.get("NEO4J_DATABASE", "neo4j")

try:
    from neo4j import GraphDatabase
except Exception as exc:
    print(f"[fail] neo4j Python driver import failed: {exc}")
    sys.exit(1)


def parse_version(value):
    cleaned = value.split("-", 1)[0]
    parts = cleaned.split(".")
    major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return major, minor


try:
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session(database=database) as session:
        record = session.run(
            "CALL dbms.components() YIELD name, versions RETURN name, versions LIMIT 1"
        ).single()
    driver.close()
except Exception as exc:
    print(f"[fail] Neo4j connection/version check failed for {uri}: {exc}")
    sys.exit(1)

versions = record["versions"] if record else []
version = versions[0] if versions else "unknown"
major, minor = parse_version(version)
if not (major > 5 or (major == 5 and minor >= 26)):
    print(f"[fail] Neo4j version {version} is unsupported; use Neo4j 5.26+")
    sys.exit(1)

print(f"[ok] Neo4j version {version}")
PY
  status=$?
  if [ "$status" -ne 0 ]; then
    FAILURES=$((FAILURES + 1))
  fi
}

check_ollama_if_configured() {
  provider="${MIROFISH_EMBEDDING_PROVIDER:-none}"
  search_mode="${MIROFISH_GRAPH_SEARCH_MODE:-fulltext}"
  if [ "$search_mode" != "semantic" ] && [ "$search_mode" != "hybrid" ]; then
    warn "Ollama optional, skipped because MIROFISH_GRAPH_SEARCH_MODE=$search_mode uses no semantic embedding"
    return
  fi
  if [ "$provider" != "ollama" ]; then
    warn "Ollama optional, skipped because MIROFISH_EMBEDDING_PROVIDER=$provider; semantic retrieval may be degraded"
    return
  fi

  run_backend_python - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
model = os.environ.get("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
url = f"{base_url}/api/tags"

try:
    with urllib.request.urlopen(url, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
    print(f"[fail] Ollama tags check failed for {url}: {exc}")
    print(f"[hint] start Ollama and run: ollama pull {model}")
    sys.exit(1)

names = [item.get("name", "") for item in payload.get("models", [])]
found = any(name == model or name.startswith(model + ":") for name in names)
if not found:
    print(f"[fail] Ollama embedding model '{model}' not found")
    print(f"[hint] run: ollama pull {model}")
    sys.exit(1)

print(f"[ok] Ollama embedding model available: {model}")
PY
  status=$?
  if [ "$status" -ne 0 ]; then
    FAILURES=$((FAILURES + 1))
  fi
}

install_agent_deps
explain_neo4j_mode
if [ "$NEO4J_MODE" != "docker" ]; then
  check_docker_optional || true
fi
maybe_start_docker_neo4j
check_neo4j
check_ollama_if_configured

if [ "$FAILURES" -gt 0 ]; then
  printf '[fail] setup completed with %s required failure(s)\n' "$FAILURES"
  exit 1
fi

ok "agent dependency and required service checks completed"
