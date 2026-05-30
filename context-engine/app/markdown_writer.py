"""
markdown_writer.py — all markdown output formatting.

Every output file includes:
- timestamp
- scope
- source paths
- summary
- risks
- files Claude must verify
- suggested next prompts

Keep output concise — Claude reads this to orient, then verifies source.
"""

from datetime import datetime, timezone
from pathlib import Path
from . import config


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def write(filename: str, content: str) -> str:
    """Write content to OUTPUT_DIR/filename. Returns absolute path."""
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = config.OUTPUT_DIR / filename
    out.write_text(content, encoding="utf-8")
    return str(out)


def _list(items: list[str], prefix: str = "-") -> str:
    if not items:
        return "_none_"
    return "\n".join(f"{prefix} {i}" for i in items)


def write_scan(
    path: str,
    files: list[str],
    summary: str,
    patterns: list[str],
    dependencies: list[str],
) -> str:
    slug = path.replace("/", "-").strip("-") or "root"
    content = f"""# Scan: `{path or "/"}`
_Generated: {ts()} — Model: {config.OLLAMA_MODEL}_

## Summary
{summary}

## Files ({len(files)} total)
{_list(files[:60])}

## Patterns Observed
{_list(patterns)}

## External Dependencies
{_list(dependencies[:40])}

---
**Claude: verify actual source files before editing. This is a context summary only.**
"""
    return write(f"scan-{slug}.md", content)


def write_find(
    query: str,
    path: str,
    matches: list[dict],
    synthesis: str,
) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40]
    match_lines = "\n".join(
        f"- `{m['path']}` line {m['line_no']}: `{m['line'][:100]}`"
        for m in matches[:20]
    )
    content = f"""# Find: `{query}`
_Generated: {ts()} — Scope: `{path}`_

## Synthesis
{synthesis}

## Matches ({len(matches)} files)
{match_lines or "_no matches_"}

---
**Suggested next:** `/summarize` on the most relevant file above.
"""
    return write(f"find-{slug}.md", content)


def write_routes(routes_data: dict, analysis: str) -> str:
    api = routes_data.get("api_routes", [])
    pages = routes_data.get("page_routes", [])
    actions = routes_data.get("server_actions", [])
    mw = routes_data.get("middleware", [])
    auth = routes_data.get("auth_paths", [])

    api_lines = "\n".join(
        f"- `{r['path']}` [{', '.join(r['methods']) or 'unknown'}]"
        for r in api
    ) or "_none_"

    mw_lines = "\n".join(
        f"- `{m['path']}` → matchers: {', '.join(m['matchers']) or 'none'}"
        for m in mw
    ) or "_none_"

    content = f"""# Routes Map
_Generated: {ts()}_

## API Routes ({len(api)})
{api_lines}

## Page Routes ({len(pages)})
{_list([r for r in pages[:30]])}

## Server Actions ({len(actions)})
{_list(actions[:20])}

## Middleware ({len(mw)})
{mw_lines}

## Auth Paths ({len(auth)})
{_list(auth[:20])}

## Route Analysis
{analysis}

---
**Suggested next:** `/context` with focus on auth or specific feature area.
"""
    return write("routes.md", content)


def write_dependencies(path: str, dep_data: dict) -> str:
    slug = path.replace("/", "-").strip("-") or "root"
    graph = dep_data.get("graph", {})
    graph_lines = []
    for f, imports in list(graph.items())[:30]:
        if imports:
            graph_lines.append(f"- `{f}` → {', '.join(f'`{i}`' for i in imports[:5])}")

    content = f"""# Dependencies: `{path or "/"}`
_Generated: {ts()}_

## External Packages ({len(dep_data.get('external', []))})
{_list(dep_data.get('external', [])[:50])}

## Internal Imports ({len(dep_data.get('internal', []))})
{_list(dep_data.get('internal', [])[:50])}

## Package.json Declared Deps
{_list(dep_data.get('package_json_deps', [])[:50])}

## Import Graph (sample)
{chr(10).join(graph_lines) or "_none_"}

---
**Claude: cross-reference external packages against package.json for undeclared dependencies.**
"""
    return write(f"dependencies-{slug}.md", content)


def write_summary(
    file: str,
    purpose: str,
    dependencies: list[str],
    risks: list[str],
    arch_notes: list[str],
) -> str:
    slug = file.replace("/", "-").replace(".", "-")[:50]
    content = f"""# File Summary: `{file}`
_Generated: {ts()} — Model: {config.OLLAMA_MODEL}_

## Purpose
{purpose}

## Dependencies
{_list(dependencies[:20])}

## Risks
{_list(risks)}

## Architectural Notes
{_list(arch_notes)}

---
**Claude: read the actual file at `{file}` before making changes.**
**Suggested next:** `/find` for any dependency or pattern identified above.
"""
    return write(f"summary-{slug}.md", content)


def write_context(
    task: str,
    files: list[str],
    summary: str,
    risks: list[str],
    suggested_files: list[str],
    vector_hits: list[dict],
) -> str:
    vh_lines = "\n".join(
        f"- `{h['path']}` (similarity: {h.get('similarity', 0):.2f})"
        for h in vector_hits[:8]
    ) or "_no vector hits_"

    content = f"""# Context Bundle
_Task: {task}_
_Generated: {ts()} — Model: {config.OLLAMA_MODEL}_

## Summary
{summary}

## Relevant Files ({len(files)})
{_list(files[:30])}

## Vector Search Hits
{vh_lines}

## Risks
{_list(risks)}

## Files Claude Must Verify
{_list(suggested_files[:20])}

## Suggested Next Prompts
- "Read the files above and implement: {task}"
- "Review risks before editing"
- "Run `/diff-summary` after changes"

---
**This is scout context. Claude verifies source, makes all final decisions.**
"""
    return write("context-bundle.md", content)


def write_diff(
    summary: str,
    risks: list[str],
    files_touched: list[str],
    test_recs: list[str],
) -> str:
    from hashlib import md5
    slug = md5(summary[:100].encode()).hexdigest()[:8]
    content = f"""# Diff Summary
_Generated: {ts()} — Model: {config.OLLAMA_MODEL}_

## Summary
{summary}

## Files Touched ({len(files_touched)})
{_list(files_touched)}

## Risks
{_list(risks)}

## Test Recommendations
{_list(test_recs)}

---
**Claude: review actual diff before final sign-off.**
"""
    return write(f"diff-{slug}.md", content)


def write_vector_results(query: str, matches: list[dict]) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40]
    lines = []
    for m in matches[:10]:
        sim = m.get("similarity", 0)
        lines.append(f"### `{m['path']}` (similarity: {sim:.2f})\n```\n{m.get('chunk', '')[:300]}\n```")

    content = f"""# Vector Search: `{query}`
_Generated: {ts()}_

## Matches ({len(matches)})
{chr(10).join(lines) or "_no matches_"}

---
**Suggested next:** `/summarize` on the most relevant file.
"""
    return write(f"vector-{slug}.md", content)


def write_draft(file: str, task: str, mode: str, code: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", file.lower().replace("/", "-"))[:50]
    ext  = Path(file).suffix or ".txt"
    content = f"""# Draft: `{file}`
_Task: {task}_
_Generated: {ts()} — Mode: {mode} — Model: {config.OLLAMA_MODEL}_

## Generated Code
```{ext.lstrip(".")}
{code}
```

---
**Claude: review this draft before applying. You own the write — qwen owns the generation.**
**Verify:** types, imports, edge cases, security boundaries. Edit inline before applying.
"""
    return write(f"draft-{slug}.md", content)


def write_scaffold_file(file: str, task: str, spec: str, mode: str, code: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", file.lower().replace("/", "-"))[:50]
    ext  = Path(file).suffix or ".txt"
    content = f"""# Scaffold: `{file}`
_Task: {task}_
_Spec: {spec}_
_Generated: {ts()} — Mode: {mode} — Model: {config.OLLAMA_MODEL}_

## Generated Code
```{ext.lstrip(".")}
{code}
```

---
**Claude: review before applying. Check types, imports, edge cases. You own the write.**
"""
    return write(f"scaffold-{slug}.md", content)


def write_index_report(paths: list[str], indexed: int, skipped: int, errors: int) -> str:
    content = f"""# Index Report
_Generated: {ts()}_

## Paths Indexed
{_list(paths)}

## Results
- Indexed: {indexed} chunks
- Skipped (already up to date): {skipped}
- Errors: {errors}

## Notes
- Embeddings stored in Supabase table: `{config.SUPABASE_VECTOR_TABLE}`
- Search via `/vector-search` or `/context` with focus terms
- Re-index after significant code changes with `force: true`
"""
    return write("index-report.md", content)
