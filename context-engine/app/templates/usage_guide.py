"""
usage_guide.py — Clean, concise integration guide for connecting client agents.
"""

def get_usage_guide(
    base: str,
    repo: str,
    mcp_url: str,
    ollama_timeout: int,
    reason_timeout: int,
    agent_timeout: int,
    call_timeout: int,
) -> str:
    """Return concise operational integration guide markdown with endpoint expectations and timeouts."""
    return (
        "# Context Engine Usage Guide\n\n"
        f"_Base URL: `{base}` · REPO_ROOT: `{repo}`_\n\n"

        "## 1. Connection & Setup\n"
        "Configure your agent client to use Context Engine via the MCP HTTP transport:\n"
        "```bash\n"
        f"claude mcp add --scope user --transport http context-engine {mcp_url}\n"
        f"codex mcp add context-engine --url {mcp_url}\n"
        "```\n\n"

        "## 2. Core Integration Rules\n"
        "Include these rules in your client context (e.g. `.claude/rules`, `AGENTS.md` or `.claude.json`):\n\n"
        "- **Path Contract**: Every repository path must be relative to `REPO_ROOT` and prefixed with `owner/repo` "
        "(e.g., `ryemyster/context-manager/context-engine/app`). Never pass bare paths (`.`, `/`, or `src`).\n"
        "- **Reference-First Lookups**: Treat search results (`/find`, `/vector-search`, `/scan`) as references. "
        "Read summaries first; do not load raw file contents unless explicitly required.\n"
        "- **Curated Memory Writes**: Persist only explicit, reviewable notes with `/store-context-note`. "
        "Do not treat the service as a raw chat transcript sink.\n"
        "- **Context-Safe Mode**: Run discovery calls with `mode=context_safe` by default to preserve the context window.\n"
        "- **Agent Loop Budget**: `investigate_codebase` is a multi-turn junior-agent loop, not a single search. "
        "Use at least `max_iterations: 15` for cross-file investigations; a low cap such as 5–6 usually stops during discovery.\n"
        "- **Clarification Gate**: If a task is underspecified or recursive, stop before broad scans and return 1-3 concrete "
        "questions to the caller. Do not guess the scope and burn the iteration budget on repeated directory scans.\n"
        "- **Scope Contract**: `allowed_scopes` accepts capability scopes only: `repo:read`, `memory:read`, and `engine:read`. "
        "Put path restrictions in the task text, not in `allowed_scopes`.\n"
        "- **Diff Verification**: Always call `/diff-summary` (with `git diff`) after any workspace modification to "
        "assess changes and test recommendations.\n\n"

        "## 3. Operational Workflow\n"
        "When executing repository tasks, follow this sequence:\n"
        "1. **Delegate**: For open-ended repository investigation, prefer `investigate_codebase` with a bounded task, "
        "path constraints in the task text, and enough iterations for discovery, reading, and synthesis. If the task is "
        "still ambiguous, ask clarifying questions before scanning broadly.\n"
        "2. **Discover**: For single bounded lookups, search via `/find` or `/vector-search` using `mode=context_safe`.\n"
        "3. **Assess**: Evaluate match scores and paths. If matches are thin, retry once without `mode=context_safe`.\n"
        "4. **Read**: Retrieve details of specific files using `/read` (bounded using `max_chars`).\n"
        "5. **Execute**: Modify the codebase.\n"
        "6. **Persist**: Store curated plans, decisions, or triage notes with `/store-context-note` when they should survive the session.\n"
        "7. **Verify**: Always run `/diff-summary` to audit your changes.\n\n"

        "## 4. Endpoint Specifications & Timeouts\n\n"
        "**Workload Classes:**\n"
        "- **Interactive**: Await directly. If a call times out, narrow the path/scope parameter rather than increasing client timeouts.\n"
        "- **Background (Async)**: Fire-and-poll. Returns a `run_id` immediately; poll status periodically.\n\n"
        "**Per-Endpoint Timeouts & Budgets:**\n\n"
        "| Endpoint Group | Mode | Hard Timeout | Notes |\n"
        "| :--- | :--- | :--- | :--- |\n"
        f"| `/healthcheck`, `/setup` | Interactive | — | Always fast |\n"
        "| `/health`, `/stats` | Interactive | 90s | Dependency probes and rolling operational metrics |\n"
        "| `/store-context-note` | Interactive | 90s | Durable curated note write plus immediate indexing |\n"
        "| `/vector-search` | Interactive | 90s | Semantic index lookup |\n"
        f"| `/scan`, `/find`, `/routes`, `/dependencies`, `/summarize` | Interactive | {ollama_timeout}s | Codebase metadata & snippet extraction |\n"
        f"| `/context`, `/draft`, `/scaffold` | Interactive | {ollama_timeout}s | Code and context generation |\n"
        f"| `/diff-summary` | Interactive/Sync | {reason_timeout}s | Deep change assessment & risk review |\n"
        f"| `/agents/run`, `/agents/issue-auditor/run` | Async (Poll) | {agent_timeout}s (Total) | Deep agent runs; poll status every 10–30s (max {call_timeout}s per call) |\n\n"
        "**Rules for Orchestrators:**\n"
        "- Await Interactive endpoints directly.\n"
        "- If an agent or endpoint returns clarification questions, answer them before resuming; do not retry the same broad task unchanged.\n"
        "- Poll Background loops on their status endpoints; never block the main thread.\n"
        "- If a call returns a partial result or `stopped_reason: timeout`, retry with a narrower scope.\n"
        "- If an agent run returns `stopped_reason: max_iterations`, either increase `max_iterations` or narrow the task. "
        "Do not treat the artifact summary as a conclusive answer.\n"
        "- With MCP `mode=context_safe`, expect a compact artifact reference/summary. Read the artifact only when the summary is too thin.\n"
    )
