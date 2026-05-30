# Development Rules — context-engine

## After editing any Python file

Always rebuild before testing. The container caches the old code until rebuilt.
Use `/rebuild` — it builds, restarts, and confirms health in one step.

Never assume a change works by reading the code. Test with curl.

## Testing endpoints

```bash
# Health — check all three models
curl -s http://localhost:8088/health | python3 -m json.tool

# Quick smoke test any endpoint
curl -s -X POST http://localhost:8088/<endpoint> \
  -H "Content-Type: application/json" \
  -d '{"field": "value"}' | python3 -m json.tool
```

## Config changes

All env vars live in `config.py` — never call `os.getenv()` anywhere else.
`.env` overrides compose defaults. After changing `.env`, restart with `docker compose up -d` (no rebuild needed unless Python changed).

## Model routing — enforce always

| Task | Function |
|------|----------|
| Judgment, risks, synthesis, "what matters" | `ollama_client.generate_reasoning()` |
| Code reading, pattern matching, generation | `ollama_client.generate()` |
| Embeddings | `ollama_client.embed()` |

Never use `generate()` for a task that is judgment. Never use `generate_reasoning()` for a task that is code pattern matching.

## Path safety

Always use `safe_resolve()` from `repo_reader.py` before reading any file path from a request. Never construct paths manually from user input.

## Adding endpoints

Four files, always: `models.py` → `markdown_writer.py` → `main.py` (route) → `main.py` (/setup). Use the `/endpoint` agent.

## /setup is the agent contract

Every agent that calls this service reads `/setup` to understand how to use it. When you add, remove, or change an endpoint, update `/setup` in the same commit. Stale `/setup` = broken agent integrations across all projects.
