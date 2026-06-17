"""Contract checks for the live agent-facing /setup guide."""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "context-engine"))

from app import config
from app.main import setup


@pytest.mark.asyncio
async def test_setup_is_context_safe_operational_playbook():
    models = [
        config.OLLAMA_MODEL,
        config.OLLAMA_AGENT_MODEL,
        config.OLLAMA_REASON_MODEL,
        config.OLLAMA_EMBED_MODEL,
    ]
    with patch("app.main.ollama_client.list_models", new_callable=AsyncMock, return_value=models), \
         patch("app.main.supabase_vector.is_available", new_callable=AsyncMock, return_value=True):
        body = await setup()

    assert body.startswith("# Context Engine Usage Guide")
    assert "Context Engine is a retrieval system." in body
    assert "## Agent Integration" in body
    assert "claude mcp add --scope user --transport http context-engine" in body
    assert "codex mcp add context-engine --url" in body
    assert "`AGENTS.md`, `CLAUDE.md`, `.claude/rules/*`, MCP config, slash commands" in body
    assert "skills, hooks, or local scripts" in body
    assert "Use references first." in body
    assert "Retrieve details only when necessary." in body
    assert "Avoid loading large artifacts into context." in body
    assert "### Step 1: Discover" in body
    assert "### Step 2: Narrow" in body
    assert "### Step 3: Read" in body
    assert "### Step 4: Execute" in body
    assert "### Step 5: Refresh" in body
    assert "find -> read -> act" in body
    assert "scan everything -> read everything -> act" in body
    assert "Small task: 1-3 artifacts" in body
    assert "mode=context_safe" in body
    assert "two-call fallback" in body
    assert "make one re-call without the mode flag" in body
    assert "If the result is thin" in body
    assert "Token-Saving Rationale" in body
    assert f"Code model (`{config.OLLAMA_MODEL}`) | available" in body
