"""Provider-neutral inference request and capability models."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    model: str
    timeout: float
    temperature: float = 0.1
    max_tokens: int = 400
    context_window: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatRequest:
    messages: list[dict[str, Any]]
    model: str
    timeout: float
    tools: list[dict[str, Any]] = field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    temperature: float = 0.1
    max_tokens: int = 400
    context_window: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmbeddingRequest:
    text: str
    model: str
    timeout: float = 90.0


@dataclass(frozen=True)
class ModelCapabilities:
    name: str
    chat: bool = True
    embeddings: bool = False
    vision: bool = False
    tools: bool = False
    json: bool = False
    streaming: bool = False
    context: int | None = None


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    models: list[ModelCapabilities] = field(default_factory=list)
