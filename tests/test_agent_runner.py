"""
Tests for agent_runner.py — agentic loop logic.

Mocks chat_with_tools and execute_tool so the loop logic is tested
without hitting Ollama or the filesystem.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'context-engine'))

from unittest.mock import AsyncMock, patch, MagicMock
from app.agent_runner import AgentResult, _coerce_arguments, run_agent


# ── _coerce_arguments ──────────────────────────────────────────────────────────

class TestCoerceArguments:
    def test_dict_passthrough(self):
        d = {"path": "owner/repo/app", "query": "foo"}
        assert _coerce_arguments(d) == d

    def test_json_string_decoded(self):
        raw = '{"path": "owner/repo/app"}'
        assert _coerce_arguments(raw) == {"path": "owner/repo/app"}

    def test_double_encoded_json_string(self):
        # Ollama sometimes double-encodes: the string itself is JSON
        import json
        inner = json.dumps({"key": "val"})
        outer = json.dumps(inner)
        # outer is a JSON-encoded JSON string — parse once → inner string, parse again → dict
        parsed_outer = json.loads(outer)  # → inner (a string)
        assert _coerce_arguments(parsed_outer) == {"key": "val"}

    def test_invalid_json_string_returns_empty(self):
        assert _coerce_arguments("not-json{{{") == {}

    def test_non_dict_json_returns_empty(self):
        assert _coerce_arguments("[1, 2, 3]") == {}

    def test_none_returns_empty(self):
        assert _coerce_arguments(None) == {}

    def test_integer_returns_empty(self):
        assert _coerce_arguments(42) == {}


# ── run_agent — stop conditions ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_agent_stops_on_final_answer():
    """When model returns content with no tool_calls, loop stops with final_answer."""
    mock_message = {"role": "assistant", "content": "The answer is 42.", "tool_calls": []}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]):
        mock_chat.return_value = mock_message

        result = await run_agent("What is the answer?", max_iterations=5)

    assert result.stopped_reason == "final_answer"
    assert result.final_answer == "The answer is 42."
    assert result.iterations == 1
    assert result.tool_calls_made == []


@pytest.mark.asyncio
async def test_run_agent_calls_tool_and_continues():
    """When model returns tool_calls, they are executed and loop continues."""
    tool_call_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "health_check", "arguments": {}}}],
    }
    final_msg = {"role": "assistant", "content": "Engine is healthy.", "tool_calls": []}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_chat.side_effect = [tool_call_msg, final_msg]
        mock_exec.return_value = "health: ok (HTTP 200)"

        result = await run_agent("Check health", max_iterations=5)

    assert result.stopped_reason == "final_answer"
    assert result.iterations == 2
    assert len(result.tool_calls_made) == 1
    assert result.tool_calls_made[0]["name"] == "health_check"
    mock_exec.assert_called_once_with("health_check", {})


@pytest.mark.asyncio
async def test_run_agent_stops_on_model_error():
    """When chat_with_tools returns an error dict, loop stops with model_error."""
    error_msg = {"error": "Ollama timeout"}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]):
        mock_chat.return_value = error_msg

        result = await run_agent("Any task", max_iterations=5)

    assert result.stopped_reason == "model_error"
    assert "Ollama timeout" in result.final_answer
    assert result.iterations == 1


@pytest.mark.asyncio
async def test_run_agent_stops_at_max_iterations():
    """When model always calls tools, loop stops at max_iterations."""
    tool_call_msg = {
        "role": "assistant",
        "content": "Still thinking...",
        "tool_calls": [{"function": {"name": "health_check", "arguments": {}}}],
    }

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_chat.return_value = tool_call_msg
        mock_exec.return_value = "health: ok"

        result = await run_agent("Any task", max_iterations=3)

    assert result.stopped_reason == "max_iterations"
    assert result.iterations == 3


@pytest.mark.asyncio
async def test_run_agent_timeout():
    """When elapsed time exceeds budget, loop stops with timeout."""
    import time

    tool_call_msg = {
        "role": "assistant",
        "content": "thinking",
        "tool_calls": [{"function": {"name": "health_check", "arguments": {}}}],
    }

    # Patch time.monotonic to simulate budget exceeded after first call
    call_count = 0
    base_time = 1000.0

    def fake_monotonic():
        nonlocal call_count
        call_count += 1
        # First two calls return start time, third simulates past budget
        if call_count <= 2:
            return base_time
        return base_time + 700.0  # exceeds default 600s budget

    with patch("app.agent_runner.time.monotonic", side_effect=fake_monotonic), \
         patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_chat.return_value = tool_call_msg
        mock_exec.return_value = "health: ok"

        result = await run_agent("Any task", max_iterations=10)

    assert result.stopped_reason == "timeout"


@pytest.mark.asyncio
async def test_run_agent_coerces_string_arguments():
    """Tool call arguments encoded as JSON strings are coerced to dict."""
    import json
    args = json.dumps({"path": "owner/repo/app"})

    tool_call_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "scan_directory", "arguments": args}}],
    }
    final_msg = {"role": "assistant", "content": "Done.", "tool_calls": []}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_chat.side_effect = [tool_call_msg, final_msg]
        mock_exec.return_value = '{"files": [], "count": 0}'

        result = await run_agent("Scan", max_iterations=5)

    # Arguments should have been coerced from string to dict
    called_args = mock_exec.call_args[0][1]
    assert isinstance(called_args, dict)
    assert called_args == {"path": "owner/repo/app"}


@pytest.mark.asyncio
async def test_run_agent_custom_system_prompt():
    """Custom system_prompt is placed in first message."""
    final_msg = {"role": "assistant", "content": "Done.", "tool_calls": []}

    captured_messages = []

    async def capture_chat(messages, tools, model=None):
        captured_messages.extend(messages)
        return final_msg

    with patch("app.agent_runner.ollama_client.chat_with_tools", side_effect=capture_chat), \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]):
        await run_agent("Task", system_prompt="Custom prompt for testing.", max_iterations=1)

    assert captured_messages[0]["role"] == "system"
    assert captured_messages[0]["content"] == "Custom prompt for testing."


@pytest.mark.asyncio
async def test_run_agent_message_history_in_result():
    """Result includes full message_history for debugging."""
    final_msg = {"role": "assistant", "content": "Result.", "tool_calls": []}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]):
        mock_chat.return_value = final_msg

        result = await run_agent("Task", max_iterations=1)

    # system + user + assistant
    assert len(result.message_history) == 3
    assert result.message_history[0]["role"] == "system"
    assert result.message_history[1]["role"] == "user"
    assert result.message_history[2]["role"] == "assistant"
