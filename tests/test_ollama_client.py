"""Focused tests for bounded Ollama agent calls."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "context-engine"))

from app import config, ollama_client


@pytest.mark.asyncio
async def test_chat_with_tools_uses_agent_limits():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"message": {"role": "assistant", "content": "done"}}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch("app.ollama_client.get_client", return_value=client):
        result = await ollama_client.chat_with_tools([], [], timeout=12.5)

    assert result["content"] == "done"
    request = client.post.await_args
    assert request.kwargs["timeout"] == 12.5
    assert request.kwargs["json"]["model"] == config.OLLAMA_AGENT_MODEL
    assert request.kwargs["json"]["think"] is False
    assert request.kwargs["json"]["options"]["num_predict"] == config.OLLAMA_AGENT_NUM_PREDICT


@pytest.mark.asyncio
async def test_select_tool_call_uses_json_schema():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "message": {
            "content": (
                '{"action":"call_tool","name":"read_file",'
                '"arguments":{"file":"owner/repo/app.py"},"final_answer":""}'
            ),
        },
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file",
            "parameters": {"type": "object"},
        },
    }]

    with patch("app.ollama_client.get_client", return_value=client):
        result = await ollama_client.select_tool_call(
            [{"role": "user", "content": "Inspect owner/repo/app.py"}],
            tools,
            timeout=12.5,
        )

    assert result["function"]["name"] == "read_file"
    request = client.post.await_args.kwargs
    assert request["timeout"] == 12.5
    assert request["json"]["format"]["properties"]["name"]["enum"] == ["read_file"]
    assert request["json"]["format"]["properties"]["action"]["enum"] == [
        "call_tool",
        "final_answer",
    ]
    assert "tools" not in request["json"]


@pytest.mark.asyncio
async def test_select_tool_call_can_finish_from_evidence():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "message": {
            "content": (
                '{"action":"final_answer","name":"read_file","arguments":{},'
                '"final_answer":"The title is context-engine."}'
            ),
        },
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file",
            "parameters": {"type": "object"},
        },
    }]

    with patch("app.ollama_client.get_client", return_value=client):
        result = await ollama_client.select_tool_call(
            [
                {"role": "user", "content": "Find the title"},
                {"role": "tool", "content": 'title="context-engine"'},
            ],
            tools,
        )

    assert result == {"final_answer": "The title is context-engine."}


@pytest.mark.asyncio
async def test_verify_agent_answer_uses_fast_schema_model():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "message": {
            "content": (
                '{"passed":true,"rationale":"supported",'
                '"unsupported_claims":[],"evidence_gap":false}'
            ),
        },
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch("app.ollama_client.get_client", return_value=client):
        result = await ollama_client.verify_agent_answer("evidence prompt", timeout=12.5)

    assert result["passed"] is True
    request = client.post.await_args.kwargs
    assert request["timeout"] == 12.5
    assert request["json"]["model"] == config.OLLAMA_AGENT_VERIFY_MODEL
    assert request["json"]["format"]["required"] == [
        "passed",
        "rationale",
        "unsupported_claims",
        "evidence_gap",
    ]


@pytest.mark.asyncio
async def test_answer_from_evidence_uses_json_schema_without_tools():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "message": {"content": '{"final_answer":"The title is context-engine."}'},
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch("app.ollama_client.get_client", return_value=client):
        result = await ollama_client.answer_from_evidence([
            {"role": "user", "content": "Find the title"},
            {"role": "tool", "content": 'title="context-engine"'},
        ])

    assert result == {"final_answer": "The title is context-engine."}
    request = client.post.await_args.kwargs["json"]
    assert request["format"]["required"] == ["final_answer"]
    assert "tools" not in request
