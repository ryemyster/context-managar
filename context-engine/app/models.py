"""
models.py — Pydantic request/response models for all endpoints.
"""

from typing import Literal, Optional
from pydantic import BaseModel, field_validator


DetailLevel = Literal["summary", "standard", "full"]
ResponseMode = Literal["context_safe"]


class DiscoveryOptions(BaseModel):
    detail: DetailLevel = "summary"
    mode: Optional[ResponseMode] = None
    limit: Optional[int] = None
    max_chars: Optional[int] = None
    max_results: Optional[int] = None


# ── Requests ───────────────────────────────────────────────────────────────────

class ScanRequest(DiscoveryOptions):
    path: str = ""

class FindRequest(DiscoveryOptions):
    query: str
    path: str = "."

class DependenciesRequest(DiscoveryOptions):
    path: str = "."

class RoutesRequest(DiscoveryOptions):
    path: str = "."

class ReadRequest(BaseModel):
    path: str
    max_chars: Optional[int] = None

class SummarizeRequest(BaseModel):
    file: str

class ContextRequest(BaseModel):
    task: str
    paths: list[str] = []
    focus: list[str] = []
    use_vector: bool = False   # opt-in — adds embed+model swap latency; only useful after /index

    @field_validator("paths")
    @classmethod
    def paths_must_be_scoped(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p or p in (".", "/"):
                raise ValueError(f"path {p!r} is too broad — must be scoped to a subdirectory (e.g. 'owner/repo/src')")
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

class ScaffoldFile(BaseModel):
    file: str                           # target file path (relative to REPO_ROOT)
    spec: str                           # what this specific file should do
    mode: str = "create"                # "create" | "edit"

class ScaffoldRequest(BaseModel):
    task: str                           # overall feature or task description
    files: list[ScaffoldFile]           # ordered list of files to generate
    context_files: list[str] = []       # shared reference files for all generations


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
