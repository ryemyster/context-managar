# Context Engine Agent Rule

Use this block in a project's `CLAUDE.md`, `AGENTS.md`, or equivalent durable
agent instructions after registering the `context-engine` MCP server.

```markdown
## Local Context Engine

A read-only junior engineer is available through the `context-engine` MCP
server.

For non-trivial repository investigations, call `investigate_codebase` and give
it one bounded task, exact owner/repo-relative paths, focus symbols, a numbered
output contract, and explicit read-only constraints. Let the existing Context
Engine Agent plan, search memory, scan, grep, read, verify, and repair its
answer.

Use `load_context` for a bounded pre-task context bundle, `review_diff` after
editing, and `audit_issue` for evidence-based issue investigation.

Treat `scan_directory`, `find_in_code`, `summarize_file`,
`dependency_analysis`, and `vector_search` as advanced direct tools. Do not
manually orchestrate them when `investigate_codebase` can own the investigation.
Discovery results are references first. Read only the specific files needed for
the task after reviewing returned paths and summaries. For advanced direct
discovery, use `mode=context_safe` first; if the result is thin, has too few
hits, or lacks enough content to choose the next read, make one re-call without
the mode flag before escalating to broader reading.

Context Engine never writes to the repository. Verify cited source files before
acting. The senior engineer owns architecture, security decisions, code edits,
and final conclusions.

If Context Engine is unavailable, continue without it.
```

Example delegation:

```text
investigate_codebase(
  task="Read-only authentication inventory for owner/repo. Inspect owner/repo/src and owner/repo/tests. Focus on AuthMiddleware and agent routes. Return: (1) protected routes, (2) uncovered routes, (3) evidence files, (4) tests, and (5) verification. Do not write files. Do not redesign authentication. Do not invent missing code."
)
```

The live usage and repository integration playbook is available from
`GET http://localhost:8088/setup`.
