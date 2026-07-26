"""Provider construction and validation."""

from .config import ProviderConfig
from .provider import InferenceProvider
from .providers import OllamaProvider, OpenAICompatibleProvider


def create_provider(config: ProviderConfig) -> InferenceProvider:
    if config.name == "ollama":
        return OllamaProvider(config.endpoint, config.api_key)
    if config.name in {"openai", "openai_compatible", "vllm", "openrouter"}:
        return OpenAICompatibleProvider(config.endpoint, config.api_key)
    raise ValueError(f"unsupported inference provider: {config.name!r}")
