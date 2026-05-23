"""
models.py — Pydantic request/response models for all endpoints.
"""

from typing import Optional
from pydantic import BaseModel


# ── Requests ───────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    path: str = ""

class FindRequest(BaseModel):
    query: str
    path: str = "."

class DependenciesRequest(BaseModel):
    path: str = "."

class SummarizeRequest(BaseModel):
    file: str

class ContextRequest(BaseModel):
    task: str
    paths: list[str] = ["."]
    focus: list[str] = []

class DiffRequest(BaseModel):
    diff: str

class VectorSearchRequest(BaseModel):
    query: str
    limit: int = 8

class IndexRequest(BaseModel):
    paths: list[str] = ["."]
    force: bool = False


# ── Responses ──────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    ollama: bool
    ollama_host: str
    model: str
    model_available: bool
    embed_model: str
    embed_model_available: bool
    supabase: bool
    vector_ready: bool
    repo_mounted: bool
    output_mounted: bool

class ScanResponse(BaseModel):
    path: str
    files: list[str]
    summary: str
    patterns: list[str]
    dependencies: list[str]
    written_to: str

class FindResponse(BaseModel):
    query: str
    path: str
    matches: list[str]
    files: list[str]
    written_to: str

class RoutesResponse(BaseModel):
    routes: list[str]
    api_routes: list[str]
    server_actions: list[str]
    middleware: list[str]
    auth_paths: list[str]
    written_to: str

class DependenciesResponse(BaseModel):
    path: str
    internal: list[str]
    external: list[str]
    graph: dict[str, list[str]]
    written_to: str

class SummarizeResponse(BaseModel):
    file: str
    purpose: str
    dependencies: list[str]
    risks: list[str]
    architectural_notes: list[str]
    written_to: str

class ContextResponse(BaseModel):
    task: str
    files: list[str]
    summary: str
    risks: list[str]
    suggested_files: list[str]
    vector_hits: list[str]
    written_to: str

class DiffResponse(BaseModel):
    summary: str
    risks: list[str]
    files_touched: list[str]
    test_recommendations: list[str]
    written_to: str

class VectorSearchResponse(BaseModel):
    query: str
    matches: list[dict]
    written_to: str
    available: bool

class IndexResponse(BaseModel):
    paths: list[str]
    indexed: int
    skipped: int
    errors: int
    available: bool
    written_to: str
