import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "context-engine"))

from app import config, main
from app.metrics import metrics


def _reset_metrics() -> None:
    metrics.endpoint_metrics.clear()
    metrics.inference_metrics.clear()
    metrics.vector_metrics.clear()
    metrics.agent_metrics.clear()
    metrics.health_cache.clear()


@pytest.mark.asyncio
async def test_health_reports_round_trip_dependency_checks(monkeypatch):
    _reset_metrics()
    with tempfile.TemporaryDirectory() as tmp:
        original_output = config.OUTPUT_DIR
        config.OUTPUT_DIR = Path(tmp)
        try:
            monkeypatch.setattr(main.inference, "list_models", AsyncMock(return_value=[
                config.OLLAMA_MODEL,
                config.OLLAMA_REASON_MODEL,
                config.OLLAMA_AGENT_MODEL,
            ]))
            monkeypatch.setattr(main.inference, "list_embedding_models", AsyncMock(return_value=[
                config.OLLAMA_EMBED_MODEL
            ]))
            monkeypatch.setattr(main.inference, "embed", AsyncMock(return_value=[0.1, 0.2, 0.3]))
            monkeypatch.setattr(main.supabase_vector, "is_supabase_reachable", AsyncMock(return_value=True))
            monkeypatch.setattr(main.supabase_vector, "is_available", AsyncMock(return_value=True))
            monkeypatch.setattr(main.supabase_vector, "vector_row_count", AsyncMock(return_value=42))
            monkeypatch.setattr(main.supabase_vector, "search", AsyncMock(return_value=[{"path": "a"}]))

            body = await main.health()
        finally:
            config.OUTPUT_DIR = original_output

    assert body["status"] in {"ok", "degraded", "critical"}
    assert body["checks"]["ollama_embedding"]["round_trip"]["ok"] is True
    assert body["checks"]["vector_store"]["round_trip"]["ok"] is True
    assert body["checks"]["vector_store"]["row_count"] == 42
    assert body["checks"]["artifact_store"]["writable"] is True


@pytest.mark.asyncio
async def test_stats_returns_rolling_metrics_and_context_note_counts():
    _reset_metrics()
    metrics.record_request("GET", "/find", 200, 120)
    metrics.record_request("GET", "/find", 504, 900)
    metrics.record_inference(
        operation="embed",
        provider="ollama",
        model="nomic-embed-text",
        duration_ms=80,
        outcome="success",
    )
    metrics.record_inference(
        operation="generate",
        provider="ollama",
        model="qwen2.5-coder:3b",
        duration_ms=500,
        outcome="timeout",
    )
    metrics.record_vector(operation="supabase_search", duration_ms=45, outcome="success")
    metrics.record_agent_run(kind="agents/run", duration_ms=1500, outcome="timeout")
    metrics.update_health_probe("embedding_round_trip", {"ok": True, "latency_ms": 80})

    with tempfile.TemporaryDirectory() as tmp:
        original_output = config.OUTPUT_DIR
        config.OUTPUT_DIR = Path(tmp)
        try:
            records = config.OUTPUT_DIR / "records"
            records.mkdir(parents=True, exist_ok=True)
            (config.OUTPUT_DIR / "markdown").mkdir(parents=True, exist_ok=True)
            (config.OUTPUT_DIR / "events.jsonl").write_text("", encoding="utf-8")
            (records / "context_note-1.json").write_text(
                json.dumps(
                    {
                        "request": {
                            "repo": "ryemyster/ShaleYeah",
                            "scope": "repo",
                            "source": "github",
                        },
                        "response": {"warnings": []},
                    }
                ),
                encoding="utf-8",
            )

            body = await main.stats()
        finally:
            config.OUTPUT_DIR = original_output

    assert body["endpoints"]["GET /find"]["count"] == 2
    assert body["endpoints"]["GET /find"]["timeout_count"] == 1
    assert body["endpoints"]["GET /find"]["median_ms"] > 0
    assert body["ollama"]["embed:ollama:nomic-embed-text"]["success_count"] == 1
    assert body["ollama"]["generate:ollama:qwen2.5-coder:3b"]["timeout_count"] == 1
    assert body["vector"]["supabase_search"]["success_count"] == 1
    assert body["agent_runs"]["agents/run"]["timeout_count"] == 1
    assert body["context_notes"]["total"] == 1
    assert body["context_notes"]["by_scope"]["repo"] == 1
    assert "embedding_round_trip" in body["health_probes"]
