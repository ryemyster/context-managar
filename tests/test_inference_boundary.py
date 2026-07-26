"""Architectural dependency rules for inference providers."""

import ast
from pathlib import Path


APP_ROOT = Path(__file__).parents[1] / "context-engine" / "app"


def test_provider_modules_are_not_imported_outside_inference_package():
    violations = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] == "inference" or relative.name == "ollama_client.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(alias.name for alias in node.names)
            if "inference.providers" in module or "ollama_client" in module:
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []
