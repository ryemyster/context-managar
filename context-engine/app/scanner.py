"""
scanner.py — directory scanning with bounded reads.

Deterministic first: walk, read, extract imports.
Model call last: one synthesis call on the collected snippet block.
"""

import time
from pathlib import Path
from . import config
from .repo_reader import walk_repo, read_file, rel_path, safe_resolve, build_snippet_block
from .search_worker import extract_imports
from . import ollama_client
from .logger import log


async def scan_directory(path: str = "") -> dict:
    """
    Scan a directory in the repo.

    Steps:
    1. Walk files (deterministic)
    2. Read bounded snippets (deterministic)
    3. Extract imports (deterministic)
    4. One model call for summary + patterns

    Returns file list, summary, patterns, dependencies.
    """
    t0 = time.monotonic()
    base = safe_resolve(path) if path else config.REPO_ROOT
    log.debug("scan start path=%s max_files=%d", base, config.MAX_FILES_PER_SCAN)

    files = walk_repo(base)
    file_paths = [rel_path(f) for f in files]
    log.debug("scan walk done files=%d dur=%.2fs", len(files), time.monotonic() - t0)

    # Collect snippets — cap at MAX_FILES_PER_SCAN
    snippets: list[tuple[str, str]] = []
    all_deps: set[str] = set()

    for f in files[:config.MAX_FILES_PER_SCAN]:
        content = read_file(f)
        if content:
            snippets.append((rel_path(f), content))
            all_deps.update(extract_imports(content))

    ext_deps = sorted(d for d in all_deps if not d.startswith("."))
    snippet_block = build_snippet_block(snippets)

    # Single model call
    file_list_str = "\n".join(file_paths[:25])
    prompt = f"""Analyze this directory. Respond with JSON only — no markdown fences, no explanation.

Directory: {path or "/"}
Files: {file_list_str}
Code: {snippet_block}

Respond with exactly this structure:
{{"summary":"2-3 sentences on what this code does","patterns":["pattern1","pattern2"]}}"""

    raw = await ollama_client.generate(prompt)
    parsed = ollama_client.parse_json_response(raw)

    summary  = parsed.get("summary",  raw[:300] if not parsed else "Could not parse model output")
    patterns = parsed.get("patterns", [])

    log.debug("scan done path=%s files=%d dur=%.2fs", path or "/", len(file_paths), time.monotonic() - t0)
    return {
        "files":        file_paths,
        "snippets":     snippets,
        "summary":      summary,
        "patterns":     patterns,
        "dependencies": ext_deps[:60],
    }
