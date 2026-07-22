"""Contract checks for the live agent-facing /setup guide."""

import os
import sys
from types import SimpleNamespace
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
    request = SimpleNamespace(
        headers={},
        base_url="http://localhost:8088/",
        url=SimpleNamespace(scheme="http", netloc="localhost:8088"),
    )
    with patch("app.main.inference.list_models", new_callable=AsyncMock, return_value=models), \
         patch("app.main.inference.list_embedding_models", new_callable=AsyncMock, return_value=models), \
         patch("app.main.supabase_vector.is_available", new_callable=AsyncMock, return_value=True):
        body = await setup(request)

    assert body.startswith("# Context Engine Usage Guide")
    assert "claude mcp add --scope user --transport http context-engine" in body
    assert "codex mcp add context-engine --url" in body
    assert ":8089/mcp" in body
    assert "**Path Contract**" in body
    assert "**Reference-First Lookups**" in body
    assert "**Curated Memory Writes**" in body
    assert "**Context-Safe Mode**" in body
    assert "**Agent Loop Budget**" in body
    assert "max_iterations: 15" in body
    assert "**Scope Contract**" in body
    assert "Put path restrictions in the task text, not in `allowed_scopes`" in body
    assert "**Diff Verification**" in body
    assert "**Delegate**" in body
    assert "**Discover**" in body
    assert "**Assess**" in body
    assert "**Read**" in body
    assert "**Execute**" in body
    assert "**Persist**" in body
    assert "**Verify**" in body
    assert "## 4. Endpoint Specifications & Timeouts" in body
    assert "`/store-context-note`" in body
    assert "`/stats`" in body
    assert "**Workload Classes:**" in body
    assert "**Rules for Orchestrators:**" in body
    assert "stopped_reason: max_iterations" in body
    assert "Do not treat the artifact summary as a conclusive answer" in body
    assert "With MCP `mode=context_safe`, expect a compact artifact reference/summary" in body


@pytest.mark.asyncio
async def test_setup_prefers_request_derived_public_urls_when_present():
    request = SimpleNamespace(
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "context.example.com",
        },
        base_url="http://internal:8088/",
        url=SimpleNamespace(scheme="http", netloc="internal:8088"),
    )

    body = await setup(request)

    assert "https://context.example.com" in body
    assert "context-engine https://context.example.com/mcp" in body
