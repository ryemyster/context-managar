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
    use_vector: bool = False   # opt-in — adds embed+model swap latency; only useful after /index

class DiffRequest(BaseModel):
    diff: str

class VectorSearchRequest(BaseModel):
    query: str
    limit: int = 8
    threshold: float = 0.3   # cosine similarity floor; prose/markdown typically 0.2-0.5, code 0.5-0.8

class IndexRequest(BaseModel):
    paths: list[str] = ["."]
    force: bool = False

class DraftRequest(BaseModel):
    task: str                           # what to implement — be specific
    file: str                           # target file path (relative to REPO_ROOT)
    context_files: list[str] = []       # additional files to read for context
    mode: str = "edit"                  # "create" | "edit"

class ScaffoldFile(BaseModel):
    file: str                           # target file path (relative to REPO_ROOT)
    spec: str                           # what this specific file should do
    mode: str = "create"                # "create" | "edit"

class ScaffoldRequest(BaseModel):
    task: str                           # overall feature or task description
    files: list[ScaffoldFile]           # ordered list of files to generate
    context_files: list[str] = []       # shared reference files for all generations


# ── Responses ──────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

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

class DraftResponse(BaseModel):
    file: str
    mode: str
    code: str
    written_to: str

class ScaffoldFileResult(BaseModel):
    file: str
    mode: str
    code: str
    written_to: str

class ScaffoldResponse(BaseModel):
    task: str
    files: list[ScaffoldFileResult]
    total: int
    errors: list[str]
