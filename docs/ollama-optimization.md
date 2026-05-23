# Ollama Optimization for M3 Air (8 GB RAM)

> **Context:** `founderos-ollama` runs CPU-only in Docker on macOS.  
> There is no Metal/ANE access inside the Docker VM on macOS.  
> These settings prevent OOM, reduce swap pressure, and maximize throughput within that constraint.

---

## The Problem with Out-of-Box Ollama

Default Ollama configuration is designed for machines with spare capacity. On 8 GB RAM with multiple Docker stacks running, the defaults will cause:

- **Multiple models loaded simultaneously** — each 3b model takes ~2.2 GB
- **Parallel requests** — two concurrent inference calls can exhaust memory
- **Model stays loaded indefinitely** — KEEP_ALIVE default is 5 minutes, but loaded = RAM consumed even at idle
- **Thread count untuned** — Ollama may spawn more threads than your P-core count, causing contention

---

## Required Environment Variables

Add these to the `ollama` service in your **founderos** `docker-compose.yml`:

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    environment:
      # Only 1 model loaded at a time — evicts previous model on new load
      - OLLAMA_MAX_LOADED_MODELS=1

      # Only 1 concurrent inference request — prevents double memory load
      - OLLAMA_NUM_PARALLEL=1

      # Unload model after 5 minutes of idle — frees ~2.2 GB back to Docker VM
      # Set to "0" to unload immediately after each request (slowest first call, safest RAM)
      - OLLAMA_KEEP_ALIVE=5m

      # M3 base chip has 4 performance cores + 4 efficiency cores.
      # CPU-only inference benefits from matching P-core count, not total core count.
      # M3 Air: use 4. M3 Pro (11-core): use 6.
      - OLLAMA_NUM_THREAD=4

      # Reduces KV-cache memory footprint — safe to enable on all models
      - OLLAMA_FLASH_ATTENTION=1
```

> **Note:** These changes must be applied in the founderos session/repo, not here.  
> This file documents what to add — do not edit founderos from this repo.

---

## Model Selection

| Model | Disk | RAM (loaded) | Speed (CPU) | Verdict |
|---|---|---|---|---|
| `qwen2.5-coder:3b` | 1.9 GB | ~2.2 GB | 5–12 tok/s | ✅ Use this |
| `qwen2.5-coder:7b` | 4.7 GB | ~5.0 GB | 2–4 tok/s | ❌ Will OOM |
| `nomic-embed-text` | 274 MB | ~300 MB | fast | ✅ Already there |

**Always pull the default tag**, which uses Q4_K_M quantization:
```bash
bash scripts/pull-model.sh
# or manually:
curl -X POST http://localhost:11434/api/pull -d '{"name":"qwen2.5-coder:3b"}'
```

Do **not** pull `:latest` explicitly as it may resolve to Q8 (double the RAM).

---

## Verify Settings Are Applied

After updating founderos docker-compose and running `docker compose up -d`:

```bash
# Confirm env vars are set inside the container
docker exec founderos-ollama env | grep OLLAMA

# Check what models are loaded right now (0 = model unloaded, good)
curl -s http://localhost:11434/api/ps | python3 -m json.tool

# Check all available models
curl -s http://localhost:11434/api/tags | python3 -m json.tool
```

---

## Monitor Memory During Inference

Run this in a terminal while making a scan or summarize request:

```bash
# Watch Ollama memory usage in real time
watch -n 2 'docker stats --no-stream founderos-ollama --format "{{.Name}}: {{.MemUsage}} ({{.MemPerc}})"'

# Or watch all Docker containers
docker stats --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}"
```

**Expected behaviour:**
- **Before request:** founderos-ollama ~20 MB (model unloaded)
- **During first request:** spikes to ~2.2 GB as qwen2.5-coder:3b loads
- **During inference:** stays at ~2.2 GB, total Docker VM ~5.5 GB / 5.786 GB
- **5 min after last request:** drops back to ~20 MB (KEEP_ALIVE fires)

---

## Docker Desktop Resource Settings

Docker Desktop → Settings → Resources:

| Setting | Current | Recommendation |
|---|---|---|
| Memory | 5.786 GB | Keep at 6 GB max — macOS needs ≥2 GB |
| CPUs | (default) | Leave at default — Ollama thread count handles this |
| Swap | 1 GB | Leave at 1 GB — a small safety valve is fine |
| Virtual disk | — | No change needed |

> Do **not** increase Docker memory beyond 6 GB on an 8 GB machine.  
> macOS itself needs room for kernel, browser, IDE, etc.

---

## What Slow Means Here (Set Expectations)

On CPU-only Docker with qwen2.5-coder:3b:

| Task | Estimated Time |
|---|---|
| `/health` check | < 1s |
| `/routes` (no model — all deterministic) | 2–5s |
| `/summarize` single file | 20–60s |
| `/scan` small directory (10 files) | 30–90s |
| `/find` broad query | 30–120s |
| `/diff-summary` medium diff | 30–90s |

This is expected. The value is that Claude Code doesn't spend its context on mechanical scanning — these results are pre-computed markdown files Claude reads in one shot.

---

## Quick Tuning Reference

```bash
# See current Ollama config inside container
docker exec founderos-ollama env | grep OLLAMA

# Force unload all models now (without restart)
curl -X POST http://localhost:11434/api/generate \
  -d '{"model":"qwen2.5-coder:3b","keep_alive":0,"prompt":""}'

# Load model and warm it (first call is slowest)
curl -X POST http://localhost:11434/api/generate \
  -d '{"model":"qwen2.5-coder:3b","prompt":"hello","stream":false}'

# Check loaded models
curl -s http://localhost:11434/api/ps
```
