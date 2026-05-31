"""
config.py — all environment variables and constants for context-engine.
Single source of truth. All other modules import from here.
"""

import os
from pathlib import Path

# ── Ollama ─────────────────────────────────────────────────────────────────────
OLLAMA_HOST         = os.getenv("OLLAMA_HOST",         "http://founderos-ollama:11434")
OLLAMA_MODEL        = os.getenv("OLLAMA_MODEL",        "qwen2.5-coder:3b")
OLLAMA_REASON_MODEL = os.getenv("OLLAMA_REASON_MODEL", "qwen3.5:9b")
OLLAMA_EMBED_MODEL  = os.getenv("OLLAMA_EMBED_MODEL",  "nomic-embed-text")

# Inference settings — tuned for 3b code model (~3.6s) / 9b reasoning on M3 16GB
OLLAMA_TIMEOUT        = 120.0   # seconds; 3b on M3 completes in ~3-15s typical
OLLAMA_REASON_TIMEOUT = 600.0   # reasoning model cold-load + generation on M3 Docker can take 3-5 min
OLLAMA_NUM_CTX        = 4096    # 3b at 4096 ctx = ~2.4GB total — fine on 16GB M3
OLLAMA_NUM_PREDICT    = 400     # max output tokens for code model
OLLAMA_REASON_PREDICT = 1024    # reasoning model needs room for chain-of-thought

# ── Paths ──────────────────────────────────────────────────────────────────────
REPO_ROOT  = Path(os.getenv("REPO_ROOT",   "/repo")).resolve()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR",  "/output")).resolve()

# Max size of the artifacts dir before oldest files are evicted (0 = no limit)
ARTIFACTS_MAX_MB = int(os.getenv("ARTIFACTS_MAX_MB", "50"))

# ── Scan limits ────────────────────────────────────────────────────────────────
MAX_FILE_BYTES       = int(os.getenv("MAX_FILE_BYTES",       "32768"))   # 32 KB per file
MAX_FILES_PER_SCAN   = int(os.getenv("MAX_FILES_PER_SCAN",   "80"))
MAX_SNIPPETS_PER_QUERY = int(os.getenv("MAX_SNIPPETS_PER_QUERY", "20"))
MAX_TOTAL_CHARS      = 3_500   # ~875 tokens — safe for 2048 ctx with prompt overhead

# ── Supabase ───────────────────────────────────────────────────────────────────
SUPABASE_URL              = os.getenv("SUPABASE_URL",              "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_VECTOR_TABLE     = os.getenv("SUPABASE_VECTOR_TABLE",     "code_embeddings")
SUPABASE_MATCH_FUNCTION   = os.getenv("SUPABASE_MATCH_FUNCTION",   "match_code_embeddings")

# Embedding dimensions for nomic-embed-text
EMBED_DIMENSIONS = 768

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
