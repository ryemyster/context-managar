"""Ollama implementation of the provider contract."""

import httpx

from ...logger import log
from ..models import (
    ChatRequest,
    EmbeddingRequest,
    GenerationRequest,
    ModelCapabilities,
    ProviderCapabilities,
)


class OllamaProvider:
    name = "ollama"

    def __init__(self, endpoint: str, api_key: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(base_url=self.endpoint, headers=headers)
        return self._client

    async def generate(self, request: GenerationRequest) -> str:
        payload = {
            "model": request.model,
            "prompt": request.prompt,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        if request.context_window:
            payload["options"]["num_ctx"] = request.context_window
        payload.update(request.extra)
        response = await self.get_client().post(
            "/api/generate",
            json=payload,
            timeout=request.timeout,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()

    async def chat(self, request: ChatRequest) -> dict:
        payload = {
            "model": request.model,
            "messages": request.messages,
            "stream": False,
            "think": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        if request.tools:
            payload["tools"] = request.tools
        if request.response_schema:
            payload["format"] = request.response_schema
        if request.context_window:
            payload["options"]["num_ctx"] = request.context_window
        payload.update(request.extra)
        response = await self.get_client().post(
            "/api/chat",
            json=payload,
            timeout=request.timeout,
        )
        response.raise_for_status()
        return response.json().get("message", {})

    async def embed(self, request: EmbeddingRequest) -> list[float] | None:
        response = await self.get_client().post(
            "/api/embeddings",
            json={"model": request.model, "prompt": request.text},
            timeout=request.timeout,
        )
        response.raise_for_status()
        return response.json().get("embedding")

    async def list_models(self) -> list[str]:
        try:
            response = await self.get_client().get("/api/tags", timeout=5.0)
            if response.status_code == 200:
                return [model["name"] for model in response.json().get("models", [])]
        except Exception as exc:
            log.debug("ollama list_models failed: %s", exc)
        return []

    async def health(self) -> bool:
        return bool(await self.list_models())

    async def loaded_models(self) -> list[str]:
        try:
            response = await self.get_client().get("/api/ps", timeout=5.0)
            if response.status_code == 200:
                return [
                    model["name"]
                    for model in response.json().get("models", [])
                ]
        except Exception as exc:
            log.debug("ollama loaded_models failed: %s", exc)
        return []

    async def capabilities(self) -> ProviderCapabilities:
        models = await self.list_models()
        return ProviderCapabilities(
            provider=self.name,
            models=[
                ModelCapabilities(
                    name=model,
                    embeddings=True,
                    tools=True,
                    json=True,
                    streaming=True,
                )
                for model in models
            ],
        )
