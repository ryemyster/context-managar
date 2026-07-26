"""Common provider contract."""

from typing import Protocol

from .models import (
    ChatRequest,
    EmbeddingRequest,
    GenerationRequest,
    ProviderCapabilities,
)


class InferenceProvider(Protocol):
    name: str

    async def generate(self, request: GenerationRequest) -> str: ...

    async def chat(self, request: ChatRequest) -> dict: ...

    async def embed(self, request: EmbeddingRequest) -> list[float] | None: ...

    async def health(self) -> bool: ...

    async def list_models(self) -> list[str]: ...

    async def loaded_models(self) -> list[str]: ...

    async def capabilities(self) -> ProviderCapabilities: ...
