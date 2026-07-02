"""Provider for cloud endpoints implementing the OpenAI API specification."""

import httpx

from ...logger import log
from ..models import (
    ChatRequest,
    EmbeddingRequest,
    GenerationRequest,
    ModelCapabilities,
    ProviderCapabilities,
)


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, endpoint: str, api_key: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(base_url=self.endpoint, headers=headers)
        return self._client

    @staticmethod
    def _message(payload: dict) -> dict:
        choices = payload.get("choices") or []
        if not choices:
            return {}
        message = dict(choices[0].get("message") or {})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    import json

                    function["arguments"] = json.loads(arguments)
                except Exception:
                    pass
        return message

    async def generate(self, request: GenerationRequest) -> str:
        message = await self.chat(
            ChatRequest(
                messages=[{"role": "user", "content": request.prompt}],
                model=request.model,
                timeout=request.timeout,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                extra=request.extra,
            )
        )
        return str(message.get("content") or "").strip()

    async def chat(self, request: ChatRequest) -> dict:
        payload = {
            "model": request.model,
            "messages": request.messages,
            "stream": False,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = request.tools
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "context_engine_response",
                    # Tool arguments are dynamic objects whose properties come
                    # from the enabled tool definitions. Service-side validation
                    # preserves safety without falsely claiming a strict schema.
                    "strict": False,
                    "schema": request.response_schema,
                },
            }
        payload.update(request.extra)
        response = await self.get_client().post(
            "/chat/completions",
            json=payload,
            timeout=request.timeout,
        )
        response.raise_for_status()
        return self._message(response.json())

    async def embed(self, request: EmbeddingRequest) -> list[float] | None:
        response = await self.get_client().post(
            "/embeddings",
            json={"model": request.model, "input": request.text},
            timeout=request.timeout,
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        return data[0].get("embedding") if data else None

    async def list_models(self) -> list[str]:
        try:
            response = await self.get_client().get("/models", timeout=5.0)
            if response.status_code == 200:
                return [model["id"] for model in response.json().get("data", [])]
        except Exception as exc:
            log.debug("openai-compatible list_models failed: %s", exc)
        return []

    async def health(self) -> bool:
        return bool(await self.list_models())

    async def loaded_models(self) -> list[str]:
        return []

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            models=[
                ModelCapabilities(
                    name=model,
                    tools=True,
                    json=True,
                    streaming=True,
                )
                for model in await self.list_models()
            ],
        )
