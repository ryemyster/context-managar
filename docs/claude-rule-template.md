# Context Engine Rule Template

Copy this into your project's `CLAUDE.md` or `.claude/rules/context-engine.md`.

The fastest path: in a new Claude Code session, run:
```
Run: `curl -s http://localhost:8088/setup` and use it to configure this project to use context-engine
```
Claude reads live status and handles the setup automatically.

---

## CLAUDE.md block (copy-paste)

```markdown
## Context Engine

A local context-engine runs at http://localhost:8088 and pre-digests repo context
so you spend fewer tokens walking files yourself.

**Before starting any non-trivial task**, check if a context bundle exists:
- If `./ai-context/context-bundle.md` exists and is recent → read it first
- If not → ask the user to run the context script, or run it yourself via Bash

### Running context scripts

Scripts live at: ~/Repos/ryemyster/local-model/scripts/

```bash
# Full context bundle (primary workflow — run before most tasks)
bash ~/Repos/ryemyster/local-model/scripts/context.sh \
  "describe the task here" \
  "src/app,src/lib" \
  "auth,stripe,relevant-terms"

# Then read:
# ./ai-context/context-bundle.md

# Other scripts:
bash ~/Repos/ryemyster/local-model/scripts/scan.sh src/app/api
bash ~/Repos/ryemyster/local-model/scripts/find.sh "stripe subscription"
bash ~/Repos/ryemyster/local-model/scripts/routes.sh
bash ~/Repos/ryemyster/local-model/scripts/summarize.sh src/lib/auth.ts
bash ~/Repos/ryemyster/local-model/scripts/vector-search.sh "auth middleware"
git diff | bash ~/Repos/ryemyster/local-model/scripts/diff-summary.sh
```

### What the context bundle gives you

- Directory structure and file inventory
- Grepped matches for your focus terms
- Semantic vector search results (if repo is indexed)
- One-paragraph synthesis of what's relevant
- Suggested files to verify

### Rules

1. The bundle is a scout report — always verify actual source files before editing
2. Context-engine is read-only. It never modifies /repo
3. One model call per request. Don't send parallel requests
4. If health check fails, proceed without it — don't block on the scout

### Endpoints

| URL | Purpose |
|---|---|
| `curl http://localhost:8088/healthcheck` | Pass/fail — HTTP 200 or 503 |
| `curl http://localhost:8088/health` | Full status JSON |
| `curl http://localhost:8088/debug` | Diagnostics: model loaded, row count, output files, tips |
| `curl http://localhost:8088/setup` | Live setup guide (Markdown) |
| http://localhost:8088/docs | Interactive API explorer |
```

---

## /context slash command (optional)

Create `.claude/commands/context.md` in your project for a one-word shortcut:

```markdown
Run the context-engine scout before implementing the task.

Usage: /context <task description>

Steps:
1. Run: bash ~/Repos/ryemyster/local-model/scripts/context.sh "$ARGUMENTS" "src/app,src/lib" ""
2. Read the output at ./ai-context/context-bundle.md
3. Report what was found (files, risks, vector hits)
4. Ask what to implement
```

Then in Claude Code, type `/context add plan enforcement to checkins` and it auto-runs the scout.

---

## Keeping the rule up to date

The `/setup` endpoint generates a fresh version of this rule based on live system state.
If something changes (new script, new endpoint, different REPO_PATH), re-fetch `/setup`
in a Claude Code session and it will rewrite the CLAUDE.md block accordingly.
