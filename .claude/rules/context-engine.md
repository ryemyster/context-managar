## Context Engine — when and how to use it

A local context scout runs at http://localhost:8088. Claude is the SR dev; the scout is the JR dev — you plan, delegate, review, and apply.
Do not delegate to `/draft` or `/scaffold` for novel architecture, auth/security paths, or complex multi-system logic.

**Hard constraint:** the scout is read-only. All output goes to `~/Library/Application Support/context-store/artifacts/` as Markdown.
Always verify actual source files before editing — output files are scout reports, not ground truth.

### Workflow: find → assess → read → act → verify

**Step 1 — Discover:** `investigate_codebase`, `/find`, `/vector-search`, or `/scan`. Goal: candidate references, not content.

**Step 2 — Assess confidence (gate):** decide before reading anything.
- **HIGH → proceed:** ≥1 artifact with a matching path, specific file/symbol located, scope ≤3 files.
- **LOW → escalate in order:**
  1. One re-call without `mode=context_safe`
  2. Spawn an Explore subagent
  3. Ask the user
- **LOW signals:** 0 artifacts returned, unrelated paths, symbol not found, scope >5 files.

**Step 3 — Read:** 1–3 artifacts max. Prefer summary mode; full mode only when exact implementation is required.

**Step 4 — Execute:** make the change with minimal loaded context.

**Step 5 — Verify:** after every Edit or Write, run `review_diff` (or `POST /diff-summary`) with `git diff HEAD`. Do not skip.

**Step 6 — Refresh if stale:** if follow-up edits shift scope, repeat from Step 1.

### Availability check — always first

```bash
curl -s http://localhost:8088/healthcheck
```

If non-200 or connection refused: proceed without the scout. Never block on it.
The service is local-only and will not be available in CI, preview, or production environments.

### Decision table

All `path` values must use the `owner/repo/` prefix — the service root is `~/Repos`, so `"src"` alone resolves to nothing.

| Situation | Endpoint | Body |
|-----------|----------|------|
| Starting any non-trivial task | `POST /context` | `{"task": "...", "paths": ["ryemyster/context-manager/context-engine/app"], "focus": [...]}` |
| Need to know what is in a directory | `POST /scan` | `{"path": "ryemyster/context-manager/context-engine/app"}` |
| Need to find where a concept lives | `POST /find` | `{"query": "...", "path": "ryemyster/context-manager/context-engine/app"}` |
| Need to understand one specific file | `POST /summarize` | `{"file": "ryemyster/context-manager/context-engine/app/..."}` |
| Need the import graph of a path | `POST /dependencies` | `{"path": "ryemyster/context-manager/context-engine/app"}` |
| Mechanical task, single file (`mode: "edit"` or `"create"`) | `POST /draft` | `{"task": "...", "file": "ryemyster/context-manager/context-engine/app/...", "context_files": [...], "mode": "edit"}` |
| Mechanical task, multiple files — preserve context window | `POST /scaffold` | `{"task": "...", "files": [{"file": "...", "spec": "...", "mode": "create"}], "context_files": [...]}` |
| After editing — before returning | `POST /diff-summary` | `{"diff": "<git diff output>"}` |
| Semantic code search (after indexing) | `POST /vector-search` | `{"query": "...", "limit": 8}` |

### Output file reuse

Output files live in `~/Library/Application Support/context-store/artifacts/`. Re-use a cached file when the task and
underlying files have not changed this session — do not re-call the endpoint unnecessarily.

| File pattern | Endpoint | Re-use if |
|---|---|---|
| `context-bundle.md` | `/context` | same task this session |
| `scan-<slug>.md` | `/scan` | same path, no code changes |
| `find-<slug>.md` | `/find` | same query |
| `summary-<slug>.md` | `/summarize` | file has not been edited |
| `diff-<hash>.md` | `/diff-summary` | same diff |
| `vector-<slug>.md` | `/vector-search` | same query |
| `draft-<slug>.md` | `/draft` | same task + file (re-draft if unsatisfied) |
| `scaffold-<slug>.md` | `/scaffold` | same task + file list (re-scaffold individual files if needed) |

### Scout vs. Explore agents

Use localhost:8088 for **broad scan work** — code quality audits, smell detection, coverage analysis, finding where a concept lives across the codebase. The scout does the scanning; Claude reviews and applies.

Use Explore agents only for **targeted file location** — "find the file that owns this specific route or behaviour." When an Explore agent reports a finding, treat it as a lead — verify against source files before acting.

### Skip the scout when

- The task is a one-liner or the relevant files are already in context this session
- `http://localhost:8088/healthcheck` returns non-200
- You are running in a non-local environment (CI, etc.)
