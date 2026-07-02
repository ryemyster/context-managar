"""
models.py — Pydantic request/response models for all endpoints.
"""

from typing import Literal, Optional
from pydantic import BaseModel, field_validator


DetailLevel = Literal["summary", "standard", "full"]
ResponseMode = Literal["context_safe"]


_BARE_SOURCE_ROOTS = {
    "app",
    "components",
    "context-engine",
    "lib",
    "pages",
    "src",
    "tests",
}


def _is_scoped_repo_path(path: str) -> bool:
    if not path or path.startswith("/") or path in {".", "/"}:
        return False
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2 or ".." in parts:
        return False
    return parts[0] not in _BARE_SOURCE_ROOTS


def _validate_scoped_repo_path(path: str, field_name: str) -> str:
    if not _is_scoped_repo_path(path):
        raise ValueError(
            f"{field_name} must include the owner/repo prefix relative to REPO_ROOT "
            "(for example 'owner/repo/src')"
        )
    return path


class DiscoveryOptions(BaseModel):
    detail: DetailLevel = "summary"
    mode: Optional[ResponseMode] = None
    limit: Optional[int] = None
    max_chars: Optional[int] = None
    max_results: Optional[int] = None


# ── Requests ───────────────────────────────────────────────────────────────────

class ScanRequest(DiscoveryOptions):
    path: str = ""

    @field_validator("path")
    @classmethod
    def path_must_be_scoped(cls, v: str) -> str:
        return _validate_scoped_repo_path(v, "path")

class FindRequest(DiscoveryOptions):
    query: str
    path: str = "."

    @field_validator("path")
    @classmethod
    def path_must_be_scoped_or_default(cls, v: str) -> str:
        if v == ".":
            return v
        return _validate_scoped_repo_path(v, "path")

class DependenciesRequest(DiscoveryOptions):
    path: str = "."

    @field_validator("path")
    @classmethod
    def path_must_be_scoped_or_default(cls, v: str) -> str:
        if v == ".":
            return v
        return _validate_scoped_repo_path(v, "path")

class RoutesRequest(DiscoveryOptions):
    path: str = "."

    @field_validator("path")
    @classmethod
    def path_must_be_scoped_or_default(cls, v: str) -> str:
        if v == ".":
            return v
        return _validate_scoped_repo_path(v, "path")

class ReadRequest(BaseModel):
    path: str
    max_chars: Optional[int] = None

    @field_validator("path")
    @classmethod
    def path_must_be_scoped(cls, v: str) -> str:
        return _validate_scoped_repo_path(v, "path")

class SummarizeRequest(BaseModel):
    file: str

    @field_validator("file")
    @classmethod
    def file_must_be_scoped(cls, v: str) -> str:
        return _validate_scoped_repo_path(v, "file")

class ContextRequest(BaseModel):
    task: str
    paths: list[str] = []
    focus: list[str] = []
    use_vector: bool = False   # opt-in — adds embed+model swap latency; only useful after /index

    @field_validator("paths")
    @classmethod
    def paths_must_be_scoped(cls, v: list[str]) -> list[str]:
        for p in v:
            _validate_scoped_repo_path(p, f"path {p!r}")
        return v

class DiffRequest(BaseModel):
    diff: str

class IssueAuditRequest(BaseModel):
    task: str
    repo: str
    paths: list[str]
    focus: list[str] = []
    requirements: list[str] = []
    use_vector: bool = False

class VectorSearchRequest(DiscoveryOptions):
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

    @field_validator("file")
    @classmethod
    def file_must_be_scoped(cls, v: str) -> str:
        return _validate_scoped_repo_path(v, "file")

    @field_validator("context_files")
    @classmethod
    def context_files_must_be_scoped(cls, v: list[str]) -> list[str]:
        for path in v:
            _validate_scoped_repo_path(path, f"context file {path!r}")
        return v

class ScaffoldFile(BaseModel):
    file: str                           # target file path (relative to REPO_ROOT)
    spec: str                           # what this specific file should do
    mode: str = "create"                # "create" | "edit"

    @field_validator("file")
    @classmethod
    def file_must_be_scoped(cls, v: str) -> str:
        return _validate_scoped_repo_path(v, "file")

class ScaffoldRequest(BaseModel):
    task: str                           # overall feature or task description
    files: list[ScaffoldFile]           # ordered list of files to generate
    context_files: list[str] = []       # shared reference files for all generations

    @field_validator("context_files")
    @classmethod
    def context_files_must_be_scoped(cls, v: list[str]) -> list[str]:
        for path in v:
            _validate_scoped_repo_path(path, f"context file {path!r}")
        return v


# ── Responses ──────────────────────────────────────────────────────────────────

class IssueAuditResponse(BaseModel):
    run_id: str
    status: str
    findings: list[dict]
    evidence_matrix: list[dict] = []
    suggested_files: list[str]
    warnings: list[str]
    artifacts: dict

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


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = {}

class AgentRunRequest(BaseModel):
    task: str
    tools: list[str] = []           # empty = all tools enabled
    max_iterations: int = 10
    system_prompt: Optional[str] = None
    allowed_scopes: Optional[list[str]] = None   # None = all scopes permitted

class LogLevelRequest(BaseModel):
    level: str                         # TRACE | DEBUG | INFO | WARNING | ERROR

class LogLevelResponse(BaseModel):
    previous: str
    current: str


class AgentRunResponse(BaseModel):
    run_id: str
    status: str                      # "running" | "complete" | "error"
    task: str
    final_answer: str = ""
    tool_calls_made: list[dict] = []
    iterations: int = 0
    stopped_reason: str = ""
    warnings: list[str] = []
    artifacts: dict = {}
    memory_context_used: bool = False
    memory_hits: int = 0
    plan_state: dict = {}
    verification: dict = {}
