"""
context_builder.py — the /context endpoint orchestration.

This is the highest-value endpoint: builds a full context bundle
for a specific Claude task.

Order of operations (IMPORTANT for memory safety):
1. Deterministic: scan paths, grep focus terms, extract routes
2. Vector: embed query + search Supabase (uses nomic-embed-text)
3. Generation: ONE synthesis call (uses qwen2.5-coder:3b)

Steps 2 and 3 use different models. With MAX_LOADED_MODELS=1,
there will be a model swap between them (~5-10s). Acceptable.
Never interleave embed and generate calls.
"""

from pathlib import Path
from . import config
from .repo_reader import walk_repo, read_file, rel_path, safe_resolve, build_snippet_block
from .search_worker import find_in_repo, extract_imports
from .route_extractor import extract_routes
from . import ollama_client, supabase_vector


async def build_context(
    task: str,
    paths: list[str],
    focus: list[str],
) -> dict:
    """
    Build a comprehensive context bundle for a Claude task.

    Returns files, summary, risks, suggested_files, vector_hits.
    """

    # ── 1. Deterministic scan ─────────────────────────────────────────────────
    all_files: list[str] = []
    snippets: list[tuple[str, str]] = []

    for path in paths:
        try:
            base = safe_resolve(path) if path and path != "." else config.REPO_ROOT
        except Exception:
            continue

        files = walk_repo(base)
        for f in files[:config.MAX_FILES_PER_SCAN // len(paths)]:
            content = read_file(f)
            if content:
                rp = rel_path(f)
                all_files.append(rp)
                snippets.append((rp, content))

    # ── 2. Focus term grep ────────────────────────────────────────────────────
    focus_matches: list[dict] = []
    if focus:
        query = " ".join(focus)
        focus_matches = find_in_repo(query, ".", max_results=config.MAX_SNIPPETS_PER_QUERY)

        # Boost matched files into snippets if not already included
        matched_paths = {m["path"] for m in focus_matches}
        existing_paths = {s[0] for s in snippets}
        for m in focus_matches:
            if m["path"] not in existing_paths:
                try:
                    f = safe_resolve(m["path"])
                    content = read_file(f)
                    if content:
                        snippets.insert(0, (m["path"], content))  # prioritize matched files
                except Exception:
                    pass

    # ── 3. Vector search (uses nomic-embed-text — different model than generation) ──
    vector_hits: list[dict] = []
    if focus and await supabase_vector.is_available():
        embed_query = f"{task} {' '.join(focus)}"
        embedding = await ollama_client.embed(embed_query)
        if embedding:
            vector_hits = await supabase_vector.search(embedding, limit=8)

    # ── 4. Build snippet block within context budget ──────────────────────────
    # Deduplicate snippets (focus matches may overlap with path scans)
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for path, content in snippets:
        if path not in seen:
            seen.add(path)
            deduped.append((path, content))

    snippet_block = build_snippet_block(deduped)

    # File list for the prompt (deterministic, no model)
    suggested_files = list(dict.fromkeys(
        [m["path"] for m in focus_matches[:10]] +
        [h["path"] for h in vector_hits[:5]] +
        all_files[:20]
    ))

    # ── 5. ONE model call — synthesis only (uses qwen2.5-coder:3b) ───────────
    focus_str = ", ".join(focus) if focus else "general"
    prompt = f"""Task: {task}
Focus areas: {focus_str}

Relevant code:
{snippet_block}

Respond with JSON only — no markdown fences:
{{
  "summary": "What existing code is relevant to this task and where to find it",
  "risks": ["risk or gotcha to watch for", "..."],
  "suggested_files": ["path/to/file.ts", "..."]
}}"""

    raw    = await ollama_client.generate(prompt)
    parsed = ollama_client.parse_json_response(raw)

    summary         = parsed.get("summary",         raw[:400] if not parsed else "")
    risks           = parsed.get("risks",            [])
    model_suggested = parsed.get("suggested_files",  [])

    # Merge model suggestions with deterministic suggestions
    final_suggested = list(dict.fromkeys(model_suggested + suggested_files))[:20]

    return {
        "files":           list(dict.fromkeys(all_files)),
        "summary":         summary,
        "risks":           risks,
        "suggested_files": final_suggested,
        "vector_hits":     vector_hits,
    }
