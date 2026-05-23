"""
route_extractor.py — deterministic Next.js route extraction.

Classifies files by naming conventions. No model calls.
Reads route files for HTTP method detection via regex.
"""

import re
from pathlib import Path
from . import config
from .repo_reader import walk_repo, read_file, rel_path, safe_resolve

AUTH_TERMS = {"auth", "login", "session", "token", "jwt", "oauth", "signin", "signup", "logout"}

HTTP_METHOD_PATTERN = re.compile(
    r'export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)',
    re.MULTILINE,
)

MIDDLEWARE_MATCHER_PATTERN = re.compile(r'matcher\s*[:=]\s*\[([^\]]+)\]', re.MULTILINE)


def classify_file(path: Path) -> str | None:
    """Return route type or None if not a route file."""
    name = path.name.lower()
    stem = path.stem.lower()

    if name in ("route.ts", "route.js", "route.tsx", "route.jsx"):
        return "api"
    if name in ("page.tsx", "page.jsx", "page.ts", "page.js"):
        return "page"
    if name in ("middleware.ts", "middleware.js"):
        return "middleware"
    if "action" in name or stem.endswith("actions") or stem.endswith("action"):
        return "action"
    if name in ("layout.tsx", "layout.jsx", "layout.ts", "layout.js"):
        return "layout"
    return None


def extract_http_methods(content: str) -> list[str]:
    """Return HTTP methods exported from a route file."""
    return HTTP_METHOD_PATTERN.findall(content)


def extract_middleware_matchers(content: str) -> list[str]:
    """Return middleware matcher paths."""
    m = MIDDLEWARE_MATCHER_PATTERN.search(content)
    if not m:
        return []
    raw = m.group(1)
    return [p.strip().strip("'\"") for p in raw.split(",") if p.strip()]


def extract_routes(repo_root: Path | None = None) -> dict:
    """
    Walk the repo and classify all route-related files.

    Returns:
      api_routes: [{path, methods}]
      page_routes: [path]
      server_actions: [path]
      middleware: [{path, matchers}]
      auth_paths: [path]
      layouts: [path]
    """
    base = repo_root or config.REPO_ROOT
    files = walk_repo(base)

    api_routes:     list[dict] = []
    page_routes:    list[str]  = []
    server_actions: list[str]  = []
    middleware:     list[dict] = []
    auth_paths:     list[str]  = []
    layouts:        list[str]  = []

    for f in files:
        r = rel_path(f)
        kind = classify_file(f)

        if kind == "api":
            content = read_file(f)
            methods = extract_http_methods(content)
            api_routes.append({"path": r, "methods": methods})

        elif kind == "page":
            page_routes.append(r)

        elif kind == "middleware":
            content = read_file(f)
            matchers = extract_middleware_matchers(content)
            middleware.append({"path": r, "matchers": matchers})

        elif kind == "action":
            server_actions.append(r)

        elif kind == "layout":
            layouts.append(r)

        # Auth classification — any route type can be auth-related
        if any(term in r.lower() for term in AUTH_TERMS) and r not in auth_paths:
            auth_paths.append(r)

    return {
        "api_routes":     api_routes,
        "page_routes":    page_routes,
        "server_actions": server_actions,
        "middleware":     middleware,
        "auth_paths":     auth_paths,
        "layouts":        layouts,
    }
