"""Deprecated compatibility facade for the former Ollama-specific boundary.

Production code imports ``app.inference``. This module remains temporarily for
third-party Python consumers that imported the old helpers directly.
"""

from .inference.service import inference, parse_json_response

generate = inference.generate
generate_reasoning = inference.generate_reasoning
embed = inference.embed
chat_with_tools = inference.chat_with_tools
select_tool_call = inference.select_tool_call
verify_agent_answer = inference.verify_agent_answer
answer_from_evidence = inference.answer_from_evidence
list_models = inference.list_models

__all__ = [
    "answer_from_evidence",
    "chat_with_tools",
    "embed",
    "generate",
    "generate_reasoning",
    "list_models",
    "parse_json_response",
    "select_tool_call",
    "verify_agent_answer",
]
