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
from app.agent_runner import AgentResult, _coerce_arguments, run_agent, _verify_answer, _build_repair_prompt
from app.tool_registry import ToolResult


# ── _coerce_arguments ──────────────────────────────────────────────────────────

class TestCoerceArguments:
    def test_dict_passthrough(self):
        d = {"path": "owner/repo/app", "query": "foo"}
        assert _coerce_arguments(d) == d

    def test_json_string_decoded(self):
        raw = '{"path": "owner/repo/app"}'
        assert _coerce_arguments(raw) == {"path": "owner/repo/app"}

    def test_double_encoded_json_string(self):
        import json
        inner = json.dumps({"key": "val"})
        outer = json.dumps(inner)
        parsed_outer = json.loads(outer)
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
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec, \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""):
        mock_chat.side_effect = [tool_call_msg, final_msg]
        mock_exec.return_value = ToolResult(ok=True, data="health: ok (HTTP 200)")

        result = await run_agent("Check health", max_iterations=5)

    assert result.stopped_reason == "final_answer"
    assert result.iterations == 2
    assert len(result.tool_calls_made) == 1
    assert result.tool_calls_made[0]["name"] == "health_check"
    mock_exec.assert_called_once_with("health_check", {}, allowed_scopes=None)
    assert result.memory_context_used is False
    assert result.memory_hits == 0


@pytest.mark.asyncio
async def test_run_agent_preflight_skipped_when_search_memory_not_in_tools():
    """When tools list excludes search_memory, preflight is not called."""
    final_msg = {"role": "assistant", "content": "Done.", "tool_calls": []}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value="some memory") as mock_preflight:
        mock_chat.return_value = final_msg
        result = await run_agent("Scan code", tools=["scan_directory", "read_file"], max_iterations=1)

    mock_preflight.assert_not_called()
    assert result.memory_context_used is False


@pytest.mark.asyncio
async def test_run_agent_memory_context_used_when_preflight_returns_content():
    """memory_context_used=True and memory_hits>0 when preflight finds results."""
    final_msg = {"role": "assistant", "content": "Done.", "tool_calls": []}
    preflight_result = "Prior memory\n[0.87] some/path\nsome chunk\n\n---\n\n[0.72] other/path\nother chunk"

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=preflight_result):
        mock_chat.return_value = final_msg
        result = await run_agent("Find route handlers", max_iterations=1)

    assert result.memory_context_used is True
    assert result.memory_hits == 2


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
        mock_exec.return_value = ToolResult(ok=True, data="health: ok")

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

    call_count = 0
    base_time = 1000.0

    def fake_monotonic():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return base_time
        return base_time + 700.0

    with patch("app.agent_runner.time.monotonic", side_effect=fake_monotonic), \
         patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_chat.return_value = tool_call_msg
        mock_exec.return_value = ToolResult(ok=True, data="health: ok")

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
        mock_exec.return_value = ToolResult(ok=True, data='{"files": [], "count": 0}')

        result = await run_agent("Scan", max_iterations=5)

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

    assert len(result.message_history) == 3
    assert result.message_history[0]["role"] == "system"
    assert result.message_history[1]["role"] == "user"
    assert result.message_history[2]["role"] == "assistant"


@pytest.mark.asyncio
async def test_run_agent_plan_state_captured_from_update_plan():
    """When the model calls update_plan, plan_state is captured on AgentResult."""
    import json

    update_plan_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {
            "name": "update_plan",
            "arguments": {
                "goal": "Find all route handlers",
                "steps": ["search_memory", "scan_directory", "read_file"],
            },
        }}],
    }
    final_msg = {"role": "assistant", "content": "Done.", "tool_calls": []}

    plan_data = json.dumps({
        "goal": "Find all route handlers",
        "steps": ["search_memory", "scan_directory", "read_file"],
        "current_step": 0,
        "blockers": [],
    })

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec, \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""):
        mock_chat.side_effect = [update_plan_msg, final_msg]
        mock_exec.return_value = ToolResult(ok=True, data=plan_data)

        result = await run_agent("Find all route handlers", max_iterations=5)

    assert result.plan_state.get("goal") == "Find all route handlers"
    assert result.plan_state.get("steps") == ["search_memory", "scan_directory", "read_file"]


@pytest.mark.asyncio
async def test_run_agent_plan_state_empty_when_update_plan_not_called():
    """plan_state is empty dict when the model never calls update_plan."""
    final_msg = {"role": "assistant", "content": "Done.", "tool_calls": []}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""):
        mock_chat.return_value = final_msg
        result = await run_agent("Quick task", max_iterations=1)

    assert result.plan_state == {}


@pytest.mark.asyncio
async def test_run_agent_scope_denied_serialized_to_model():
    """When a tool is scope-denied, the error string is fed back to the model."""
    tool_call_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "read_file", "arguments": {"file": "owner/repo/f.py"}}}],
    }
    final_msg = {"role": "assistant", "content": "Cannot read files.", "tool_calls": []}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec, \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""):
        mock_chat.side_effect = [tool_call_msg, final_msg]
        mock_exec.return_value = ToolResult(
            ok=False,
            data="tool 'read_file' requires scope ['repo:read']",
            error_type="scope_denied",
            retryable=False,
            recovery_hint="Add one of ['repo:read'] to allowed_scopes",
        )

        result = await run_agent("Read a file", max_iterations=5,
                                  allowed_scopes=["memory:read"])

    assert result.stopped_reason == "final_answer"
    # The tool message in history should contain the scope_denied error
    tool_messages = [m for m in result.message_history if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "scope_denied" in tool_messages[0]["content"]


# ── _verify_answer ─────────────────────────────────────────────────────────────

_GOOD_VERIFICATION = (
    '{"passed": true, "rationale": "Answer matches evidence.", '
    '"unsupported_claims": [], "evidence_gap": false}'
)
_FAIL_VERIFICATION = (
    '{"passed": false, "rationale": "No tool evidence for claim X.", '
    '"unsupported_claims": ["claim X"], "evidence_gap": true}'
)


@pytest.mark.asyncio
async def test_verify_answer_passed():
    """Happy path — model returns passed=true."""
    with patch("app.agent_runner.ollama_client.generate_reasoning", new_callable=AsyncMock) as mock_gen, \
         patch("app.agent_runner.ollama_client.parse_json_response",
               return_value={"passed": True, "rationale": "Answer matches evidence.",
                             "unsupported_claims": [], "evidence_gap": False}):
        mock_gen.return_value = _GOOD_VERIFICATION
        result = await _verify_answer(
            task="Find routes",
            final_answer="There are 3 routes.",
            tool_calls_made=[{"name": "scan_directory", "result": "found 3 .py files"}],
            plan_state={"goal": "Find routes"},
        )

    assert result["passed"] is True
    assert isinstance(result["rationale"], str)
    assert isinstance(result["unsupported_claims"], list)
    assert result["evidence_gap"] is False


@pytest.mark.asyncio
async def test_verify_answer_failed_with_unsupported_claims():
    """Verifier returns passed=false with unsupported claims."""
    with patch("app.agent_runner.ollama_client.generate_reasoning", new_callable=AsyncMock) as mock_gen, \
         patch("app.agent_runner.ollama_client.parse_json_response",
               return_value={"passed": False, "rationale": "No evidence for claim X.",
                             "unsupported_claims": ["claim X"], "evidence_gap": True}):
        mock_gen.return_value = _FAIL_VERIFICATION
        result = await _verify_answer(
            task="Find routes",
            final_answer="There are 3 routes and a websocket handler.",
            tool_calls_made=[],
            plan_state={},
        )

    assert result["passed"] is False
    assert "claim X" in result["unsupported_claims"]
    assert result["evidence_gap"] is True


@pytest.mark.asyncio
async def test_verify_answer_parse_failure_degrades_gracefully():
    """When the model returns non-JSON, result has passed=None and error=parse_failed."""
    with patch("app.agent_runner.ollama_client.generate_reasoning", new_callable=AsyncMock) as mock_gen, \
         patch("app.agent_runner.ollama_client.parse_json_response", return_value={}):
        mock_gen.return_value = "Sorry, I cannot determine this."
        result = await _verify_answer("task", "answer", [], {})

    assert result["passed"] is None
    assert result["error"] == "parse_failed"


@pytest.mark.asyncio
async def test_verify_answer_exception_returns_unavailable():
    """When generate_reasoning raises, result has passed=None and error=verifier_unavailable."""
    with patch("app.agent_runner.ollama_client.generate_reasoning",
               new_callable=AsyncMock, side_effect=RuntimeError("connection refused")):
        result = await _verify_answer("task", "answer", [], {})

    assert result["passed"] is None
    assert result["error"] == "verifier_unavailable"


@pytest.mark.asyncio
async def test_verify_answer_uses_plan_goal_when_present():
    """Verifier uses plan_state.goal rather than task when available."""
    captured_prompts = []

    async def capture_gen(prompt: str) -> str:
        captured_prompts.append(prompt)
        return _GOOD_VERIFICATION

    with patch("app.agent_runner.ollama_client.generate_reasoning", side_effect=capture_gen), \
         patch("app.agent_runner.ollama_client.parse_json_response",
               return_value={"passed": True, "rationale": "ok", "unsupported_claims": [], "evidence_gap": False}):
        await _verify_answer(
            task="generic task",
            final_answer="answer",
            tool_calls_made=[],
            plan_state={"goal": "specific goal from update_plan"},
        )

    assert "specific goal from update_plan" in captured_prompts[0]
    assert "generic task" not in captured_prompts[0]


@pytest.mark.asyncio
async def test_run_agent_verification_populated_on_final_answer():
    """When run_agent stops with final_answer, verification dict is populated."""
    final_msg = {"role": "assistant", "content": "Routes are in main.py.", "tool_calls": []}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""), \
         patch("app.agent_runner.ollama_client.generate_reasoning", new_callable=AsyncMock) as mock_gen, \
         patch("app.agent_runner.ollama_client.parse_json_response",
               return_value={"passed": True, "rationale": "Good.", "unsupported_claims": [], "evidence_gap": False}):
        mock_chat.return_value = final_msg
        mock_gen.return_value = _GOOD_VERIFICATION

        result = await run_agent("Find routes", max_iterations=1)

    assert result.stopped_reason == "final_answer"
    assert result.verification.get("passed") is True
    assert isinstance(result.verification.get("rationale"), str)


@pytest.mark.asyncio
async def test_run_agent_verification_empty_on_max_iterations():
    """When run_agent stops at max_iterations, verification is not run — stays empty."""
    tool_call_msg = {
        "role": "assistant",
        "content": "Thinking...",
        "tool_calls": [{"function": {"name": "health_check", "arguments": {}}}],
    }

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner.tool_registry.execute_tool", new_callable=AsyncMock) as mock_exec, \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""), \
         patch("app.agent_runner.ollama_client.generate_reasoning", new_callable=AsyncMock) as mock_gen:
        mock_chat.return_value = tool_call_msg
        mock_exec.return_value = ToolResult(ok=True, data="ok")

        result = await run_agent("Any task", max_iterations=2)

    assert result.stopped_reason == "max_iterations"
    assert result.verification == {}
    mock_gen.assert_not_called()


# ── repair iteration ───────────────────────────────────────────────────────────

def test_build_repair_prompt_includes_claims_and_rationale():
    v = {
        "passed": False,
        "rationale": "No evidence for the websocket claim.",
        "unsupported_claims": ["websocket handler exists", "routes return JSON"],
    }
    prompt = _build_repair_prompt(v)
    assert "No evidence for the websocket claim." in prompt
    assert "websocket handler exists" in prompt
    assert "routes return JSON" in prompt
    assert "Use tools" in prompt


def test_build_repair_prompt_handles_empty_claims():
    v = {"passed": False, "rationale": "Answer is vague.", "unsupported_claims": []}
    prompt = _build_repair_prompt(v)
    assert "Answer is vague." in prompt
    assert "Candidates" not in prompt  # no claim list injected


@pytest.mark.asyncio
async def test_run_agent_repair_succeeds_updates_stopped_reason():
    """When first verification fails, repair pass fires and re-verification passes."""
    first_final = {"role": "assistant", "content": "Partial answer.", "tool_calls": []}
    repair_final = {"role": "assistant", "content": "Corrected answer.", "tool_calls": []}

    fail_verif = {"passed": False, "rationale": "Missing evidence.", "unsupported_claims": ["claim X"]}
    pass_verif = {"passed": True, "rationale": "Evidence found.", "unsupported_claims": [], "evidence_gap": False}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""), \
         patch("app.agent_runner._verify_answer", new_callable=AsyncMock) as mock_verify:
        mock_chat.side_effect = [first_final, repair_final]
        mock_verify.side_effect = [fail_verif, pass_verif]

        result = await run_agent("Find routes", max_iterations=5)

    assert result.stopped_reason == "final_answer"
    assert result.final_answer == "Corrected answer."
    assert result.verification.get("passed") is True
    assert result.verification.get("repaired") is True
    assert mock_verify.call_count == 2


@pytest.mark.asyncio
async def test_run_agent_verification_failed_when_repair_also_fails():
    """When repair pass also fails verification, stopped_reason is verification_failed."""
    first_final = {"role": "assistant", "content": "Bad answer.", "tool_calls": []}
    repair_final = {"role": "assistant", "content": "Still bad.", "tool_calls": []}

    fail_verif = {"passed": False, "rationale": "Still no evidence.", "unsupported_claims": ["claim X"]}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""), \
         patch("app.agent_runner._verify_answer", new_callable=AsyncMock, return_value=fail_verif):
        mock_chat.side_effect = [first_final, repair_final]

        result = await run_agent("Find routes", max_iterations=5)

    assert result.stopped_reason == "verification_failed"
    assert result.final_answer == "Still bad."
    assert result.verification.get("passed") is False
    assert result.verification.get("repaired") is True


@pytest.mark.asyncio
async def test_run_agent_no_repair_when_verification_passes():
    """When first verification passes, repair pass is never entered."""
    final_msg = {"role": "assistant", "content": "Good answer.", "tool_calls": []}
    pass_verif = {"passed": True, "rationale": "All good.", "unsupported_claims": [], "evidence_gap": False}

    with patch("app.agent_runner.ollama_client.chat_with_tools", new_callable=AsyncMock) as mock_chat, \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""), \
         patch("app.agent_runner._verify_answer", new_callable=AsyncMock, return_value=pass_verif) as mock_verify:
        mock_chat.return_value = final_msg

        result = await run_agent("Find routes", max_iterations=5)

    assert result.stopped_reason == "final_answer"
    assert result.verification.get("repaired") is not True
    mock_verify.assert_called_once()  # only the initial verification, no repair


@pytest.mark.asyncio
async def test_run_agent_repair_prompt_injected_as_user_message():
    """The repair prompt is injected as a user-role message before the repair loop."""
    first_final = {"role": "assistant", "content": "Incomplete.", "tool_calls": []}
    repair_final = {"role": "assistant", "content": "Fixed.", "tool_calls": []}

    fail_verif = {"passed": False, "rationale": "Missing data.", "unsupported_claims": ["missing claim"]}
    pass_verif = {"passed": True, "rationale": "Good now.", "unsupported_claims": [], "evidence_gap": False}

    captured_messages: list[list] = []

    async def capture_chat(messages, tools, model=None):
        captured_messages.append(list(messages))
        if len(captured_messages) == 1:
            return first_final
        return repair_final

    with patch("app.agent_runner.ollama_client.chat_with_tools", side_effect=capture_chat), \
         patch("app.agent_runner.tool_registry.get_tool_definitions", return_value=[]), \
         patch("app.agent_runner._preflight_memory", new_callable=AsyncMock, return_value=""), \
         patch("app.agent_runner._verify_answer", new_callable=AsyncMock,
               side_effect=[fail_verif, pass_verif]):
        await run_agent("Find routes", max_iterations=5)

    # The second chat call should have a user message containing the repair instructions
    second_call_messages = captured_messages[1]
    user_msgs = [m for m in second_call_messages if m.get("role") == "user"]
    repair_user_msgs = [m for m in user_msgs if "flagged" in m.get("content", "")]
    assert len(repair_user_msgs) == 1
    assert "missing claim" in repair_user_msgs[0]["content"]
