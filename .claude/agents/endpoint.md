---
name: endpoint
description: Add a new endpoint to the context-engine following the established 4-file pattern. Use when the user wants to add a new API route. Handles models, writer, route, and /setup documentation in one pass.
---

You are adding a new endpoint to the context-engine FastAPI app. Follow this exact 4-file pattern — every endpoint in this codebase uses it without exception.

## The pattern

**File 1: `context-engine/app/models.py`**
Add a request model and response model at the bottom of their respective sections:
```python
class FooRequest(BaseModel):
    field: str
    optional_field: list[str] = []

class FooResponse(BaseModel):
    result: str
    written_to: str
```

**File 2: `context-engine/app/markdown_writer.py`**
Add a `write_foo()` function that formats the output as Markdown:
```python
def write_foo(field: str, result: str) -> str:
    slug = field.replace("/", "-")[:40]
    content = f"""# Foo: `{field}`
_Generated: {ts()} — Model: {config.OLLAMA_MODEL}_

## Result
{result}

---
**Claude: verify actual source files before acting on this output.**
"""
    return write(f"foo-{slug}.md", content)
```

**File 3: `context-engine/app/main.py`**
- Import the new models at the top
- Add the route, choosing the correct model call:
  - Judgment/risk/synthesis → `ollama_client.generate_reasoning(prompt)`
  - Code pattern/generation → `ollama_client.generate(prompt)`
  - Embeddings only → `ollama_client.embed(text)`
```python
@app.post("/foo", response_model=FooResponse)
async def foo(req: FooRequest):
    """One-line docstring describing what this endpoint does."""
    t0 = time.monotonic()
    log.debug("POST /foo field=%s", req.field)
    # ... logic ...
    written = mw.write_foo(req.field, result)
    log.debug("POST /foo done dur=%.2fs", time.monotonic() - t0)
    return FooResponse(result=result, written_to=written)
```

**File 4: Update `/setup` in `main.py`**
Add to three places in the `/setup` return string:
1. Decision rules table — when to call this endpoint
2. Endpoint reference section — shape + what it returns
3. Output files table — file pattern + re-use condition

## Model routing reminder

| Task type | Call |
|-----------|------|
| Deciding what matters, risk analysis | `generate_reasoning()` |
| Code reading, pattern matching, generation | `generate()` |
| Semantic similarity | `embed()` |

## After writing all 4 files

Run `/rebuild` to apply the changes, then test with curl:
```bash
curl -s -X POST http://localhost:8088/foo \
  -H "Content-Type: application/json" \
  -d '{"field": "test-value"}'
```
