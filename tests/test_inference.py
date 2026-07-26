"""Focused tests for inference providers and role routing."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import config
from app.inference.config import InferenceConfig, ProviderConfig, load_inference_config
from app.inference.models import ChatRequest, EmbeddingRequest, GenerationRequest
from app.inference.providers.ollama import OllamaProvider
from app.inference.providers.openai_compatible import OpenAICompatibleProvider
from app.inference.service import InferenceService


def settings(
    generation: str = "ollama",
    embedding: str = "ollama",
) -> InferenceConfig:
    return InferenceConfig(
        generation=ProviderConfig(generation, "https://generation.example", "secret"),
        embedding=ProviderConfig(embedding, "http://localhost:11434"),
        fast_model="fast-model",
        reasoning_model="reason-model",
        agent_model="agent-model",
        selection_model="select-model",
        verification_model="verify-model",
        embedding_model="nomic-embed-text",
    )


def test_blank_new_configuration_falls_back_to_legacy_values(monkeypatch):
    for name in (
        "INFERENCE_GENERATION_PROVIDER",
        "INFERENCE_GENERATION_ENDPOINT",
        "INFERENCE_FAST_MODEL",
        "INFERENCE_EMBEDDING_PROVIDER",
        "INFERENCE_EMBEDDING_ENDPOINT",
        "INFERENCE_EMBEDDING_MODEL",
    ):
        monkeypatch.setenv(name, "")

    loaded = load_inference_config()
    assert loaded.generation.name == "ollama"
    assert loaded.generation.endpoint == config.OLLAMA_HOST.rstrip("/")
    assert loaded.fast_model == config.OLLAMA_MODEL
    assert loaded.embedding.endpoint == config.OLLAMA_HOST.rstrip("/")
    assert loaded.embedding_model == config.OLLAMA_EMBED_MODEL


@pytest.mark.asyncio
async def test_service_routes_generation_and_embeddings_independently():
    service = InferenceService(settings("openai_compatible", "ollama"))
    service.generation_provider.generate = AsyncMock(return_value="generated")
    service.embedding_provider.embed = AsyncMock(return_value=[0.1, 0.2])

    assert await service.generate("prompt") == "generated"
    assert await service.embed("text") == [0.1, 0.2]

    generation = service.generation_provider.generate.await_args.args[0]
    embedding = service.embedding_provider.embed.await_args.args[0]
    assert generation.model == "fast-model"
    assert embedding.model == "nomic-embed-text"


@pytest.mark.asyncio
async def test_service_reasoning_uses_reasoning_role():
    service = InferenceService(settings())
    service.generation_provider.generate = AsyncMock(return_value="analysis")

    assert await service.generate_reasoning("diff") == "analysis"
    request = service.generation_provider.generate.await_args.args[0]
    assert request.model == "reason-model"
    assert request.temperature == 0.2
    assert request.max_tokens == config.OLLAMA_REASON_PREDICT


@pytest.mark.asyncio
async def test_service_uses_selection_model_for_selector_and_fallback_routes():
    service = InferenceService(settings())
    service.chat = AsyncMock(
        return_value={
            "content": (
                '{"action":"final_answer","name":"read_file",'
                '"arguments":{},"final_answer":"done"}'
            )
        }
    )
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    result = await service.select_tool_call([], tools)
    assert result == {"final_answer": "done"}
    assert service.chat.await_args.kwargs["model"] == "select-model"

    await service.chat_with_tools([], tools)
    assert service.chat.await_args.kwargs["model"] == "select-model"


@pytest.mark.asyncio
async def test_service_selector_accepts_native_tool_call_response():
    service = InferenceService(settings())
    service.chat = AsyncMock(
        return_value={
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": "repo_browser.read_file",
                    "arguments": {"file": "owner/repo/app.py"},
                },
            }],
        }
    )
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    result = await service.select_tool_call([], tools)

    assert result == {
        "function": {
            "name": "read_file",
            "arguments": {"file": "owner/repo/app.py"},
        },
    }


@pytest.mark.asyncio
async def test_service_selector_accepts_nested_tool_call_json():
    service = InferenceService(settings())
    service.chat = AsyncMock(
        return_value={
            "content": (
                '{"tool_call":{"name":"read_file",'
                '"arguments":{"file":"owner/repo/app.py"}}}'
            )
        }
    )
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    result = await service.select_tool_call([], tools)

    assert result == {
        "function": {
            "name": "read_file",
            "arguments": {"file": "owner/repo/app.py"},
        },
    }


@pytest.mark.asyncio
async def test_service_verifier_accepts_supported_alias():
    service = InferenceService(settings())
    service.chat = AsyncMock(return_value={"content": '{"supported": true}'})

    result = await service.verify_agent_answer("prompt")

    assert result["passed"] is True
    assert result["unsupported_claims"] == []
    assert result["evidence_gap"] is False


@pytest.mark.asyncio
async def test_service_answer_from_evidence_uses_fast_model_and_preserves_task():
    service = InferenceService(settings())
    service.chat = AsyncMock(return_value={"content": "The first heading is # context-engine."})

    result = await service.answer_from_evidence([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Tell me the first heading only."},
        {"role": "tool", "content": "# context-engine\n\nbody"},
    ])

    assert result == {"final_answer": "The first heading is # context-engine."}
    assert service.chat.await_args.kwargs["model"] == "fast-model"
    prompt = service.chat.await_args.args[0][1]["content"]
    assert "Tell me the first heading only." in prompt
    assert "Answer exactly the task" in prompt


@pytest.mark.asyncio
async def test_service_preserves_generation_timeout_shape():
    service = InferenceService(settings())
    service.generation_provider.generate = AsyncMock(
        side_effect=httpx.ReadTimeout("slow")
    )

    result = await service.generate("prompt")
    assert result == "[timeout — prompt may be too long, try a smaller path]"


@pytest.mark.asyncio
async def test_ollama_provider_translates_generate_request():
    provider = OllamaProvider("http://localhost:11434")
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: {"response": " done "}
    client = AsyncMock()
    client.post.return_value = response

    with patch.object(provider, "get_client", return_value=client):
        result = await provider.generate(
            GenerationRequest(
                prompt="hello",
                model="qwen",
                timeout=12,
                context_window=8192,
            )
        )

    assert result == "done"
    payload = client.post.await_args.kwargs["json"]
    assert payload["model"] == "qwen"
    assert payload["options"]["num_ctx"] == 8192
    assert client.post.await_args.kwargs["timeout"] == 12


@pytest.mark.asyncio
async def test_ollama_provider_uses_existing_embedding_contract():
    provider = OllamaProvider("http://localhost:11434")
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: {"embedding": [0.1, 0.2]}
    client = AsyncMock()
    client.post.return_value = response

    with patch.object(provider, "get_client", return_value=client):
        result = await provider.embed(
            EmbeddingRequest("source", "nomic-embed-text", 90)
        )

    assert result == [0.1, 0.2]
    assert client.post.await_args.args[0] == "/api/embeddings"
    assert client.post.await_args.kwargs["json"]["prompt"] == "source"


@pytest.mark.asyncio
async def test_openai_compatible_provider_normalizes_tool_arguments():
    provider = OpenAICompatibleProvider("https://cloud.example/v1", "key")
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"src/app.py"}',
                            }
                        }
                    ],
                }
            }
        ]
    }
    client = AsyncMock()
    client.post.return_value = response

    with patch.object(provider, "get_client", return_value=client):
        message = await provider.chat(
            ChatRequest(
                messages=[{"role": "user", "content": "inspect"}],
                tools=[{"type": "function", "function": {"name": "read_file"}}],
                model="cloud-model",
                timeout=30,
            )
        )

    assert message["tool_calls"][0]["function"]["arguments"] == {
        "path": "src/app.py"
    }
    assert client.post.await_args.args[0] == "/chat/completions"
    assert client.post.await_args.kwargs["json"]["model"] == "cloud-model"


@pytest.mark.asyncio
async def test_openai_compatible_provider_translates_json_schema():
    provider = OpenAICompatibleProvider("https://cloud.example/v1", "key")
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: {
        "choices": [{"message": {"content": '{"passed":true}'}}]
    }
    client = AsyncMock()
    client.post.return_value = response
    schema = {
        "type": "object",
        "properties": {"passed": {"type": "boolean"}},
        "required": ["passed"],
    }

    with patch.object(provider, "get_client", return_value=client):
        await provider.chat(
            ChatRequest(
                messages=[{"role": "user", "content": "verify"}],
                model="cloud-model",
                timeout=30,
                response_schema=schema,
            )
        )

    response_format = client.post.await_args.kwargs["json"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == schema
    assert response_format["json_schema"]["strict"] is False
