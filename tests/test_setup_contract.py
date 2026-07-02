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
    with patch("app.main.inference.list_models", new_callable=AsyncMock, return_value=models), \
         patch("app.main.inference.list_embedding_models", new_callable=AsyncMock, return_value=models), \
         patch("app.main.supabase_vector.is_available", new_callable=AsyncMock, return_value=True):
        body = await setup()

    assert body.startswith("# Context Engine Usage Guide")
    assert "claude mcp add --scope user --transport http context-engine" in body
    assert "codex mcp add context-engine --url" in body
    assert "**Path Contract**" in body
    assert "**Reference-First Lookups**" in body
    assert "**Context-Safe Mode**" in body
    assert "**Diff Verification**" in body
    assert "**Discover**" in body
    assert "**Assess**" in body
    assert "**Read**" in body
    assert "**Execute**" in body
    assert "**Verify**" in body
