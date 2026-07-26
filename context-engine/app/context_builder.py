"""
context_builder.py — the /context endpoint orchestration.

This is the highest-value endpoint: builds a full context bundle
for a specific Claude task.

Order of operations (IMPORTANT for memory safety):
1. Deterministic: scan paths, grep focus terms, extract routes
2. Vector: embed query + search Supabase (uses nomic-embed-text)
3. Code model: ONE synthesis call (uses qwen2.5-coder:3b)

Steps 2 and 3 use different models. With MAX_LOADED_MODELS=1,
there will be a model swap between them (~1-2s for 3b). Acceptable.
Never interleave embed and generate calls.

Code model is used here (not reasoning) because:
- "What files are relevant to this task" = pattern matching, not judgment
- After nomic embed, 3b loads in ~1-2s vs 9b loads in 2-3 min (Docker)
- /diff-summary is the right home for the reasoning model (risk analysis)
"""

from . import config
from .repo_reader import walk_repo, read_file, rel_path, safe_resolve, build_snippet_block
from .logger import log
from .search_worker import find_in_repo
from . import supabase_vector
from .inference import inference


def _normalize_prefix(path: str) -> str:
    return path.strip().strip("/")


def _is_in_scope(path: str, prefixes: list[str]) -> bool:
    clean = _normalize_prefix(path)
    return any(clean == prefix or clean.startswith(f"{prefix}/") for prefix in prefixes)


def _existing_scoped_path(path: str, prefixes: list[str]) -> str | None:
    clean = _normalize_prefix(path)
    if not clean or not _is_in_scope(clean, prefixes):
        return None
    try:
        resolved = safe_resolve(clean)
    except Exception:
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    return clean


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _is_issue_audit(task: str, focus: list[str]) -> bool:
    task_text = task.lower()
    combined = f"{task} {' '.join(focus)}".lower()
    audit_term = "audit" in combined or "triage" in combined or "closable" in combined
    task_requests_audit = "issue" in task_text and ("audit" in task_text or "triage" in task_text or "closable" in task_text)
    return task_requests_audit or (bool(_issue_numbers(task, focus)) and audit_term)


def _issue_numbers(task: str, focus: list[str]) -> list[int]:
    import re

    text = f"{task} {' '.join(focus)}"
    found: set[int] = set()
    for start, end in re.findall(r"#?(\d{2,5})\s*-\s*#?(\d{2,5})", text):
        a, b = int(start), int(end)
        if a <= b and b - a <= 100:
            found.update(range(a, b + 1))
    for value in re.findall(r"(?:issue\s*|#)(\d{2,5})", text, flags=re.IGNORECASE):
        found.add(int(value))
    return sorted(found)


def _path_fragment_matches(all_files: list[str], focus: list[str], prefixes: list[str]) -> list[str]:
    fragments: list[str] = []
    for term in focus:
        clean = term.strip().strip("/")
        if "/" in clean or "." in clean:
            fragments.append(clean)
            fragments.append(clean.split("/")[-1])

    matches: list[str] = []
    for path in all_files:
        if not _is_in_scope(path, prefixes):
            continue
        for fragment in fragments:
            if fragment and (path.endswith(fragment) or fragment in path):
                matches.append(path)
                break
    return _dedupe(matches)


def _issue_audit_evidence_files(all_files: list[str], prefixes: list[str]) -> list[str]:
    evidence_suffixes = (
        "/src/agent/index.ts",
        "/src/agent/index.tsx",
        "/tests/agent.test.ts",
        "/tests/agent.test.tsx",
        "/tests/mcp-client.test.ts",
        "/tests/mcp-client.test.tsx",
        "/README.md",
    )
    evidence_keywords = (
        "/src/agent/",
        "/tests/",
    )

    matches: list[str] = []
    for path in all_files:
        if not _is_in_scope(path, prefixes):
            continue
        if path.endswith(evidence_suffixes):
            matches.append(path)
            continue
        lower = path.lower()
        if "stub" in lower and any(keyword in lower for keyword in evidence_keywords):
            matches.append(path)
    return _dedupe(matches)


async def build_context(
    task: str,
    paths: list[str],
    focus: list[str],
    use_vector: bool = False,
    force_issue_audit: bool = False,
) -> dict:
    """
    Build a comprehensive context bundle for a Claude task.

    Returns files, summary, risks, suggested_files, vector_hits.
    """

    # ── 1. Deterministic scan ─────────────────────────────────────────────────
    scoped_prefixes = [_normalize_prefix(p) for p in paths if p and p not in (".", "/")]
    all_files: list[str] = []
    discovered_files: list[str] = []
    snippets: list[tuple[str, str]] = []
    warnings: list[str] = []
    dropped_candidates: list[dict] = []

    for path in paths:
        if not path or path in (".", "/"):
            log.warning("context: skipping broad path %r — must be scoped to a subdirectory", path)
            continue
        try:
            base = safe_resolve(path)
        except Exception:
            continue

        if base.is_file():
            content = read_file(base)
            if content:
                rp = rel_path(base)
                discovered_files.append(rp)
                all_files.append(rp)
                snippets.append((rp, content))
        else:
            files = walk_repo(base)
            discovered_files.extend(rel_path(f) for f in files)
            limit = max(1, config.MAX_FILES_PER_SCAN // max(1, len(paths)))
            for f in files[:limit]:
                content = read_file(f)
                if content:
                    rp = rel_path(f)
                    all_files.append(rp)
                    snippets.append((rp, content))

    if not snippets:
        raise ValueError(
            "no valid paths produced content — all paths were skipped, unresolvable, or empty. "
            "Paths must use the owner/repo/ prefix (e.g. 'owner/repo/src' or 'owner/repo/file.py'). "
            "Bare '.' or '/' are rejected."
        )

    # ── 2. Focus term grep ────────────────────────────────────────────────────
    focus_matches: list[dict] = []
    if focus:
        query = " ".join(focus)
        for path in scoped_prefixes:
            focus_matches.extend(find_in_repo(query, path, max_results=config.MAX_SNIPPETS_PER_QUERY))

        seen_match_paths: set[str] = set()
        scoped_matches: list[dict] = []
        for match in focus_matches:
            match_path = match.get("path", "")
            if match_path in seen_match_paths:
                continue
            if _existing_scoped_path(match_path, scoped_prefixes):
                seen_match_paths.add(match_path)
                scoped_matches.append(match)
            else:
                dropped_candidates.append({"path": match_path, "reason": "focus match outside requested scope or missing"})
        focus_matches = scoped_matches

        # Boost matched files into snippets if not already included
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

    # ── 3. Vector search — opt-in only (adds embed + model-swap latency) ────────
    # Disabled by default. Pass use_vector=True only after /index has been run
    # and you want semantic hits in addition to grep matches.
    vector_hits: list[dict] = []
    if use_vector and focus and await supabase_vector.is_available():
        embed_query = f"{task} {' '.join(focus)}"
        embedding = await inference.embed(embed_query)
        if embedding:
            raw_hits = await supabase_vector.search(embedding, limit=16)
            for hit in raw_hits:
                hit_path = hit.get("path", "")
                scoped_hit = _existing_scoped_path(hit_path, scoped_prefixes)
                if scoped_hit:
                    vector_hits.append(hit)
                else:
                    dropped_candidates.append({"path": hit_path, "reason": "vector hit outside requested scope or missing"})
                if len(vector_hits) >= 8:
                    break

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
    issue_audit = force_issue_audit or _is_issue_audit(task, focus)
    path_fragment_matches = _path_fragment_matches(discovered_files, focus, scoped_prefixes)
    issue_evidence_files = _issue_audit_evidence_files(discovered_files, scoped_prefixes) if issue_audit else []
    suggested_files = _dedupe(
        [m["path"] for m in focus_matches[:10]] +
        [h["path"] for h in vector_hits[:5]] +
        issue_evidence_files[:20] +
        path_fragment_matches[:10] +
        all_files[:20]
    )
    suggested_files = [p for p in suggested_files if _existing_scoped_path(p, scoped_prefixes)]

    existing_paths = {s[0] for s in snippets}
    for path in suggested_files[:20]:
        if path in existing_paths:
            continue
        try:
            f = safe_resolve(path)
            content = read_file(f)
            if content:
                snippets.insert(0, (path, content))
                existing_paths.add(path)
        except Exception:
            pass

    # ── 5. ONE code model call — synthesis only (uses qwen2.5-coder:3b) ─────
    focus_str = ", ".join(focus) if focus else "general"
    issues = _issue_numbers(task, focus)
    audit_contract = ""
    if issue_audit:
        issue_list = ", ".join(f"#{n}" for n in issues) if issues else "the issues named in the task"
        audit_contract = f"""
This is an issue audit for {issue_list}. Include an "audit_table" array.
Each row must have exactly: issue, status, evidence_files, missing_evidence, recommendation.
Allowed status values: complete, partial, stub/docs only, insufficient evidence.
Use "insufficient evidence" when concrete implementation and test files are missing.
"""

    prompt = f"""Task: {task}
Focus areas: {focus_str}
Requested path scope: {", ".join(scoped_prefixes)}
Valid candidate files: {", ".join(suggested_files[:30])}

Relevant code:
{snippet_block}

Rules:
- Only cite files from Requested path scope.
- Preserve full repository-relative paths exactly as shown.
- Do not invent files or collapse monorepo package paths into bare root-relative paths.
- If the task contains a numbered output contract, follow that structure in the summary.
- If evidence is insufficient, say so directly.
{audit_contract}

Respond with JSON only — no markdown fences:
{{
  "summary": "What existing code is relevant to this task and where to find it",
  "risks": ["risk or gotcha to watch for", "..."],
  "suggested_files": ["path/to/file.ts", "..."],
  "audit_table": []
}}"""

    raw    = await inference.generate(prompt)
    parsed = inference.parse_json_response(raw)

    summary         = parsed.get("summary",         raw[:400] if not parsed else "")
    risks           = parsed.get("risks",            [])
    model_suggested = parsed.get("suggested_files",  [])
    audit_table     = parsed.get("audit_table",      [])

    # Merge model suggestions with deterministic suggestions, then validate them.
    final_suggested: list[str] = []
    for candidate in model_suggested + suggested_files:
        scoped = _existing_scoped_path(str(candidate), scoped_prefixes)
        if scoped:
            final_suggested.append(scoped)
        else:
            dropped_candidates.append({"path": str(candidate), "reason": "model suggested path outside requested scope or missing"})
    final_suggested = _dedupe(final_suggested)[:20]

    if issue_audit and not audit_table:
        warnings.append("issue audit requested but model did not return audit_table")

    return {
        "files":           list(dict.fromkeys(all_files)),
        "summary":         summary,
        "risks":           risks,
        "suggested_files": final_suggested,
        "vector_hits":     vector_hits,
        "audit_table":     audit_table if isinstance(audit_table, list) else [],
        "warnings":        warnings,
        "dropped_candidates": dropped_candidates,
        "scope":           scoped_prefixes,
        "audit_evidence_files": issue_evidence_files,
    }
