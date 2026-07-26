"""
config.py — all environment variables and constants for context-engine.
Single source of truth. All other modules import from here.
"""

import os
from pathlib import Path


def _clean_env_path(value: str) -> str:
    """Trim whitespace and one matching quote pair from a path env var."""
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def resolve_output_dir(raw_value: str, repo_root: Path) -> Path:
    """
    Resolve OUTPUT_DIR and reject paths inside REPO_ROOT.

    Artifacts are generated retrieval outputs. If they land inside the repo tree,
    future scans and indexes can ingest generated artifacts as source context.
    """
    output_dir = Path(_clean_env_path(raw_value)).expanduser().resolve()
    resolved_repo = repo_root.resolve()
    if output_dir == resolved_repo or output_dir.is_relative_to(resolved_repo):
        raise RuntimeError(
            "OUTPUT_DIR must be outside REPO_ROOT; generated artifacts would "
            f"pollute repository scans: OUTPUT_DIR={output_dir} REPO_ROOT={resolved_repo}"
        )
    return output_dir

# ── Ollama ─────────────────────────────────────────────────────────────────────
OLLAMA_HOST         = os.getenv("OLLAMA_HOST",         "http://founderos-ollama:11434")
OLLAMA_MODEL        = os.getenv("OLLAMA_MODEL",        "qwen2.5-coder:3b")
OLLAMA_REASON_MODEL = os.getenv("OLLAMA_REASON_MODEL", "qwen3.5:9b")
OLLAMA_ARCH_MODEL   = os.getenv("OLLAMA_ARCH_MODEL",   "qwen3:4b")
OLLAMA_AGENT_MODEL  = os.getenv("OLLAMA_AGENT_MODEL",  OLLAMA_ARCH_MODEL)
OLLAMA_AGENT_SELECT_MODEL = os.getenv("OLLAMA_AGENT_SELECT_MODEL", OLLAMA_MODEL)
OLLAMA_AGENT_VERIFY_MODEL = os.getenv("OLLAMA_AGENT_VERIFY_MODEL", OLLAMA_MODEL)
OLLAMA_EMBED_MODEL  = os.getenv("OLLAMA_EMBED_MODEL",  "nomic-embed-text")

# Inference timeouts — set to benchmark max × 1.1 (upper bound + 10% headroom)
# Benchmarked on Apple Silicon M-series, local Ollama, extended timeout runs
OLLAMA_TIMEOUT        = 150.0   # qwen2.5-coder:3b max observed 122.8s × 1.1 = 135s → 150s
OLLAMA_REASON_TIMEOUT = 600.0   # qwen3.5:9b max observed 519.3s × 1.1 = 571s → 600s
OLLAMA_ARCH_TIMEOUT   = float(os.getenv("OLLAMA_ARCH_TIMEOUT", "650.0"))  # qwen3:4b max observed 562.6s × 1.1 = 619s → 650s
OLLAMA_NUM_CTX        = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
OLLAMA_NUM_PREDICT    = 400     # max output tokens for code model
OLLAMA_REASON_PREDICT = 1024    # reasoning model needs room for chain-of-thought

# Agentic loop settings — tuned for qwen3:4b agent model (select/verify stay on 3b fast path)
OLLAMA_AGENT_TIMEOUT        = float(os.getenv("OLLAMA_AGENT_TIMEOUT",        "3000.0")) # 50 min total: covers ~4 deep qwen3:4b turns with tool calls
OLLAMA_AGENT_CALL_TIMEOUT   = float(os.getenv("OLLAMA_AGENT_CALL_TIMEOUT",   "650.0"))  # per-call limit matches OLLAMA_ARCH_TIMEOUT (qwen3:4b worst case)
OLLAMA_AGENT_SELECT_TIMEOUT = float(os.getenv("OLLAMA_AGENT_SELECT_TIMEOUT", "45.0"))   # schema-constrained next-action selection (3b, always fast)
OLLAMA_AGENT_MEMORY_TIMEOUT = float(os.getenv("OLLAMA_AGENT_MEMORY_TIMEOUT", "10.0"))   # memory is useful but must not block the agent
OLLAMA_AGENT_VERIFY_TIMEOUT = float(os.getenv("OLLAMA_AGENT_VERIFY_TIMEOUT", "60.0"))   # verification degrades gracefully when reasoning is slow
OLLAMA_AGENT_NUM_PREDICT    = int(os.getenv("OLLAMA_AGENT_NUM_PREDICT",      "400"))    # bound each tool-selection/final-answer response
AGENT_MAX_ITERATIONS        = int(os.getenv("AGENT_MAX_ITERATIONS",          "10"))     # max think→act cycles before forced stop
AGENT_MAX_REPAIR_ITERATIONS = int(os.getenv("AGENT_MAX_REPAIR_ITERATIONS",   "3"))      # max tool cycles in the single repair pass after a failed verification
AGENT_TOOL_RESULT_MAX_CHARS = int(os.getenv("AGENT_TOOL_RESULT_MAX_CHARS",   "3000"))   # truncate tool results to protect context window

# ── Paths ──────────────────────────────────────────────────────────────────────
REPO_ROOT  = Path(_clean_env_path(os.getenv("REPO_ROOT", "/repo"))).expanduser().resolve()
OUTPUT_DIR = resolve_output_dir(os.getenv("OUTPUT_DIR", "/output"), REPO_ROOT)

# Max size of the artifacts dir before oldest files are evicted (0 = no limit)
ARTIFACTS_MAX_MB = int(os.getenv("ARTIFACTS_MAX_MB", "50"))

# ── Scan limits ────────────────────────────────────────────────────────────────
MAX_FILE_BYTES       = int(os.getenv("MAX_FILE_BYTES",       "262144"))  # 256 KB per file
MAX_FILES_PER_SCAN   = int(os.getenv("MAX_FILES_PER_SCAN",   "80"))
MAX_SNIPPETS_PER_QUERY = int(os.getenv("MAX_SNIPPETS_PER_QUERY", "20"))
MAX_TOTAL_CHARS      = 3_500   # ~875 tokens — safe for 2048 ctx with prompt overhead
DIFF_MAX_CHARS       = 8_000   # ~2000 tokens — diff budget (4096 ctx - 1024 output - overhead)

# ── Supabase ───────────────────────────────────────────────────────────────────
SUPABASE_URL              = os.getenv("SUPABASE_URL",              "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_VECTOR_TABLE     = os.getenv("SUPABASE_VECTOR_TABLE",     "code_embeddings")
SUPABASE_MATCH_FUNCTION   = os.getenv("SUPABASE_MATCH_FUNCTION",   "match_code_embeddings")

# Embedding dimensions for nomic-embed-text
EMBED_DIMENSIONS = 768

# ── Security ───────────────────────────────────────────────────────────────────
# Set CONTEXT_ENGINE_API_KEY to a non-empty string to require X-API-Key on every request.
# Leave empty (default) to allow unauthenticated local access.
# Cloud deployments MUST set this. Local dev can leave it unset.
CONTEXT_ENGINE_API_KEY = os.getenv("CONTEXT_ENGINE_API_KEY", "")

# Optional public URLs for deployment-aware /setup output.
# When unset, /setup derives values from the incoming request and falls back to
# localhost-oriented defaults for local launchd installs.
CONTEXT_ENGINE_PUBLIC_BASE_URL = os.getenv("CONTEXT_ENGINE_PUBLIC_BASE_URL", "").strip()
CONTEXT_ENGINE_PUBLIC_MCP_URL = os.getenv("CONTEXT_ENGINE_PUBLIC_MCP_URL", "").strip()

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL  = os.getenv("LOG_LEVEL",  "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()   # "text" or "json"

SLOW_REQUEST_MS = int(os.getenv("SLOW_REQUEST_MS", "5000"))

# ── Skip / filter rules ────────────────────────────────────────────────────────
SKIP_DIRS: set[str] = {
    "node_modules", ".next", ".git", "dist", "build",
    "coverage", ".turbo", ".vercel", ".netlify",
    "__pycache__", ".cache", ".pytest_cache",
    "venv", ".venv", ".mypy_cache", ".tsbuildinfo",
}

NEVER_INDEX_FILES: set[str] = {
    ".env", ".env.local", ".env.production", ".env.development",
    ".env.staging", ".env.test",
}

CODE_EXTENSIONS: set[str] = {
    ".ts", ".tsx", ".js", ".jsx",
    ".py", ".go", ".rs",
    ".json", ".yaml", ".yml", ".toml",
    ".sql", ".sh", ".md",
    ".css", ".scss",
}

# ── Chunking defaults ──────────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE    = int(os.getenv("DEFAULT_CHUNK_SIZE", "500"))
DEFAULT_CHUNK_OVERLAP = int(os.getenv("DEFAULT_CHUNK_OVERLAP", "50"))
