# Context Engine MCP Integration

Context Engine's MCP server is a persistent Streamable HTTP service over the
existing REST API.
The Context Engine Agent remains the junior engineer and retains planning,
repository exploration, memory search, verification, repair passes, and evidence
collection.

```text
Claude / Codex / GPT
        |
        v
context-engine MCP tools
        |
        v
existing REST endpoints
        |
        v
existing Context Engine Agent and repository tools
```

## Project Structure

```text
context-engine/
  mcp_server.py                 # shared REST mapping and polling only
  mcp_http_server.py            # Streamable HTTP transport on :8089/mcp
  requirements-mcp.txt          # isolated MCP runtime dependencies
  app/
    main.py                     # REST endpoints and live /setup guide
    agent_runner.py             # existing junior agent loop
    tool_registry.py            # existing internal repository tools
tests/
  test_mcp_server.py            # adapter schemas, routing, polling, and errors
  test_mcp_http_server.py       # persistent transport configuration and tools
config/
  claude-code.mcp.json.example
  codex.config.toml.example
docs/
  mcp-integration.md
```

## Tool Selection

Primary tools:

| Tool | REST workflow | Use |
|---|---|---|
| `investigate_codebase` | `POST /agents/run`, then status polling | Default for repository investigation |
| `load_context` | `POST /context` | Bounded pre-task context |
| `review_diff` | `POST /diff-summary` | Risk review after edits |
| `audit_issue` | Issue-auditor start, then status polling | Evidence-based issue audit |

Advanced direct tools:

| Tool | REST endpoint |
|---|---|
| `scan_directory` | `POST /scan` |
| `find_in_code` | `POST /find` |
| `summarize_file` | `POST /summarize` |
| `dependency_analysis` | `POST /dependencies` |
| `vector_search` | `POST /vector-search` |

Prefer `investigate_codebase` over manually chaining advanced tools. Advanced
tools are for bounded primitive retrieval when the caller already knows the
single operation it needs.

Discovery endpoints are reference-first by default. `/scan`, `/find`,
`/routes`, `/dependencies`, and `/vector-search` default to `detail="summary"`
and return compact references plus `metadata` (`result_count`,
`payload_bytes`, `estimated_tokens`, `truncated`, `detail_level`). Use
`mode="context_safe"` for the smallest Claude-safe payload, `detail="standard"`
for richer summaries, and `detail="full"` only for legacy inline payloads.
Fetch file content deliberately with `POST /read`.

The Streamable HTTP MCP tools expose the same discovery controls: `detail`,
`mode`, `max_results`, and `max_chars` where applicable.

## Claude Code

Install or restart the service:

```bash
bash scripts/install-mcp.sh
```

Register its URL:

```bash
claude mcp add --scope user --transport http \
  context-engine http://127.0.0.1:8089/mcp
```

For project-scoped configuration, adapt
[`config/claude-code.mcp.json.example`](../config/claude-code.mcp.json.example)
as `.mcp.json`.

Verify:

```bash
claude mcp get context-engine
```

## Codex

```bash
codex mcp add context-engine --url http://127.0.0.1:8089/mcp
```

Alternatively, adapt
[`config/codex.config.toml.example`](../config/codex.config.toml.example) into
`~/.codex/config.toml` or the trusted project's `.codex/config.toml`.

Verify:

```bash
codex mcp get context-engine
```

## Environment

The MCP launchd service accepts:

| Variable | Default | Purpose |
|---|---|---|
| `CONTEXT_ENGINE_URL` | `http://localhost:8088` | Existing REST service |
| `CONTEXT_ENGINE_API_KEY` | empty | Sent as `X-API-Key` when configured |
| `CONTEXT_ENGINE_MCP_HOST` | `127.0.0.1` | MCP bind address |
| `CONTEXT_ENGINE_MCP_PORT` | `8089` | MCP service port |
| `CONTEXT_ENGINE_MCP_REQUEST_TIMEOUT` | `120` | Individual REST request timeout in seconds |
| `CONTEXT_ENGINE_MCP_RUN_TIMEOUT` | `900` | Agent or issue-audit polling deadline |
| `CONTEXT_ENGINE_MCP_POLL_INTERVAL` | `1` | Poll interval in seconds |
| `OUTPUT_DIR` | `~/Library/Application Support/context-store/artifacts` | Artifact store used for MCP summary/reference records |
| `MCP_INLINE_LIMIT` | `1000` | Serialized payload byte limit before `mode="auto"` returns an artifact reference |

## Response Modes

MCP is a control plane. REST endpoints still return their existing payloads, but
large MCP tool responses default to artifact references so they do not expand the
active LLM conversation.

| Mode | Behavior |
|---|---|
| `auto` | Default. Return inline for small responses; write and return an artifact reference when a large-result payload exceeds `MCP_INLINE_LIMIT`. |
| `summary` | Always write the full payload to an MCP artifact record and return `artifact_id`, `artifact_path`, `artifact_type`, `summary`, `token_estimate`, and metadata. |
| `inline` | Preserve the old behavior and return the full REST payload directly. |

## Example Calls

```text
investigate_codebase(
  task="Determine whether authentication protects all agent endpoints. Return evidence files and any uncovered routes."
)
```

```text
load_context(
  task="Prepare to add MCP authentication forwarding",
  paths=["ryemyster/context-manager/context-engine"],
  focus=["CONTEXT_ENGINE_API_KEY", "_AuthMiddleware", "mcp_server"]
)
```

```text
review_diff(diff="<raw git diff>")
```

```text
audit_issue(
  task="Determine whether issue 42 is implemented",
  repo="ryemyster/context-manager",
  paths=["context-engine", "tests"],
  focus=["42", "acceptance criteria"]
)
```

## Curl Migration

| Previous REST sequence | MCP replacement |
|---|---|
| Start `/agents/run`, extract `run_id`, poll status | One `investigate_codebase` call |
| `POST /context` | `load_context` |
| `POST /diff-summary` | `review_diff` |
| Start issue auditor and poll status | One `audit_issue` call |

REST clients and endpoint contracts remain supported. Shell scripts are retained
for compatibility, diagnostics, and automation outside MCP hosts.
