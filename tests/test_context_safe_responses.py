from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "context-engine"))

from app import main
from app.models import (
    DependenciesRequest,
    FindRequest,
    ReadRequest,
    RoutesRequest,
    ScanRequest,
    VectorSearchRequest,
)


@pytest.mark.asyncio
async def test_find_defaults_to_reference_summary(monkeypatch):
    monkeypatch.setattr(main, "find_in_repo", lambda *a, **k: [
        {
            "path": "owner/repo/app.py",
            "line_no": 7,
            "line": "def run_agent(task):",
            "context_before": ["before"],
            "context_after": ["after"],
        }
    ])
    monkeypatch.setattr(main, "read_file", lambda *a, **k: "def run_agent(task): pass")
    monkeypatch.setattr(main.ollama_client, "generate", AsyncMock(return_value="run_agent is implemented in app.py."))
    monkeypatch.setattr(main.mw, "write_find", lambda **k: "/tmp/find.md")
    monkeypatch.setattr(main.supabase_vector, "store_artifact", AsyncMock(return_value=None))

    response = await main.find(FindRequest(query="run_agent", path="owner/repo"))

    assert "results" in response
    assert "matches" not in response
    assert response["results"][0]["path"] == "owner/repo/app.py"
    assert response["metadata"]["detail_level"] == "summary"
    assert response["metadata"]["payload_bytes"] < 5000


@pytest.mark.asyncio
async def test_find_full_preserves_legacy_payload_with_warning(monkeypatch):
    monkeypatch.setattr(main, "find_in_repo", lambda *a, **k: [
        {"path": "owner/repo/app.py", "line_no": 7, "line": "def run_agent(task):"}
    ])
    monkeypatch.setattr(main, "read_file", lambda *a, **k: "def run_agent(task): pass")
    monkeypatch.setattr(main.ollama_client, "generate", AsyncMock(return_value="summary"))
    monkeypatch.setattr(main.mw, "write_find", lambda **k: "/tmp/find.md")
    monkeypatch.setattr(main.supabase_vector, "store_artifact", AsyncMock(return_value=None))

    response = await main.find(FindRequest(query="run_agent", path="owner/repo", detail="full"))

    assert response["matches"] == ["owner/repo/app.py"]
    assert response["files"] == ["owner/repo/app.py"]
    assert response["metadata"]["detail_level"] == "full"
    assert response["deprecation_warnings"]


@pytest.mark.asyncio
async def test_find_truncation_reports_when_more_matches_exist(monkeypatch):
    monkeypatch.setattr(main, "find_in_repo", lambda *a, **k: [
        {"path": "owner/repo/a.py", "line_no": 1, "line": "auth"},
        {"path": "owner/repo/b.py", "line_no": 2, "line": "auth"},
    ])
    monkeypatch.setattr(main, "read_file", lambda *a, **k: "auth")
    monkeypatch.setattr(main.ollama_client, "generate", AsyncMock(return_value="summary"))
    monkeypatch.setattr(main.mw, "write_find", lambda **k: "/tmp/find.md")
    monkeypatch.setattr(main.supabase_vector, "store_artifact", AsyncMock(return_value=None))

    response = await main.find(FindRequest(query="auth", path="owner/repo", max_results=1))

    assert len(response["results"]) == 1
    assert response["metadata"]["result_count"] == 2
    assert response["metadata"]["truncated"] is True


@pytest.mark.asyncio
async def test_scan_context_safe_applies_small_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "safe_resolve", lambda path: tmp_path)
    monkeypatch.setattr(main, "scan_directory", AsyncMock(return_value={
        "files": [f"owner/repo/file_{i}.py" for i in range(50)],
        "summary": "A useful summary.",
        "patterns": ["pattern"],
        "dependencies": ["fastapi"],
    }))
    monkeypatch.setattr(main.mw, "write_scan", lambda **k: "/tmp/scan.md")
    monkeypatch.setattr(main.supabase_vector, "store_artifact", AsyncMock(return_value=None))

    response = await main.scan(ScanRequest(path="owner/repo", mode="context_safe"))

    assert len(response["results"]) <= 8
    assert response["metadata"]["truncated"] is True
    assert response["metadata"]["payload_bytes"] <= 2400


@pytest.mark.asyncio
async def test_dependencies_summary_returns_references_not_graph(monkeypatch):
    monkeypatch.setattr(main, "map_dependencies", lambda path: {
        "internal": ["./local"],
        "external": ["fastapi"],
        "graph": {"owner/repo/app.py": ["fastapi", "./local"]},
    })
    monkeypatch.setattr(main.mw, "write_dependencies", lambda *a, **k: "/tmp/deps.md")
    monkeypatch.setattr(main.supabase_vector, "store_artifact", AsyncMock(return_value=None))

    response = await main.dependencies(DependenciesRequest(path="owner/repo"))

    assert response["results"][0]["type"] == "dependency_file"
    assert "graph" not in response
    assert response["metadata"]["result_count"] == 1


@pytest.mark.asyncio
async def test_routes_summary_returns_route_references(monkeypatch):
    monkeypatch.setattr(main, "extract_routes", lambda *a, **k: {
        "api_routes": [{"path": "owner/repo/app/api/route.ts", "methods": ["GET"]}],
        "page_routes": ["owner/repo/app/page.tsx"],
        "server_actions": [],
        "middleware": [],
        "auth_paths": [],
        "layouts": [],
    })
    monkeypatch.setattr(main, "read_file", lambda *a, **k: "export function GET() {}")
    monkeypatch.setattr(main.ollama_client, "generate", AsyncMock(return_value="GET route."))
    monkeypatch.setattr(main.mw, "write_routes", lambda *a, **k: "/tmp/routes.md")
    monkeypatch.setattr(main.supabase_vector, "store_artifact", AsyncMock(return_value=None))

    response = await main.routes(RoutesRequest())

    assert [r["type"] for r in response["results"]] == ["api_route", "page_route"]
    assert "api_routes" not in response


@pytest.mark.asyncio
async def test_vector_search_summary_omits_chunk_content(monkeypatch):
    monkeypatch.setattr(main.supabase_vector, "is_available", AsyncMock(return_value=True))
    monkeypatch.setattr(main.ollama_client, "embed", AsyncMock(return_value=[0.1, 0.2]))
    monkeypatch.setattr(main.supabase_vector, "search", AsyncMock(return_value=[
        {"path": "owner/repo/app.py", "chunk": "x" * 2000, "similarity": 0.82}
    ]))
    monkeypatch.setattr(main.mw, "write_vector_results", lambda *a, **k: "/tmp/vector.md")

    response = await main.vector_search(VectorSearchRequest(query="auth", mode="context_safe"))

    assert response["results"][0]["type"] == "vector_match"
    assert "matches" not in response
    assert len(response["results"][0]["summary"]) < 400


@pytest.mark.asyncio
async def test_vector_search_truncation_reports_when_more_matches_exist(monkeypatch):
    monkeypatch.setattr(main.supabase_vector, "is_available", AsyncMock(return_value=True))
    monkeypatch.setattr(main.ollama_client, "embed", AsyncMock(return_value=[0.1, 0.2]))
    monkeypatch.setattr(main.supabase_vector, "search", AsyncMock(return_value=[
        {"path": "owner/repo/a.py", "chunk": "first", "similarity": 0.9},
        {"path": "owner/repo/b.py", "chunk": "second", "similarity": 0.8},
    ]))
    monkeypatch.setattr(main.mw, "write_vector_results", lambda *a, **k: "/tmp/vector.md")

    response = await main.vector_search(VectorSearchRequest(query="auth", max_results=1))

    assert len(response["results"]) == 1
    assert response["metadata"]["result_count"] == 2
    assert response["metadata"]["truncated"] is True


@pytest.mark.asyncio
async def test_read_endpoint_returns_intentional_content(monkeypatch, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('hello')\n" * 50, encoding="utf-8")
    monkeypatch.setattr(main, "safe_resolve", lambda path: target)

    response = await main.read(ReadRequest(path="owner/repo/app.py", max_chars=40))

    assert response["path"] == "owner/repo/app.py"
    assert "content" in response
    assert response["metadata"]["truncated"] is True
