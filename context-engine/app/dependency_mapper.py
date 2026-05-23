"""
dependency_mapper.py — deterministic import/dependency analysis.

Builds a dependency graph from source files.
No model calls — pure text parsing.
"""

import re
from collections import defaultdict
from pathlib import Path
from . import config
from .repo_reader import walk_repo, read_file, rel_path
from .search_worker import extract_imports

# Package.json detection
PACKAGE_JSON_PATTERN = re.compile(r'"([^"]+)":\s*"[^"]*"')


def is_internal(dep: str) -> bool:
    """True if import path looks like an internal/relative module."""
    return dep.startswith(".") or dep.startswith("@/") or dep.startswith("~/")


def normalize_external(dep: str) -> str:
    """Strip version specifiers and sub-paths from package names."""
    # e.g. "react-dom/client" → "react-dom", "@supabase/auth-helpers-nextjs" stays
    parts = dep.split("/")
    if dep.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def map_dependencies(base_path: str = ".") -> dict:
    """
    Walk base_path and build a dependency map.

    Returns:
      internal: sorted list of internal import paths used
      external: sorted list of external packages used
      graph: {file: [imports]} for all scanned files
      package_json_deps: list of declared deps from package.json
    """
    from .repo_reader import safe_resolve
    base = safe_resolve(base_path) if base_path and base_path != "." else config.REPO_ROOT
    files = walk_repo(base)

    graph: dict[str, list[str]] = {}
    all_internal: set[str] = set()
    all_external: set[str] = set()
    package_json_deps: list[str] = []

    # Read package.json if present at repo root
    pkg_json = config.REPO_ROOT / "package.json"
    if pkg_json.exists():
        try:
            import json
            data = json.loads(pkg_json.read_text())
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                package_json_deps.extend(data.get(section, {}).keys())
        except Exception:
            pass

    for f in files[:config.MAX_FILES_PER_SCAN]:
        content = read_file(f)
        if not content:
            continue

        imports = extract_imports(content)
        rp = rel_path(f)
        graph[rp] = imports

        for imp in imports:
            if is_internal(imp):
                all_internal.add(imp)
            else:
                all_external.add(normalize_external(imp))

    return {
        "internal":          sorted(all_internal),
        "external":          sorted(all_external),
        "graph":             graph,
        "package_json_deps": sorted(set(package_json_deps)),
    }
