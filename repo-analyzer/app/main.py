"""
repo-analyzer — lightweight FastAPI service for local AI repo context generation.

Rules:
- Never writes to /repo (read-only mount)
- Validates all paths to prevent traversal outside REPO_ROOT
- Skips heavy folders (node_modules, .next, .git, etc.)
- Calls Ollama ONCE per request after deterministic scanning
- Single worker only — concurrent model calls would OOM 8GB RAM
"""

import json
import os
import re
from datetime import datetime, timezone
from hashlib import md5
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://founderos-ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
REPO_ROOT    = Path(os.getenv("REPO_ROOT",   "/repo")).resolve()
OUTPUT_DIR   = Path(os.getenv("OUTPUT_DIR",  "/output")).resolve()

SKIP_DIRS: set[str] = {
    "node_modules", ".next", ".git", "dist", "build",
    "coverage", ".turbo", ".vercel", ".netlify",
    "__pycache__", ".cache", ".pytest_cache",
    "venv", ".venv", ".mypy_cache",
}

CODE_EXTENSIONS: set[str] = {
    ".ts", ".tsx", ".js", ".jsx",
    ".py", ".go", ".rs",
    ".json", ".yaml", ".yml", ".toml",
    ".sql", ".sh", ".md", ".env",
    ".css", ".scss",
}

MAX_FILE_BYTES  = 32 * 1024   # 32 KB per file
MAX_TOTAL_CHARS = 4_000       # ~1k tokens — keeps prompt well inside 4096 ctx window on CPU
OLLAMA_TIMEOUT  = 180.0       # seconds — 3b on CPU, first call after load can be slow

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="repo-analyzer", version="1.0.0")

# Single shared client — connection reuse, respects timeout
ollama_client = httpx.AsyncClient(base_url=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)


# ── Helpers ───────────────────────────────────────────────────────────────────

def ts() -> str:
    """UTC timestamp for output headers."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def safe_resolve(rel_path: str) -> Path:
    """
    Resolve a relative path under REPO_ROOT.
    Raises HTTP 400 if the resolved path escapes REPO_ROOT (path traversal guard).
    """
    p = (REPO_ROOT / rel_path.lstrip("/")).resolve()
    if not str(p).startswith(str(REPO_ROOT)):
        raise HTTPException(status_code=400, detail=f"Path traversal rejected: {rel_path!r}")
    return p


def should_skip(path: Path) -> bool:
    """True if any component of the path is in SKIP_DIRS."""
    try:
        rel = path.relative_to(REPO_ROOT)
        return any(part in SKIP_DIRS for part in rel.parts)
    except ValueError:
        return False


def walk_repo(base: Path) -> list[Path]:
    """Walk base directory, skipping heavy dirs, returning code files only."""
    results = []
    for item in sorted(base.rglob("*")):
        if item.is_file() and item.suffix in CODE_EXTENSIONS and not should_skip(item):
            results.append(item)
    return results


def read_snippet(path: Path, max_bytes: int = MAX_FILE_BYTES) -> str:
    """Read up to max_bytes from a file, replacing encoding errors gracefully."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except Exception:
        return ""


def rel_path(path: Path) -> str:
    """Return path relative to REPO_ROOT as a string."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def write_output(filename: str, content: str) -> str:
    """Write content to OUTPUT_DIR/filename, return absolute path as string."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / filename
    out.write_text(content, encoding="utf-8")
    return str(out)


def build_snippet_block(files: list[tuple[str, str]]) -> str:
    """
    Aggregate file snippets into a single block within MAX_TOTAL_CHARS.
    Stops adding files once budget is exhausted.
    """
    block: list[str] = []
    total = 0
    for path, content in files:
        header = f"\n### {path}\n```\n"
        footer = "\n```\n"
        budget = MAX_TOTAL_CHARS - total - len(header) - len(footer)
        if budget <= 0:
            break
        chunk = content[:budget]
        block.append(header + chunk + footer)
        total += len(header) + len(chunk) + len(footer)
    return "".join(block)


def extract_imports(content: str) -> list[str]:
    """Deterministically extract import paths from source content."""
    pattern = re.compile(
        r'(?:import|from|require)\s+[\'"]([^\'"\s]+)[\'"]',
        re.MULTILINE,
    )
    return pattern.findall(content)


async def ollama_generate(prompt: str) -> str:
    """
    Call Ollama /api/generate. Returns response text.
    Never raises — returns error string on failure so endpoints degrade gracefully.
    """
    try:
        r = await ollama_client.post(
            "/api/generate",
            json={
                "model":   OLLAMA_MODEL,
                "prompt":  prompt,
                "stream":  False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 400,
                    "num_ctx": 2048,    # explicit — prevents model loading huge context on CPU
                },
            },
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except httpx.TimeoutException:
        return "[model timeout — try a smaller path or shorter diff]"
    except Exception as e:
        return f"[model error: {e}]"


def parse_json_response(text: str) -> dict:
    """Extract first JSON object from model response. Returns empty dict on failure."""
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception:
        pass
    return {}


def slugify(s: str, max_len: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:max_len]


# ── Request / Response models ─────────────────────────────────────────────────

class ScanRequest(BaseModel):
    path: str = ""


class FindRequest(BaseModel):
    query: str


class SummarizeRequest(BaseModel):
    file: str


class DiffRequest(BaseModel):
    diff: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Service health + Ollama connectivity check.
    Returns whether the configured model is actually available.
    """
    ollama_ok   = False
    model_avail = False
    models: list[str] = []

    try:
        r = await ollama_client.get("/api/tags", timeout=5.0)
        if r.status_code == 200:
            ollama_ok = True
            models    = [m["name"] for m in r.json().get("models", [])]
            model_avail = any(OLLAMA_MODEL in m for m in models)
    except Exception:
        pass

    return {
        "status":          "ok",
        "ollama":          ollama_ok,
        "ollama_host":     OLLAMA_HOST,
        "model":           OLLAMA_MODEL,
        "model_available": model_avail,
        "available_models": models,
        "repo_root":       str(REPO_ROOT),
        "output_dir":      str(OUTPUT_DIR),
    }


@app.post("/scan")
async def scan(req: ScanRequest):
    """
    Scan a directory in the repo. Walks files, extracts deps, asks model to summarize.
    Writes: /output/scan-{slug}.md
    """
    base = safe_resolve(req.path) if req.path else REPO_ROOT
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {req.path!r}")

    files     = walk_repo(base)
    file_list = [rel_path(f) for f in files]

    # Collect snippets — cap at 50 files before model call
    snippets: list[tuple[str, str]] = []
    all_deps: set[str] = set()
    for f in files[:50]:
        content = read_snippet(f)
        if content:
            snippets.append((rel_path(f), content))
            all_deps.update(extract_imports(content))

    ext_deps = sorted(d for d in all_deps if not d.startswith("."))
    snippet_block = build_snippet_block(snippets)

    prompt = f"""You are a code analyst. Analyze this repository directory and respond with JSON only — no prose, no markdown fences around the JSON itself.

Directory: {req.path or "/"}
Files found:
{chr(10).join(file_list[:30])}

Code snippets:
{snippet_block}

Respond with exactly this structure:
{{
  "summary": "2-3 sentences describing what this code does and its purpose",
  "patterns": ["pattern or convention observed", "..."],
  "key_modules": ["important module or file", "..."]
}}"""

    raw = await ollama_generate(prompt)
    parsed = parse_json_response(raw)

    summary     = parsed.get("summary",     raw[:400] if not parsed else "")
    patterns    = parsed.get("patterns",    [])
    key_modules = parsed.get("key_modules", [])

    slug   = slugify(req.path or "root") or "root"
    md_out = f"""# Scan: {req.path or "/"}
Generated: {ts()}
Model: {OLLAMA_MODEL}

## Summary
{summary}

## Files ({len(file_list)} total)
{chr(10).join(f"- {p}" for p in file_list)}

## Key Modules
{chr(10).join(f"- {m}" for m in key_modules)}

## Patterns
{chr(10).join(f"- {p}" for p in patterns)}

## External Dependencies
{chr(10).join(f"- {d}" for d in ext_deps[:60])}
"""
    written = write_output(f"scan-{slug}.md", md_out)

    return {
        "path":         req.path,
        "files":        file_list,
        "summary":      summary,
        "patterns":     patterns,
        "dependencies": ext_deps[:60],
        "written_to":   written,
    }


@app.post("/find")
async def find(req: FindRequest):
    """
    Grep the repo for query terms. Asks model to synthesize what the matches mean.
    Writes: /output/find-{slug}.md
    """
    terms = req.query.lower().split()
    files = walk_repo(REPO_ROOT)

    matches: list[str]           = []
    match_snippets: list[tuple[str, str]] = []

    for f in files:
        content = read_snippet(f)
        if not content:
            continue
        cl = content.lower()
        if any(term in cl for term in terms):
            r = rel_path(f)
            matches.append(r)
            if len(match_snippets) < 10:
                match_snippets.append((r, content))

    if match_snippets:
        snippet_block = build_snippet_block(match_snippets)
        prompt = f"""Query: "{req.query}"

The following files matched. Summarize what they do in relation to the query.
List the key implementation locations. Be concise — one paragraph max.

{snippet_block}"""
        synthesis = await ollama_generate(prompt)
    else:
        synthesis = "No files matched the query."

    slug   = slugify(req.query)
    md_out = f"""# Find: {req.query}
Generated: {ts()}
Model: {OLLAMA_MODEL}

## Synthesis
{synthesis}

## Matching Files ({len(matches)} total)
{chr(10).join(f"- {m}" for m in matches)}
"""
    written = write_output(f"find-{slug}.md", md_out)

    return {
        "query":      req.query,
        "matches":    matches,
        "files":      matches,
        "written_to": written,
    }


@app.post("/routes")
async def routes():
    """
    Classify all route files in the repo (Next.js App Router pattern).
    Asks model to describe each API route's method and auth requirements.
    Writes: /output/routes.md
    """
    files = walk_repo(REPO_ROOT)

    api_routes:     list[str] = []
    page_routes:    list[str] = []
    server_actions: list[str] = []
    middleware:     list[str] = []
    auth_paths:     list[str] = []

    auth_terms = {"auth", "login", "session", "token", "jwt", "oauth", "signin", "signup"}

    for f in files:
        r = rel_path(f)
        name = f.name.lower()
        stem = f.stem.lower()

        if name in ("route.ts", "route.js", "route.tsx", "route.jsx"):
            api_routes.append(r)
        elif name in ("page.tsx", "page.jsx", "page.ts", "page.js"):
            page_routes.append(r)
        elif "action" in name or "actions" in stem:
            server_actions.append(r)
        elif name in ("middleware.ts", "middleware.js"):
            middleware.append(r)

        if any(term in r.lower() for term in auth_terms):
            if r not in auth_paths:
                auth_paths.append(r)

    # Build snippets from API routes + middleware for model analysis
    route_files_to_read = (api_routes + middleware)[:20]
    route_snippets: list[tuple[str, str]] = []
    for r in route_files_to_read:
        content = read_snippet(safe_resolve(r))
        if content:
            route_snippets.append((r, content))

    if route_snippets:
        snippet_block = build_snippet_block(route_snippets)
        prompt = f"""Analyze these Next.js route files. For each route, briefly describe:
- HTTP methods handled (GET, POST, etc.)
- Auth/middleware requirements
- What the route does

Be concise — one line per route file.

{snippet_block}"""
        analysis = await ollama_generate(prompt)
    else:
        analysis = "No API route files found in this repository."

    all_routes = sorted(set(api_routes + page_routes))

    md_out = f"""# Routes Map
Generated: {ts()}
Model: {OLLAMA_MODEL}

## API Routes ({len(api_routes)})
{chr(10).join(f"- `{r}`" for r in api_routes)}

## Page Routes ({len(page_routes)})
{chr(10).join(f"- `{r}`" for r in page_routes)}

## Server Actions ({len(server_actions)})
{chr(10).join(f"- `{r}`" for r in server_actions)}

## Middleware ({len(middleware)})
{chr(10).join(f"- `{r}`" for r in middleware)}

## Auth Paths ({len(auth_paths)})
{chr(10).join(f"- `{r}`" for r in auth_paths)}

## Route Analysis
{analysis}
"""
    written = write_output("routes.md", md_out)

    return {
        "routes":         all_routes,
        "api_routes":     api_routes,
        "server_actions": server_actions,
        "middleware":     middleware,
        "auth_paths":     auth_paths,
        "written_to":     written,
    }


@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    """
    Summarize a single file: purpose, dependencies, risks, architectural notes.
    Writes: /output/summary-{slug}.md
    """
    f = safe_resolve(req.file)
    if not f.exists() or not f.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.file!r}")

    content = read_snippet(f)
    if not content.strip():
        raise HTTPException(status_code=422, detail="File is empty or unreadable")

    det_deps = extract_imports(content)

    prompt = f"""Analyze this file and respond with JSON only — no prose, no markdown fences around the JSON.

File: {req.file}

```
{content[:8_000]}
```

Respond with exactly this structure:
{{
  "purpose": "one sentence describing what this file does",
  "dependencies": ["imported module or package", "..."],
  "risks": ["potential issue or footgun", "..."],
  "architectural_notes": ["design observation", "..."]
}}"""

    raw    = await ollama_generate(prompt)
    parsed = parse_json_response(raw)

    purpose   = parsed.get("purpose",   raw[:300] if not parsed else "")
    deps      = parsed.get("dependencies",      det_deps)
    risks     = parsed.get("risks",             [])
    arch_notes = parsed.get("architectural_notes", [])

    slug   = slugify(req.file.replace("/", "-").replace(".", "-"))
    md_out = f"""# File Summary: {req.file}
Generated: {ts()}
Model: {OLLAMA_MODEL}

## Purpose
{purpose}

## Dependencies
{chr(10).join(f"- `{d}`" for d in deps[:30])}

## Risks
{chr(10).join(f"- {r}" for r in risks)}

## Architectural Notes
{chr(10).join(f"- {n}" for n in arch_notes)}
"""
    written = write_output(f"summary-{slug}.md", md_out)

    return {
        "file":               req.file,
        "purpose":            purpose,
        "dependencies":       deps[:30],
        "risks":              risks,
        "architectural_notes": arch_notes,
        "written_to":         written,
    }


@app.post("/diff-summary")
async def diff_summary(req: DiffRequest):
    """
    Summarize a git diff: what changed, risks, test recommendations.
    Writes: /output/diff-{hash}.md
    """
    diff_text = req.diff[:MAX_TOTAL_CHARS]

    # Deterministic: extract touched file paths from diff headers
    file_pattern = re.compile(r'^(?:---|\+\+\+)\s+(?:[ab]/)?(.+)$', re.MULTILINE)
    raw_files    = file_pattern.findall(diff_text)
    files_touched = list(dict.fromkeys(
        f for f in raw_files
        if f not in ("/dev/null", "null") and not f.startswith("/dev/null")
    ))

    prompt = f"""Summarize this git diff. Respond with JSON only — no prose, no markdown fences.

{diff_text}

Respond with exactly this structure:
{{
  "summary": "2-3 sentences describing what changed and why it matters",
  "risks": ["potential regression or issue introduced", "..."],
  "test_recommendations": ["what to test or verify", "..."]
}}"""

    raw    = await ollama_generate(prompt)
    parsed = parse_json_response(raw)

    summary   = parsed.get("summary",              raw[:400] if not parsed else "")
    risks     = parsed.get("risks",                [])
    test_recs = parsed.get("test_recommendations", [])

    slug   = md5(diff_text[:200].encode()).hexdigest()[:8]
    md_out = f"""# Diff Summary
Generated: {ts()}
Model: {OLLAMA_MODEL}

## Summary
{summary}

## Files Touched ({len(files_touched)})
{chr(10).join(f"- `{f}`" for f in files_touched)}

## Risks
{chr(10).join(f"- {r}" for r in risks)}

## Test Recommendations
{chr(10).join(f"- {t}" for t in test_recs)}
"""
    written = write_output(f"diff-{slug}.md", md_out)

    return {
        "summary":              summary,
        "risks":                risks,
        "files_touched":        files_touched,
        "test_recommendations": test_recs,
        "written_to":           written,
    }
