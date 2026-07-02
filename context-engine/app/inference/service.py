"""Provider-neutral inference service and task-role routing."""

import json
import re
import time

import httpx

from .. import config as app_config
from ..logger import log
from .config import InferenceConfig, load_inference_config
from .factory import create_provider
from .models import ChatRequest, EmbeddingRequest, GenerationRequest
from .provider import InferenceProvider


class InferenceService:
    def __init__(self, settings: InferenceConfig | None = None):
        self.settings = settings or load_inference_config()
        self.generation_provider: InferenceProvider = create_provider(
            self.settings.generation
        )
        if self.settings.embedding == self.settings.generation:
            self.embedding_provider = self.generation_provider
        else:
            self.embedding_provider = create_provider(self.settings.embedding)

    async def generate(self, prompt: str) -> str:
        request = GenerationRequest(
            prompt=prompt,
            model=self.settings.fast_model,
            timeout=app_config.OLLAMA_TIMEOUT,
            temperature=0.1,
            max_tokens=app_config.OLLAMA_NUM_PREDICT,
            context_window=app_config.OLLAMA_NUM_CTX,
        )
        return await self._generate(request, "generate")

    async def generate_reasoning(self, prompt: str) -> str:
        request = GenerationRequest(
            prompt=prompt,
            model=self.settings.reasoning_model,
            timeout=app_config.OLLAMA_REASON_TIMEOUT,
            temperature=0.2,
            max_tokens=app_config.OLLAMA_REASON_PREDICT,
            context_window=app_config.OLLAMA_NUM_CTX,
        )
        return await self._generate(request, "reasoning")

    async def _generate(self, request: GenerationRequest, role: str) -> str:
        log.debug(
            "inference %s provider=%s model=%s prompt_len=%d",
            role,
            self.generation_provider.name,
            request.model,
            len(request.prompt),
        )
        started = time.monotonic()
        try:
            result = await self.generation_provider.generate(request)
            log.debug(
                "inference %s done provider=%s dur=%.2fs response_len=%d",
                role,
                self.generation_provider.name,
                time.monotonic() - started,
                len(result),
            )
            return result
        except httpx.TimeoutException:
            log.warning(
                "inference %s timeout provider=%s model=%s dur=%.2fs",
                role,
                self.generation_provider.name,
                request.model,
                time.monotonic() - started,
            )
            qualifier = "reasoning " if role == "reasoning" else ""
            return f"[timeout — {qualifier}prompt may be too long, try a smaller path]"
        except Exception as exc:
            log.error("inference %s error: %s", role, exc)
            return f"[model error: {exc}]"

    async def embed(self, text: str) -> list[float] | None:
        started = time.monotonic()
        try:
            result = await self.embedding_provider.embed(
                EmbeddingRequest(
                    text=text,
                    model=self.settings.embedding_model,
                    timeout=90.0,
                )
            )
            log.debug(
                "inference embed done provider=%s dur=%.2fs dims=%d",
                self.embedding_provider.name,
                time.monotonic() - started,
                len(result) if result else 0,
            )
            return result
        except Exception as exc:
            log.warning(
                "inference embed failed provider=%s dur=%.2fs: %s",
                self.embedding_provider.name,
                time.monotonic() - started,
                exc,
            )
            return None

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        timeout: float | None = None,
        response_schema: dict | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        request_timeout = timeout or app_config.OLLAMA_AGENT_CALL_TIMEOUT
        try:
            return await self.generation_provider.chat(
                ChatRequest(
                    messages=messages,
                    tools=tools or [],
                    model=model or self.settings.agent_model,
                    timeout=request_timeout,
                    response_schema=response_schema,
                    temperature=temperature,
                    max_tokens=max_tokens or app_config.OLLAMA_AGENT_NUM_PREDICT,
                    context_window=app_config.OLLAMA_NUM_CTX,
                )
            )
        except httpx.TimeoutException:
            return {"error": f"timeout after {request_timeout:.0f}s — model took too long"}
        except Exception as exc:
            log.error("inference chat error: %s", exc)
            return {"error": str(exc)}

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        return await self.chat(
            messages,
            tools=tools,
            model=model or self.settings.selection_model,
            timeout=timeout,
        )

    async def select_tool_call(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        names = [
            tool.get("function", {}).get("name")
            for tool in tools
            if tool.get("function", {}).get("name")
        ]
        if not names:
            return {"error": "no enabled tools"}
        conversation = [
            {
                "role": message.get("role", ""),
                "content": str(message.get("content") or "")[:2500],
            }
            for message in messages[-8:]
        ]
        compact_tools = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "parameters": tool["function"].get("parameters", {}),
            }
            for tool in tools
            if tool.get("function", {}).get("name")
        ]
        prompt = (
            f"Conversation and evidence:\n{json.dumps(conversation, separators=(',', ':'))}\n\n"
            f"Enabled tools:\n{json.dumps(compact_tools, separators=(',', ':'))}\n\n"
            "Choose the next action. Use call_tool only when more repository evidence "
            "is required. Use final_answer when the existing tool evidence is enough. "
            "Never repeat an identical successful tool call."
        )
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["call_tool", "final_answer"],
                },
                "name": {"type": "string", "enum": names},
                "arguments": {"type": "object"},
                "final_answer": {"type": "string"},
            },
            "required": ["action", "name", "arguments", "final_answer"],
            "additionalProperties": False,
        }
        request_timeout = timeout or app_config.OLLAMA_AGENT_SELECT_TIMEOUT
        message = await self.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Choose the next repository-agent action. Return only "
                        "the JSON object required by the response schema."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            # Preserve the existing structured selector route. The lighter
            # selection model remains the native tool-call fallback route.
            model=model or self.settings.agent_model,
            timeout=request_timeout,
            response_schema=schema,
            temperature=0,
            max_tokens=200,
        )
        if message.get("error"):
            error = message["error"]
            if str(error).startswith("timeout after"):
                return {
                    "error": (
                        "structured tool selection timed out after "
                        f"{request_timeout:.0f}s"
                    )
                }
            return {"error": error}
        parsed = parse_json_response(str(message.get("content") or ""))
        if parsed.get("action") == "final_answer":
            final_answer = str(parsed.get("final_answer") or "").strip()
            return (
                {"final_answer": final_answer}
                if final_answer
                else {"error": "structured action returned an empty final answer"}
            )
        name = parsed.get("name")
        arguments = parsed.get("arguments")
        if (
            parsed.get("action") != "call_tool"
            or name not in names
            or not isinstance(arguments, dict)
        ):
            return {"error": "structured tool selection returned an invalid call"}
        return {"function": {"name": name, "arguments": arguments}}

    async def verify_agent_answer(
        self,
        prompt: str,
        model: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        schema = {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "rationale": {"type": "string"},
                "unsupported_claims": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "evidence_gap": {"type": "boolean"},
            },
            "required": [
                "passed",
                "rationale",
                "unsupported_claims",
                "evidence_gap",
            ],
            "additionalProperties": False,
        }
        message = await self.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Evaluate whether the answer is supported by the "
                        "provided tool evidence. Return only the required JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            model=model or self.settings.verification_model,
            timeout=timeout or app_config.OLLAMA_AGENT_VERIFY_TIMEOUT,
            response_schema=schema,
            temperature=0,
            max_tokens=200,
        )
        if message.get("error"):
            return {
                "error": (
                    "verifier_timeout"
                    if str(message["error"]).startswith("timeout after")
                    else message["error"]
                )
            }
        parsed = parse_json_response(str(message.get("content") or ""))
        return parsed if "passed" in parsed else {"error": "parse_failed"}

    async def answer_from_evidence(
        self,
        messages: list[dict],
        model: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        conversation = [
            {
                "role": message.get("role", ""),
                "content": str(message.get("content") or "")[:3000],
            }
            for message in messages[-8:]
        ]
        schema = {
            "type": "object",
            "properties": {"final_answer": {"type": "string"}},
            "required": ["final_answer"],
            "additionalProperties": False,
        }
        message = await self.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Answer the repository task using only the accumulated "
                        "tool evidence. Be concise and cite repository-relative paths. "
                        "Return only the required JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(conversation, separators=(",", ":")),
                },
            ],
            model=model or self.settings.agent_model,
            timeout=timeout or app_config.OLLAMA_AGENT_SELECT_TIMEOUT,
            response_schema=schema,
            temperature=0,
        )
        if message.get("error"):
            return {"error": message["error"]}
        parsed = parse_json_response(str(message.get("content") or ""))
        answer = str(parsed.get("final_answer") or "").strip()
        return {"final_answer": answer} if answer else {"error": "empty final answer"}

    async def list_models(self) -> list[str]:
        return await self.generation_provider.list_models()

    async def list_embedding_models(self) -> list[str]:
        return await self.embedding_provider.list_models()

    async def loaded_embedding_models(self) -> list[str]:
        return await self.embedding_provider.loaded_models()

    async def health(self) -> dict:
        return {
            "generation": await self.generation_provider.health(),
            "embedding": await self.embedding_provider.health(),
        }

    async def capabilities(self) -> dict:
        generation = await self.generation_provider.capabilities()
        embedding = await self.embedding_provider.capabilities()
        return {
            "generation": generation,
            "embedding": embedding,
        }

    @staticmethod
    def parse_json_response(text: str) -> dict:
        return parse_json_response(text)


def parse_json_response(text: str) -> dict:
    """Extract the first JSON object from a model response."""
    stripped = re.sub(r"```(?:json)?\s*", "", text).strip()
    for candidate in (stripped, text):
        try:
            match = re.search(r"\{.*\}", candidate, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception:
            continue
    return {}


inference = InferenceService()
