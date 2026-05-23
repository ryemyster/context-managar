"""
search_worker.py — deterministic grep-style search across the repo.

No LLM involved. Fast. Runs before any model call.
"""

import re
from pathlib import Path
from . import config
from .repo_reader import walk_repo, read_file, rel_path, safe_resolve


def find_in_repo(
    query: str,
    base_path: str = ".",
    max_results: int | None = None,
) -> list[dict]:
    """
    Search for query terms across all code files in base_path.

    Returns list of matches:
      {path, line_no, line, context_before, context_after}

    Deterministic — no model calls.
    """
    limit = max_results or config.MAX_SNIPPETS_PER_QUERY
    terms = [t.lower() for t in query.split() if t]
    if not terms:
        return []

    base = safe_resolve(base_path) if base_path and base_path != "." else config.REPO_ROOT
    files = walk_repo(base)

    matches: list[dict] = []

    for f in files:
        if len(matches) >= limit * 3:   # gather more than limit, deduplicate by file
            break
        content = read_file(f)
        if not content:
            continue

        lines = content.splitlines()
        for i, line in enumerate(lines):
            ll = line.lower()
            if any(term in ll for term in terms):
                matches.append({
                    "path":           rel_path(f),
                    "line_no":        i + 1,
                    "line":           line.rstrip(),
                    "context_before": lines[max(0, i-2):i],
                    "context_after":  lines[i+1:min(len(lines), i+3)],
                })
                if len(matches) >= limit * 5:
                    break

    # Deduplicate by file, keep first match per file
    seen_files: set[str] = set()
    deduped: list[dict] = []
    for m in matches:
        if m["path"] not in seen_files:
            seen_files.add(m["path"])
            deduped.append(m)
        if len(deduped) >= limit:
            break

    return deduped


def extract_imports(content: str) -> list[str]:
    """Deterministically extract all import paths from source content."""
    pattern = re.compile(
        r'(?:import|from|require)\s+[\'"]([^\'"\s]+)[\'"]',
        re.MULTILINE,
    )
    return list(dict.fromkeys(pattern.findall(content)))   # preserve order, dedupe


def grep_pattern(content: str, pattern: str) -> list[str]:
    """Find all lines matching a regex pattern. Returns matched lines."""
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        return [line.strip() for line in content.splitlines() if rx.search(line)]
    except re.error:
        return []
