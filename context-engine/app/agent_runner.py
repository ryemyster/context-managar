"""
agent_runner.py — core agentic loop.

Coordinates ollama_client.chat_with_tools() and tool_registry.execute_tool()
so the model can autonomously call tools until it has a final answer.

No FastAPI dependency — pure async Python. Import and await run_agent().
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import config
from . import ollama_client, tool_registry
from .logger import log

_DEFAULT_SYSTEM_PROMPT = (
    "You are a code analysis agent with tools to explore a software repository. "
    "Use tools to gather real evidence before drawing conclusions — do not guess. "
    "Recommended call order: search_memory (check prior runs first) → update_plan (record your plan) "
    "→ scan_directory → find_in_code or grep → read_file. "
    "Always call search_memory first — if a similar task was run before, use those results as a starting point. "
    "After search_memory, call update_plan to record your goal and steps before exploring. "
    "When you have enough evidence, provide a final answer without calling more tools. "
    f"Repository root: {config.REPO_ROOT}"
)


@dataclass
class AgentResult:
    final_answer: str
    iterations: int
    tool_calls_made: list[dict] = field(default_factory=list)
    # "final_answer" | "max_iterations" | "timeout" | "model_error"
    stopped_reason: str = "final_answer"
    message_history: list[dict] = field(default_factory=list)
    memory_context_used: bool = False
    memory_hits: int = 0
    plan_state: dict = field(default_factory=dict)


def _coerce_arguments(raw) -> dict:
    """Normalize tool call arguments — Ollama sometimes double-encodes them as a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _serialize_tool_result(result: tool_registry.ToolResult) -> str:
    """Convert a ToolResult into the string that goes into the model's message history."""
    if result.ok:
        return result.data
    parts = [f"[{result.error_type}, retryable={result.retryable}] {result.data}"]
    if result.recovery_hint:
        parts.append(f"— {result.recovery_hint}")
    return " ".join(parts)


async def run_agent(
    task: str,
    tools: list[str] | None = None,
    system_prompt: str | None = None,
    max_iterations: int | None = None,
    model: str | None = None,
    allowed_scopes: list[str] | None = None,
) -> AgentResult:
    """
    Run the agentic loop for a task.

    The model receives the task + tool definitions, then loops:
      think → call tool(s) → observe result → repeat until final answer or stop condition.

    Args:
        task:           What the agent should accomplish.
        tools:          Tool names to enable (None = all tools).
        system_prompt:  Override the default system prompt.
        max_iterations: Max think→act cycles (default: AGENT_MAX_ITERATIONS).
        model:          Ollama model name override (default: OLLAMA_REASON_MODEL).
        allowed_scopes: Scope whitelist enforced on every tool call (None = all scopes).

    Returns AgentResult with final_answer, iteration count, full tool call trace, and plan_state.
    """
    max_iter  = max_iterations if max_iterations is not None else config.AGENT_MAX_ITERATIONS
    budget    = config.OLLAMA_AGENT_TIMEOUT
    tool_defs = tool_registry.get_tool_definitions(tools)
    system    = system_prompt or _DEFAULT_SYSTEM_PROMPT

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user",   "content": task},
    ]

    # Pre-flight: inject prior memory context so the model starts informed.
    # Only runs when search_memory is in the allowed tool set (tools=None means all tools).
    memory_ctx = ""
    memory_hits = 0
    _search_memory_allowed = tools is None or "search_memory" in tools
    if _search_memory_allowed:
        memory_ctx = await _preflight_memory(task, allowed_scopes=allowed_scopes)
        if memory_ctx:
            memory_hits = memory_ctx.count("---") + 1
            messages[1]["content"] = task + "\n\n" + memory_ctx

    tool_calls_made: list[dict] = []
    plan_state: dict = {}
    t_start = time.monotonic()
    final_answer  = ""
    stopped_reason = "max_iterations"
    iterations_done = 0

    log.info("agent_runner start task_len=%d tools=%d max_iter=%d memory=%s hits=%d",
             len(task), len(tool_defs), max_iter, "yes" if memory_ctx else "no", memory_hits)

    for i in range(max_iter):
        iterations_done = i + 1
        elapsed = time.monotonic() - t_start

        if elapsed >= budget:
            log.warning("agent_runner budget exceeded elapsed=%.1fs budget=%.1fs", elapsed, budget)
            stopped_reason = "timeout"
            final_answer = _last_assistant_content(messages) or "[agent stopped: time budget exceeded]"
            break

        log.debug("agent_runner iter=%d elapsed=%.1fs messages=%d", i + 1, elapsed, len(messages))
        message = await ollama_client.chat_with_tools(messages, tool_defs, model=model)

        if "error" in message:
            log.error("agent_runner model error iter=%d: %s", i + 1, message["error"])
            stopped_reason = "model_error"
            final_answer   = f"[agent stopped: {message['error']}]"
            break

        # Append the assistant turn (normalize role field)
        assistant_msg = dict(message)
        assistant_msg.setdefault("role", "assistant")
        messages.append(assistant_msg)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            final_answer   = (message.get("content") or "").strip()
            stopped_reason = "final_answer"
            log.info("agent_runner final_answer iter=%d dur=%.1fs", i + 1, time.monotonic() - t_start)
            break

        # Execute each tool call and feed results back as tool messages
        for tc in tool_calls:
            fn        = tc.get("function", {})
            name      = fn.get("name", "")
            arguments = _coerce_arguments(fn.get("arguments", {}))
            log.debug("agent_runner tool name=%s args_keys=%s", name, list(arguments.keys()))

            result = await tool_registry.execute_tool(name, arguments, allowed_scopes=allowed_scopes)

            # Capture plan state whenever the model calls update_plan
            if name == "update_plan" and result.ok:
                try:
                    plan_state = json.loads(result.data)
                except Exception:
                    pass

            serialized = _serialize_tool_result(result)
            tool_calls_made.append({
                "name":      name,
                "arguments": arguments,
                "result":    serialized[:300],   # truncate trace entry; full result is in messages
            })
            messages.append({"role": "tool", "content": serialized})

    else:
        # for-loop exhausted without break → max_iterations hit
        final_answer = _last_assistant_content(messages) or f"[agent completed {max_iter} iterations without a conclusive answer]"

    log.info("agent_runner done stopped=%s iterations=%d tool_calls=%d dur=%.1fs",
             stopped_reason, iterations_done, len(tool_calls_made), time.monotonic() - t_start)

    return AgentResult(
        final_answer=final_answer,
        iterations=iterations_done,
        tool_calls_made=tool_calls_made,
        stopped_reason=stopped_reason,
        message_history=messages,
        memory_context_used=bool(memory_ctx),
        memory_hits=memory_hits,
        plan_state=plan_state,
    )


def _last_assistant_content(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            return msg["content"]
    return ""


async def _preflight_memory(task: str, allowed_scopes: list[str] | None = None) -> str:
    """Query prior memory for the task and return formatted context, or empty string if none."""
    try:
        result = await tool_registry.execute_tool(
            "search_memory", {"query": task, "limit": 3}, allowed_scopes=allowed_scopes
        )
        if not result.ok or not result.data.strip() or result.data.startswith("[no memory"):
            return ""
        return f"[Prior memory — relevant results from past runs]\n{result.data}"
    except Exception as exc:
        log.debug("agent_runner preflight_memory failed: %s", exc)
        return ""
