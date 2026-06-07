# Graphiti + Neo4j Setup

Agent mode defaults to:

```bash
MIROFISH_MODE=agent
MIROFISH_LLM_PROVIDER=agent_queue
MIROFISH_GRAPH_PROVIDER=graphiti
```

MiroFish does not vendor Graphiti source code. Install Graphiti through the backend Python dependency group:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend
uv sync --extra agent --group dev
```

You can also run the helper:

```bash
cd /Users/leaf/Documents/future/MiroFish
bash scripts/setup_agent_deps.sh --neo4j desktop
```

The helper loads `/Users/leaf/Documents/future/MiroFish/.env` automatically when it exists.

Docker is optional. `doctor` does not fail just because Docker or Docker Compose is unavailable.

## Required Environment

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=password
export NEO4J_DATABASE=neo4j
```

Neo4j must be reachable and must be version `5.26` or newer.

Graphiti stores and searches graph facts. It does not extract complex triples from raw seed text in MiroFish agent mode. MiroFish writes `extract_triples` requests, a desktop agent writes validated responses, then `GraphitiGraphProvider` stores triples using `run_id` as the namespace.

## Option 1: Neo4j Desktop

Recommended when you do not want Docker and prefer a GUI-managed local database:

1. Install Neo4j Desktop.
2. Create a local DBMS using Neo4j `5.26` or newer.
3. Set the password to match `NEO4J_PASSWORD`.
4. Start the database.
5. Export the connection values:

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=your-password
export NEO4J_DATABASE=neo4j
```

Then run:

```bash
cd /Users/leaf/Documents/future/MiroFish/backend
uv run mirofish-agent doctor --json
```

The `docker` and `docker_compose` doctor checks may show warnings, but they are optional and do not fail doctor.

## Option 2: Homebrew / Native Install

Recommended when you do not want Docker and prefer a local service managed by macOS. Install and start Neo4j locally:

```bash
brew install neo4j
brew services start neo4j
```

Set the same environment variables:

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=your-password
export NEO4J_DATABASE=neo4j
```

If your native installation uses another port, update `NEO4J_URI`.

Run:

```bash
cd /Users/leaf/Documents/future/MiroFish
bash scripts/setup_agent_deps.sh --neo4j native
cd backend
uv run mirofish-agent doctor --json
```

## Option 3: Existing Neo4j Instance

Point MiroFish at any reachable Neo4j `5.26+` instance:

```bash
export NEO4J_URI=bolt://your-host:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=your-password
export NEO4J_DATABASE=neo4j
```

Then run:

```bash
bash scripts/setup_agent_deps.sh --neo4j existing
```

## Option 4: Docker Compose

Docker Compose remains available for users who prefer it:

```bash
cd /Users/leaf/Documents/future/MiroFish
docker compose -f docker-compose.agent.yml up -d neo4j
```

Open the browser console at `http://localhost:7474` and log in with:

```text
user: neo4j
password: password
```

Run:

```bash
cd /Users/leaf/Documents/future/MiroFish
bash scripts/setup_agent_deps.sh --neo4j docker --start-docker
```

If Docker is not installed, the setup script prints `Docker optional, skipped`; it does not fail for that reason. The required check is still whether Neo4j is reachable through `NEO4J_URI`.

## Ollama Embedding

The no-LLM triplet write path and `fulltext` graph search do not require Ollama. Doctor only hard-fails Ollama checks when both conditions are true:

```bash
export MIROFISH_GRAPH_SEARCH_MODE=semantic  # or hybrid
export MIROFISH_EMBEDDING_PROVIDER=ollama
```

If `MIROFISH_GRAPH_SEARCH_MODE=fulltext` or `MIROFISH_EMBEDDING_PROVIDER=none`, missing Ollama is reported as an optional warning only. Semantic retrieval may be unavailable, but the agent engine and Graphiti/Neo4j fulltext path can still run.

If you opt into Ollama embeddings, install and start Ollama locally, then pull the embedding model:

```bash
ollama serve
ollama pull nomic-embed-text
```

Configure:

```bash
export MIROFISH_EMBEDDING_PROVIDER=ollama
export MIROFISH_GRAPH_SEARCH_MODE=semantic
export OLLAMA_BASE_URL=http://localhost:11434
export OLLAMA_EMBEDDING_MODEL=nomic-embed-text
```

Doctor checks `GET $OLLAMA_BASE_URL/api/tags` and verifies that `OLLAMA_EMBEDDING_MODEL` is installed.

## Offline Compatibility Store

Offline tests can use a file-backed no-LLM triplet store:

```bash
export MIROFISH_GRAPHITI_STORE=file
export MIROFISH_GRAPHITI_COMPAT_PATH=/tmp/mirofish-graphiti-store.json
```

This is for smoke tests and local development without Neo4j. Production agent mode should use Neo4j. The default `MIROFISH_GRAPHITI_STORE=auto` path uses Neo4j; it does not silently downgrade to file storage.

## Compatibility Layer

`GraphitiCompatibilityStore` provides the no-LLM triplet write path. If it writes directly to Neo4j, all Cypher and Graphiti schema assumptions stay inside that class. Business code must use `GraphProvider` and must not depend on Graphiti node or edge internals.

References:

- Graphiti episodes: https://help.getzep.com/graphiti/core-concepts/adding-episodes
- Graphiti fact triples: https://help.getzep.com/graphiti/working-with-data/adding-fact-triples
- Graphiti namespacing: https://help.getzep.com/graphiti/core-concepts/graph-namespacing
- Graphiti Neo4j config: https://help.getzep.com/graphiti/configuration/neo-4-j-configuration
- Graphiti LLM config: https://help.getzep.com/graphiti/configuration/llm-configuration
