"""
usage_guide.py — Clean, concise integration guide for connecting client agents.
"""

def get_usage_guide(
    base: str,
    repo: str,
    mcp_url: str,
) -> str:
    """Return concise operational integration guide markdown."""
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
        "- **Context-Safe Mode**: Run discovery calls with `mode=context_safe` by default to preserve the context window.\n"
        "- **Diff Verification**: Always call `/diff-summary` (with `git diff`) after any workspace modification to "
        "assess changes and test recommendations.\n\n"

        "## 3. Operational Workflow\n"
        "When executing repository tasks, follow this sequence:\n"
        "1. **Discover**: Search via `/find` or `/vector-search` using `mode=context_safe`.\n"
        "2. **Assess**: Evaluate match scores and paths. If matches are thin, retry once without `mode=context_safe`.\n"
        "3. **Read**: Retrieve details of specific files using `/read` (bounded using `max_chars`).\n"
        "4. **Execute**: Modify the codebase.\n"
        "5. **Verify**: Always run `/diff-summary` to audit your changes.\n"
    )
