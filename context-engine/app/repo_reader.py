"""
repo_reader.py — safe, bounded repo access.

All path operations go through safe_resolve().
No writes. No follows outside REPO_ROOT.
"""

from pathlib import Path
from fastapi import HTTPException
from . import config
from .logger import log


def normalize_repo_path(path: str) -> str:
    """
    Normalize caller-supplied paths to a canonical REPO_ROOT-relative path.

    Accepted forms:
    - src/file.py
    - ./src/file.py
    - repo-name/src/file.py when REPO_ROOT.name == repo-name
    - owner/repo-name/src/file.py when REPO_ROOT ends in owner/repo-name
    - absolute paths under REPO_ROOT
    """
    raw = str(path or "").strip()
    if not raw:
        return ""

    repo_root = config.REPO_ROOT.resolve()
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if resolved == repo_root:
            return "."
        if resolved.is_relative_to(repo_root):
            return str(resolved.relative_to(repo_root))
        return raw

    parts = [part for part in raw.replace("\\", "/").split("/") if part and part != "."]
    if not parts:
        return "."

    root_parts = repo_root.parts
    if len(parts) >= 2 and len(root_parts) >= 2 and tuple(parts[:2]) == tuple(root_parts[-2:]):
        parts = parts[2:] or ["."]
    elif parts and parts[0] == repo_root.name:
        parts = parts[1:] or ["."]

    return "/".join(parts)


def safe_resolve(rel_path: str) -> Path:
    """
    Resolve a relative path under REPO_ROOT.
    Raises HTTP 400 if the path would escape REPO_ROOT (path traversal guard).
    """
    normalized = normalize_repo_path(rel_path)
    p = (config.REPO_ROOT / normalized).resolve()
    if not p.is_relative_to(config.REPO_ROOT.resolve()):
        raise HTTPException(
            status_code=400,
            detail=f"Path traversal rejected: {rel_path!r}"
        )
    return p


def rel_path(path: Path) -> str:
    """Return path as string relative to REPO_ROOT."""
    try:
        return str(path.resolve().relative_to(config.REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def should_skip(path: Path) -> bool:
    """True if any path component is in SKIP_DIRS."""
    try:
        parts = path.relative_to(config.REPO_ROOT).parts
        return any(part in config.SKIP_DIRS for part in parts)
    except ValueError:
        return False


def is_never_index(path: Path) -> bool:
    """True if this file should never be indexed (env files etc)."""
    return path.name in config.NEVER_INDEX_FILES


def walk_repo(base: Path, extensions: set[str] | None = None) -> list[Path]:
    """
    Walk base directory, skipping heavy dirs, returning code files.
    Respects CODE_EXTENSIONS filter unless overridden.
    """
    ext_filter = extensions or config.CODE_EXTENSIONS
    results: list[Path] = []
    try:
        for item in sorted(base.rglob("*")):
            if (
                item.is_file()
                and item.suffix in ext_filter
                and not should_skip(item)
                and not is_never_index(item)
            ):
                results.append(item)
    except PermissionError as e:
        log.debug("walk permission denied base=%s: %s", base, e)
    return results


def read_file(path: Path, max_bytes: int | None = None) -> str:
    """
    Read a file up to max_bytes. Returns empty string on any error.
    Handles encoding errors gracefully.
    """
    limit = max_bytes or config.MAX_FILE_BYTES
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception as e:
        log.debug("read_file failed path=%s: %s", path, e)
        return ""


def build_snippet_block(
    files: list[tuple[str, str]],
    max_chars: int | None = None,
) -> str:
    """
    Aggregate (path, content) pairs into a single snippet block.
    Stops adding content once the character budget is exhausted.
    Always includes at least one line per file even if over budget.
    """
    budget = max_chars or config.MAX_TOTAL_CHARS
    block: list[str] = []
    total = 0

    for path, content in files:
        header = f"\n### {path}\n```\n"
        footer = "\n```\n"
        available = budget - total - len(header) - len(footer)
        if available <= 0:
            # Include file name only so Claude knows it exists
            block.append(f"\n### {path}\n[truncated — over context budget]\n")
            continue
        chunk = content[:available]
        block.append(header + chunk + footer)
        total += len(header) + len(chunk) + len(footer)

    return "".join(block)
