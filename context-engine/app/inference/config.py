"""Inference configuration with backwards-compatible Ollama defaults."""

import os
from dataclasses import dataclass

from .. import config as app_config


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    endpoint: str
    api_key: str = ""


@dataclass(frozen=True)
class InferenceConfig:
    generation: ProviderConfig
    embedding: ProviderConfig
    fast_model: str
    reasoning_model: str
    agent_model: str
    selection_model: str
    verification_model: str
    embedding_model: str


def _env(name: str, fallback: str) -> str:
    """Use a non-empty override while allowing Compose to pass blank values."""
    return os.getenv(name) or fallback


def load_inference_config() -> InferenceConfig:
    generation_name = _env("INFERENCE_GENERATION_PROVIDER", "ollama").lower()
    embedding_name = _env("INFERENCE_EMBEDDING_PROVIDER", "ollama").lower()

    generation_endpoint = _env(
        "INFERENCE_GENERATION_ENDPOINT",
        app_config.OLLAMA_HOST,
    ).rstrip("/")
    embedding_endpoint = _env(
        "INFERENCE_EMBEDDING_ENDPOINT",
        app_config.OLLAMA_HOST,
    ).rstrip("/")

    return InferenceConfig(
        generation=ProviderConfig(
            name=generation_name,
            endpoint=generation_endpoint,
            api_key=os.getenv("INFERENCE_GENERATION_API_KEY") or "",
        ),
        embedding=ProviderConfig(
            name=embedding_name,
            endpoint=embedding_endpoint,
            api_key=os.getenv("INFERENCE_EMBEDDING_API_KEY") or "",
        ),
        fast_model=_env("INFERENCE_FAST_MODEL", app_config.OLLAMA_MODEL),
        reasoning_model=_env(
            "INFERENCE_REASONING_MODEL",
            app_config.OLLAMA_REASON_MODEL,
        ),
        agent_model=_env(
            "INFERENCE_AGENT_MODEL",
            app_config.OLLAMA_AGENT_MODEL,
        ),
        selection_model=_env(
            "INFERENCE_SELECTION_MODEL",
            app_config.OLLAMA_AGENT_SELECT_MODEL,
        ),
        verification_model=_env(
            "INFERENCE_VERIFICATION_MODEL",
            app_config.OLLAMA_AGENT_VERIFY_MODEL,
        ),
        embedding_model=_env(
            "INFERENCE_EMBEDDING_MODEL",
            app_config.OLLAMA_EMBED_MODEL,
        ),
    )
