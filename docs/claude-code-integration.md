# Integrating Context Engine With Claude Code

Context Engine is the junior engineer. Claude plans, reviews evidence, makes
architecture decisions, and owns every repository write.

The preferred transport is MCP. REST remains available for scripts,
compatibility, and troubleshooting.

## Prerequisites

```bash
curl -sf http://localhost:8088/healthcheck
```

If the service is not running:

```bash
launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
```

## Install

```bash
bash scripts/install-mcp.sh
claude mcp add --scope user --transport http \
  context-engine http://127.0.0.1:8089/mcp
```

Verify:

```bash
claude mcp get context-engine
```

For project configuration, adapt
[`../config/claude-code.mcp.json.example`](../config/claude-code.mcp.json.example)
as `.mcp.json`.

## Primary Workflow

Use `investigate_codebase` for repository questions:

```text
investigate_codebase(
  task="Determine whether authentication protects all agent endpoints. Return route evidence, middleware evidence, gaps, and verification."
)
```

The adapter calls `POST /agents/run`, polls
`GET /agents/run/status/{run_id}`, and returns the completed response. Claude
does not need to orchestrate scans, greps, reads, or polling.

Check:

- `tool_calls_made` for the evidence trail
- `plan_state` for the junior's plan
- `verification.passed` and `verification.unsupported_claims`
- `final_answer` for the conclusion

Verify cited source files before implementing or making issue decisions.

## Other Tools

| Tool | Use |
|---|---|
| `load_context` | Bounded context bundle before implementation |
| `review_diff` | Risks and test recommendations after editing |
| `audit_issue` | Evidence-based issue audit |

Advanced tools are `scan_directory`, `find_in_code`, `summarize_file`,
`dependency_analysis`, and `vector_search`. Use them only for a single bounded
retrieval operation. Do not manually chain them when `investigate_codebase` can
own the investigation.

## Project Rule

Add this to `CLAUDE.md` or `.claude/rules/context-engine.md`:

```markdown
## Context Engine

Use the `context-engine` MCP server for non-trivial repository work.
Prefer `investigate_codebase` for repository investigation; do not manually
orchestrate advanced retrieval tools when delegation fits.
Use `load_context` for bounded pre-task context and `review_diff` after edits.
Context Engine is read-only. Verify its evidence and own all file writes and
decisions. If the service is unavailable, continue without it.
```

## Authentication And Remote REST

The persistent MCP launchd service reads:

```text
CONTEXT_ENGINE_URL=http://localhost:8088
CONTEXT_ENGINE_API_KEY=<optional secret>
CONTEXT_ENGINE_MCP_HOST=127.0.0.1
CONTEXT_ENGINE_MCP_PORT=8089
```

Set these before running `scripts/install-mcp.sh` when overriding defaults. The
REST API key is forwarded as `X-API-Key`.

## REST Compatibility

Existing REST clients remain unchanged:

```bash
curl -s -X POST http://localhost:8088/agents/run \
  -H "Content-Type: application/json" \
  -d '{"task":"Investigate authentication coverage"}'
```

Use REST for shell automation, diagnostics, or clients without MCP. Fetch
`GET /setup` for the live MCP and REST contract.
