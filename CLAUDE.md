# local-model / context-engine

This IS the context engine. Changes here affect every project that uses it as a scout.

## Project structure

```
context-engine/app/
  main.py           — all routes + /setup documentation (update /setup when adding endpoints)
  config.py         — env vars and constants — single source of truth, import from here
  ollama_client.py  — generate() · generate_reasoning() · embed()
  models.py         — Pydantic request/response models for all endpoints
  markdown_writer.py — output file formatters (one write_* function per endpoint)
  diff_reviewer.py  — /diff-summary logic
  context_builder.py — /context orchestration
  scanner.py        — /scan logic
  search_worker.py  — /find + grep logic
  route_extractor.py — /routes logic
  dependency_mapper.py — /dependencies logic
  supabase_vector.py — pgvector client
  repo_reader.py    — safe file reading + path guard (never bypass this)
docker-compose.yml  — container config
.env                — local model + Supabase overrides (gitignored)
scripts/            — shell wrappers for each endpoint
```

## Three-model stack

| Model | Env var | Used by |
|-------|---------|---------|
| `qwen3.5:9b` | `OLLAMA_REASON_MODEL` | `/diff-summary` |
| `qwen2.5-coder:3b` | `OLLAMA_MODEL` | `/context`, `/draft`, `/scaffold`, `/scan`, `/find`, `/summarize` |
| `nomic-embed-text` | `OLLAMA_EMBED_MODEL` | `/index`, `/vector-search`, `/context` (vector step) |

**Routing rule:** `/diff-summary` → `generate_reasoning()` (risk analysis is judgment). Everything else → `generate()` (code pattern matching) or `embed()`. `/context` uses code model for synthesis — relevance scoring is pattern matching, not reasoning.

## Dev workflow

1. Edit `.py` files
2. `/rebuild` — container must rebuild for changes to take effect
3. Test with `curl` — the API is the source of truth, not the code

Never test by reading the code and assuming it works. Always curl the endpoint.

## Adding a new endpoint — 4 files, always

1. `models.py` — add `FooRequest` and `FooResponse`
2. `markdown_writer.py` — add `write_foo()` 
3. `main.py` — add `@app.post("/foo")` route
4. `main.py /setup` — document the endpoint in the agent protocol

Use the `/endpoint` agent to scaffold the pattern.

## Context engine calls for this repo

Path prefix: `ryemyster/local-model`
App source: `ryemyster/local-model/context-engine/app`

```bash
curl -s -X POST http://localhost:8088/context \
  -H "Content-Type: application/json" \
  -d '{"task": "your task", "paths": ["ryemyster/local-model/context-engine/app"], "focus": ["relevant", "terms"]}'
```

## Hard rules

- Never bypass `safe_resolve()` in `repo_reader.py` — it's the path traversal guard
- Never write to `/repo` — the engine is read-only on the mounted repo
- `config.py` is the single source of truth for all env vars — never `os.getenv()` outside it
- Always update `/setup` when adding or changing an endpoint — it's the agent contract
