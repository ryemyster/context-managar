# Headroom — Compression Proxy

Context compression layer that sits between AI agents and the LLM. Compresses tool outputs, logs, RAG chunks, files, and conversation history before they reach the model. 60–95% fewer tokens, same answers.

Repo: https://github.com/chopratejas/headroom

## Stack role

Runs as a local proxy alongside the context-engine. Claude Code (and any other OpenAI-compatible client) routes through it via `ANTHROPIC_BASE_URL`.

```
Claude Code / other agents
        │
        ▼
  Headroom proxy  (localhost:8787)
  — compresses prompts + tool outputs
        │
        ▼
  Anthropic API
```

The context-engine (localhost:8088) is upstream of this — it reduces what Claude reads from the repo. Headroom is downstream — it compresses what actually gets sent to the API.

## Install

Requires Python 3.13 — pyo3-ffi (a Rust extension dependency) does not yet support Python 3.14.

```bash
brew install python@3.13

pipx install --python $(brew --prefix python@3.13)/bin/python3.13 "headroom-ai[all]"
```

## Modes

| Mode | Command | Notes |
|------|---------|-------|
| Agent wrap (one-shot) | `headroom wrap claude` | Launches Claude Code as subprocess; no persistent service |
| Proxy (persistent) | `headroom proxy --port 8787` | Set `ANTHROPIC_BASE_URL=http://localhost:8787`; works for all tools |
| MCP server | `headroom mcp install` | Adds `headroom_compress` / `headroom_retrieve` / `headroom_stats` tools |

## Service setup (launchd)

Plist: `~/Library/LaunchAgents/life.ascendvent.headroom-proxy.plist`

Auto-starts on login, restarts on crash (KeepAlive=true). Binary: `~/.local/bin/headroom proxy --port 8787`.

```bash
# Start
launchctl load ~/Library/LaunchAgents/life.ascendvent.headroom-proxy.plist

# Stop
launchctl unload ~/Library/LaunchAgents/life.ascendvent.headroom-proxy.plist

# Status — col 1 = PID, col 2 = last exit code
launchctl list | grep headroom

# Logs
tail -f ~/Library/Logs/headroom-proxy.log
```

## Shell env vars (~/.zshrc)

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

These persist across all terminal sessions. If the proxy is down, `claude` will fail — check `launchctl list | grep headroom` first.

## Verify

```bash
headroom perf
```

## Notes

- Local-first — data never leaves the machine
- Reversible (CCR) — originals cached locally; LLM can retrieve on demand
- `headroom learn` mines failed sessions and writes corrections to `CLAUDE.md` / `AGENTS.md`
- Cross-agent memory — shared store across Claude Code, Codex, etc.
