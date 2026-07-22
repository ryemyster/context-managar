"""
agent_runner.py — core agentic loop.

Coordinates inference.chat_with_tools() and tool_registry.execute_tool()
so the model can autonomously call tools until it has a final answer.

No FastAPI dependency — pure async Python. Import and await run_agent().
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from . import tool_registry
from .inference import inference
from .logger import log

_BASE_SYSTEM_PROMPT = (
    "You are a read-only code analysis agent. Use the enabled tools to gather "
    "real repository evidence before drawing conclusions. Never claim that you "
    "called a tool unless the runtime returned its result. Invoke tools through "
    "the native tool-call interface; do not describe a future call or print a "
    "JSON/XML imitation of one. You may provide a final answer only after at "
    "least one enabled tool has returned evidence. "
    f"Repository root: {config.REPO_ROOT}"
)


@dataclass
class AgentResult:
    final_answer: str
    iterations: int
    tool_calls_made: list[dict] = field(default_factory=list)
    # "final_answer" | "max_iterations" | "timeout" | "model_error" | "verification_failed"
    stopped_reason: str = "final_answer"
    message_history: list[dict] = field(default_factory=list)
    memory_context_used: bool = False
    memory_hits: int = 0
    plan_state: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)


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


def _normalize_tool_arguments(arguments: dict) -> dict:
    """Convert model-produced absolute repo paths to the public relative contract."""
    normalized = dict(arguments)
    for key in ("file", "path"):
        value = normalized.get(key)
        if not isinstance(value, str) or not value.startswith("/"):
            continue
        try:
            normalized[key] = str(Path(value).resolve().relative_to(config.REPO_ROOT))
        except ValueError:
            pass
    return normalized


def _serialize_tool_result(result: tool_registry.ToolResult) -> str:
    """Convert a ToolResult into the string that goes into the model's message history."""
    if result.ok:
        return result.data
    parts = [f"[{result.error_type}, retryable={result.retryable}] {result.data}"]
    if result.recovery_hint:
        parts.append(f"— {result.recovery_hint}")
    if result.candidates:
        parts.append(f"Candidates: {', '.join(result.candidates[:5])}")
    return " ".join(parts)


def _tool_names(tool_defs: list[dict]) -> list[str]:
    return [
        schema.get("function", {}).get("name", "")
        for schema in tool_defs
        if schema.get("function", {}).get("name")
    ]


def _build_system_prompt(tool_defs: list[dict], custom_prompt: str | None = None) -> str:
    """Build instructions that never reference tools excluded from this run."""
    names = _tool_names(tool_defs)
    parts = [custom_prompt.strip() if custom_prompt else _BASE_SYSTEM_PROMPT]
    parts.append(f"Enabled tools: {', '.join(names) if names else '(none)'}.")

    order = [
        name for name in (
            "search_memory",
            "update_plan",
            "scan_directory",
            "find_in_code",
            "grep",
            "read_file",
        )
        if name in names
    ]
    if order:
        parts.append(
            "Use only enabled tools. Suggested order when relevant: "
            + " -> ".join(order)
            + "."
        )
    if "read_file" in names:
        parts.append(
            "When the task supplies an exact repository-relative file path, "
            "call read_file directly; discovery is unnecessary."
        )
    if "update_plan" in names:
        parts.append(
            "Record a concise plan with update_plan before repository exploration."
        )
    return " ".join(parts)


def _recover_text_tool_calls(content: str, allowed_names: set[str]) -> list[dict]:
    """Recover valid enabled tool calls when a model prints JSON instead of native calls."""
    if not content or not allowed_names:
        return []

    decoder = json.JSONDecoder()
    parsed_objects: list[dict] = []
    for index, char in enumerate(content):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            parsed_objects.append(value)

    recovered: list[dict] = []
    for value in parsed_objects:
        candidates = value.get("tool_calls") if isinstance(value.get("tool_calls"), list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            function = candidate.get("function") if isinstance(candidate.get("function"), dict) else candidate
            name = function.get("name")
            arguments = _coerce_arguments(function.get("arguments", {}))
            if name in allowed_names:
                recovered.append({"function": {"name": name, "arguments": arguments}})
        if recovered:
            break
    return recovered


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
        model:          Ollama model name override (default: OLLAMA_AGENT_MODEL).
        allowed_scopes: Scope whitelist enforced on every tool call (None = all scopes).

    Returns AgentResult with final_answer, iteration count, full tool call trace, and plan_state.
    """
    max_iter  = max_iterations if max_iterations is not None else config.AGENT_MAX_ITERATIONS
    budget    = config.OLLAMA_AGENT_TIMEOUT
    if tools:
        unknown = [t for t in tools if t not in tool_registry.ALL_TOOLS]
        if unknown:
            raise ValueError(f"unknown tools: {unknown}. Available: {tool_registry.ALL_TOOLS}")
    available_tools = [
        name
        for name in (tools or tool_registry.ALL_TOOLS)
        if tool_registry.scopes_allow_tool(name, allowed_scopes)
    ]
    denied_tools = [
        name
        for name in (tools or tool_registry.ALL_TOOLS)
        if name not in available_tools
    ]
    if denied_tools:
        log.debug(
            "agent_runner filtered denied tools tools=%s allowed_scopes=%s",
            denied_tools,
            allowed_scopes,
        )
    tool_defs = tool_registry.get_tool_definitions(available_tools)
    system    = _build_system_prompt(tool_defs, system_prompt)

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user",   "content": task},
    ]

    # Pre-flight: inject prior memory context so the model starts informed.
    # Only runs when search_memory is in the allowed tool set (tools=None means all tools).
    memory_ctx = ""
    memory_hits = 0
    _search_memory_allowed = "search_memory" in available_tools
    if _search_memory_allowed:
        try:
            memory_ctx = await asyncio.wait_for(
                _preflight_memory(task, allowed_scopes=allowed_scopes),
                timeout=config.OLLAMA_AGENT_MEMORY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning(
                "agent_runner preflight_memory timeout limit=%.1fs",
                config.OLLAMA_AGENT_MEMORY_TIMEOUT,
            )
        if memory_ctx:
            memory_hits = memory_ctx.count("---") + 1
            messages[1]["content"] = task + "\n\n" + memory_ctx

    t_start = time.monotonic()

    log.info("agent_runner start task_len=%d tools=%d max_iter=%d memory=%s hits=%d",
             len(task), len(tool_defs), max_iter, "yes" if memory_ctx else "no", memory_hits)

    final_answer, stopped_reason, iterations_done, tool_calls_made, plan_state = await _execute_loop(
        messages, tool_defs, model, allowed_scopes, max_iter, t_start, budget
    )

    verification: dict = {}
    if stopped_reason == "final_answer" and final_answer:
        verification = await _verify_answer(task, final_answer, tool_calls_made, plan_state)
        log.info("agent_runner verifier passed=%s", verification.get("passed"))

        if verification.get("passed") is False:
            repair_prompt = _build_repair_prompt(verification)
            messages.append({"role": "user", "content": repair_prompt})
            log.info("agent_runner repair_pass starting claims=%d",
                     len(verification.get("unsupported_claims") or []))

            repair_answer, repair_reason, repair_iters, repair_calls, repair_plan = await _execute_loop(
                messages, tool_defs, model, allowed_scopes,
                max_iter=config.AGENT_MAX_REPAIR_ITERATIONS,
                t_start=t_start, budget=budget,
            )
            iterations_done += repair_iters
            tool_calls_made.extend(repair_calls)
            if repair_plan:
                plan_state = repair_plan
            if repair_answer:
                final_answer = repair_answer
                verification = await _verify_answer(task, final_answer, tool_calls_made, plan_state)
                verification["repaired"] = True
                log.info("agent_runner repair_verifier passed=%s", verification.get("passed"))

            if not verification.get("passed"):
                stopped_reason = "verification_failed"

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
        verification=verification,
    )


async def _execute_loop(
    messages: list[dict],
    tool_defs: list[dict],
    model: str | None,
    allowed_scopes: list[str] | None,
    max_iter: int,
    t_start: float,
    budget: float,
) -> tuple[str, str, int, list[dict], dict]:
    """
    Core think→act loop. Appends to messages in place.
    Returns (final_answer, stopped_reason, iterations_done, tool_calls_made, plan_state).
    """
    tool_calls_made: list[dict] = []
    plan_state: dict = {}
    final_answer = ""
    stopped_reason = "max_iterations"
    iterations_done = 0

    for i in range(max_iter):
        iterations_done = i + 1
        elapsed = time.monotonic() - t_start

        if elapsed >= budget:
            log.warning("agent_runner budget exceeded elapsed=%.1fs budget=%.1fs", elapsed, budget)
            stopped_reason = "timeout"
            final_answer = _last_assistant_content(messages) or "[agent stopped: time budget exceeded]"
            break

        remaining = budget - elapsed
        call_timeout = min(config.OLLAMA_AGENT_CALL_TIMEOUT, remaining)

        has_tool_evidence = any(msg.get("role") == "tool" for msg in messages)
        if tool_defs:
            selection_timeout = min(config.OLLAMA_AGENT_SELECT_TIMEOUT, remaining)
            selected = await inference.select_tool_call(
                messages,
                tool_defs,
                timeout=selection_timeout,
            )
            if selected.get("final_answer"):
                message = {
                    "role": "assistant",
                    "content": selected["final_answer"],
                    "tool_calls": [],
                }
                log.info("agent_runner structured final answer iter=%d", i + 1)
            elif "error" not in selected:
                selected_fn = selected.get("function", {})
                selected_name = selected_fn.get("name", "")
                selected_arguments = _normalize_tool_arguments(
                    _coerce_arguments(selected_fn.get("arguments", {}))
                )
                duplicate = any(
                    call.get("name") == selected_name
                    and call.get("arguments") == selected_arguments
                    and not str(call.get("result", "")).startswith("[")
                    for call in tool_calls_made
                )
                if duplicate:
                    synthesized = await inference.answer_from_evidence(
                        messages,
                        model=model,
                        timeout=selection_timeout,
                    )
                    if synthesized.get("final_answer"):
                        message = {
                            "role": "assistant",
                            "content": synthesized["final_answer"],
                            "tool_calls": [],
                        }
                        log.info(
                            "agent_runner synthesized answer after duplicate tool iter=%d tool=%s",
                            i + 1,
                            selected_name,
                        )
                    else:
                        message = {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [selected],
                        }
                else:
                    message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [selected],
                    }
                log.info(
                    "agent_runner structured tool selection iter=%d tool=%s",
                    i + 1,
                    selected_name,
                )
            else:
                log.warning(
                    "agent_runner primary tool selection failed iter=%d error=%s",
                    i + 1,
                    selected["error"],
                )
                message = await inference.chat_with_tools(
                    messages,
                    tool_defs,
                    model=model,
                    timeout=call_timeout,
                )
        else:
            message = await inference.chat_with_tools(
                messages,
                tool_defs,
                model=model,
                timeout=call_timeout,
            )

        log.debug(
            "agent_runner iter=%d elapsed=%.1fs messages=%d model_timeout=%.1fs",
            i + 1, elapsed, len(messages), call_timeout,
        )

        if "error" in message:
            log.error("agent_runner model error iter=%d: %s", i + 1, message["error"])
            stopped_reason = "model_error"
            final_answer = f"[agent stopped: {message['error']}]"
            break

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            tool_calls = _recover_text_tool_calls(
                message.get("content") or "",
                set(_tool_names(tool_defs)),
            )
            if tool_calls:
                log.warning(
                    "agent_runner recovered %d text-encoded tool call(s) iter=%d",
                    len(tool_calls),
                    i + 1,
                )
                message = {**message, "tool_calls": tool_calls}

        if not tool_calls and tool_defs and not has_tool_evidence:
            fallback_remaining = budget - (time.monotonic() - t_start)
            fallback_timeout = min(config.OLLAMA_AGENT_SELECT_TIMEOUT, fallback_remaining)
            if fallback_timeout > 0:
                selected = await inference.select_tool_call(
                    messages,
                    tool_defs,
                    model=model,
                    timeout=fallback_timeout,
                )
                if "error" not in selected:
                    tool_calls = [selected]
                    message = {**message, "tool_calls": tool_calls}
                    log.warning(
                        "agent_runner used structured tool selection iter=%d tool=%s",
                        i + 1,
                        selected.get("function", {}).get("name"),
                    )
                else:
                    log.warning(
                        "agent_runner structured tool selection failed iter=%d error=%s",
                        i + 1,
                        selected["error"],
                    )

        assistant_msg = dict(message)
        assistant_msg.setdefault("role", "assistant")
        messages.append(assistant_msg)

        if not tool_calls:
            if tool_defs and not has_tool_evidence:
                log.warning(
                    "agent_runner rejected unsupported final answer iter=%d",
                    i + 1,
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "No tool has returned evidence yet. Call one of the enabled "
                        "tools now through the native tool-call interface. Do not "
                        "explain or print the call as text."
                    ),
                })
                continue
            final_answer = (message.get("content") or "").strip()
            stopped_reason = "final_answer"
            log.info("agent_runner final_answer iter=%d dur=%.1fs", i + 1, time.monotonic() - t_start)
            break

        for tc in tool_calls:
            fn        = tc.get("function", {})
            name      = fn.get("name", "")
            arguments = _normalize_tool_arguments(
                _coerce_arguments(fn.get("arguments", {}))
            )
            log.debug("agent_runner tool name=%s args_keys=%s", name, list(arguments.keys()))

            result = await tool_registry.execute_tool(name, arguments, allowed_scopes=allowed_scopes)

            if name == "update_plan" and result.ok:
                try:
                    plan_state = json.loads(result.data)
                except Exception:
                    pass

            serialized = _serialize_tool_result(result)
            tool_calls_made.append({
                "name":      name,
                "arguments": arguments,
                "result":    serialized[:300],
            })
            messages.append({"role": "tool", "content": serialized})
    else:
        final_answer = (
            _last_assistant_content(messages)
            or f"[agent completed {max_iter} iterations without a conclusive answer]"
        )

    return final_answer, stopped_reason, iterations_done, tool_calls_made, plan_state


def _build_repair_prompt(verification: dict) -> str:
    """Build the repair injection message from a failed verification result."""
    parts = ["Your previous answer was flagged by the verifier."]
    rationale = verification.get("rationale", "")
    if rationale:
        parts.append(f"Reason: {rationale}")
    claims = verification.get("unsupported_claims") or []
    if claims:
        parts.append(f"Unsupported claims that need evidence: {'; '.join(claims)}")
    parts.append("Use tools to gather the missing evidence, then provide a corrected final answer.")
    return " ".join(parts)


async def _verify_answer(
    task: str,
    final_answer: str,
    tool_calls_made: list[dict],
    plan_state: dict,
) -> dict:
    """
    Post-hoc coherence check: does the final answer follow from the tool evidence?
    Never raises — returns {"passed": None, "error": "verifier_unavailable"} on any failure.
    """
    goal = plan_state.get("goal") or task

    evidence_lines = [f"- {tc['name']}: {tc['result'][:200]}" for tc in tool_calls_made]
    evidence_block = "\n".join(evidence_lines)
    if len(evidence_block) > 1500:
        evidence_block = evidence_block[:1500] + "\n[truncated]"
    if not evidence_block:
        evidence_block = "(no tool calls were made)"

    prompt = (
        "You are an evidence evaluator for an AI agent. "
        "Check whether the final answer is coherent with the task goal and supported by the tool evidence.\n\n"
        f"GOAL: {goal}\n\n"
        f"TOOL EVIDENCE:\n{evidence_block}\n\n"
        f"FINAL ANSWER:\n{final_answer}\n\n"
        "Return JSON only — no explanation outside the object:\n"
        "{\n"
        '  "passed": true | false,\n'
        '  "rationale": "one sentence explaining the verdict",\n'
        '  "unsupported_claims": ["claim that lacks evidence"],\n'
        '  "evidence_gap": true | false\n'
        "}"
    )

    try:
        parsed = await asyncio.wait_for(
            inference.verify_agent_answer(
                prompt,
                timeout=config.OLLAMA_AGENT_VERIFY_TIMEOUT,
            ),
            timeout=config.OLLAMA_AGENT_VERIFY_TIMEOUT,
        )
        if parsed.get("error"):
            error = parsed["error"]
            if error not in {"parse_failed", "verifier_timeout"}:
                error = "verifier_unavailable"
            return {"passed": None, "error": error}
        return {
            "passed":             bool(parsed.get("passed")),
            "rationale":          str(parsed.get("rationale", "")),
            "unsupported_claims": list(parsed.get("unsupported_claims") or []),
            "evidence_gap":       bool(parsed.get("evidence_gap", False)),
        }
    except asyncio.TimeoutError:
        log.warning(
            "agent_runner verifier timeout limit=%.1fs",
            config.OLLAMA_AGENT_VERIFY_TIMEOUT,
        )
        return {"passed": None, "error": "verifier_timeout"}
    except Exception as exc:
        log.debug("agent_runner verifier failed: %s", exc)
        return {"passed": None, "error": "verifier_unavailable"}


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
