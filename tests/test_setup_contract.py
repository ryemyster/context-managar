"""Contract checks for the live agent-facing /setup guide."""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "context-engine"))

from app import config
from app.main import setup


@pytest.mark.asyncio
async def test_setup_documents_mcp_routing_and_agent_boundaries():
    models = [
        config.OLLAMA_MODEL,
        config.OLLAMA_AGENT_MODEL,
        config.OLLAMA_REASON_MODEL,
        config.OLLAMA_EMBED_MODEL,
    ]
    with patch("app.main.ollama_client.list_models", new_callable=AsyncMock, return_value=models), \
         patch("app.main.supabase_vector.is_available", new_callable=AsyncMock, return_value=True):
        body = await setup()

    assert "investigate_codebase` — primary/default" in body
    assert f"Agent model (`{config.OLLAMA_AGENT_MODEL}`) | available" in body
    assert f"capped at `{config.OLLAMA_AGENT_MEMORY_TIMEOUT:g}s`" in body
    assert f"capped at `{config.OLLAMA_AGENT_CALL_TIMEOUT:g}s`" in body
    assert f"capped at `{config.OLLAMA_AGENT_SELECT_TIMEOUT:g}s`" in body
    assert f"capped at `{config.OLLAMA_AGENT_VERIFY_TIMEOUT:g}s`" in body
    assert '"verifier_timeout"' in body
    assert "A response cannot become a final answer before an enabled tool returns evidence" in body
    assert "constrained JSON action schema" in body
    assert "remain recovery paths" in body
    assert "Model routing" in body
