"""
issue_auditor.py — deterministic evidence collection for issue triage.

The model can summarize, but status and recommendation should come from concrete
repo evidence so issue closure advice is not generic.
"""

from __future__ import annotations

from pathlib import Path

from .repo_reader import read_file, rel_path, safe_resolve, walk_repo

STUB_MARKERS = (
    "placeholder",
    "not implemented",
    "not yet implemented",
    "stub implementation",
    "migration stub",
    "todo: implement",
    "todo implement",
)


def _agent_root(path: str) -> str | None:
    parts = path.split("/")
    try:
        idx = parts.index("agents")
    except ValueError:
        return None
    if len(parts) <= idx + 1:
        return None
    return "/".join(parts[: idx + 2])


def _has_non_stub_agent_impl(content: str) -> bool:
    lowered = content.lower()
    has_contract_symbol = any(
        symbol in content
        for symbol in (
            "runTask",
            "AgentManifest",
            "createAgent",
            "export",
            "manifest",
        )
    )
    has_stub_marker = any(marker.lower() in lowered for marker in STUB_MARKERS)
    return has_contract_symbol and not has_stub_marker


def _file_has_stub_marker(content: str) -> bool:
    lowered = content.lower()
    return any(marker.lower() in lowered for marker in STUB_MARKERS)


def collect_agent_evidence(paths: list[str]) -> list[dict]:
    """
    Collect package-scoped agent migration evidence under requested paths.

    Returns one row per discovered agent package root.
    """
    files: list[Path] = []
    for path in paths:
        try:
            base = safe_resolve(path)
        except Exception:
            continue
        files.extend(walk_repo(base))

    by_agent: dict[str, dict] = {}
    for file_path in files:
        rp = rel_path(file_path)
        agent_root = _agent_root(rp)
        if not agent_root:
            continue

        row = by_agent.setdefault(
            agent_root,
            {
                "agent": agent_root,
                "implementation_file": None,
                "agent_test_file": None,
                "mcp_client_test_file": None,
                "readme_file": None,
                "stub_files": [],
                "missing_evidence": [],
                "status": "insufficient evidence",
                "recommendation": "keep open; no concrete repo evidence found",
            },
        )

        content = read_file(file_path)
        if rp.endswith(("/src/agent/index.ts", "/src/agent/index.tsx")):
            row["implementation_file"] = rp
            if content and not _has_non_stub_agent_impl(content):
                row["stub_files"].append(rp)
        elif rp.endswith(("/tests/agent.test.ts", "/tests/agent.test.tsx")):
            row["agent_test_file"] = rp
            if content and _file_has_stub_marker(content):
                row["stub_files"].append(rp)
        elif rp.endswith(("/tests/mcp-client.test.ts", "/tests/mcp-client.test.tsx")):
            row["mcp_client_test_file"] = rp
            if content and _file_has_stub_marker(content):
                row["stub_files"].append(rp)
        elif rp.endswith("/README.md"):
            row["readme_file"] = rp

    return [classify_agent_evidence(row) for row in sorted(by_agent.values(), key=lambda item: item["agent"])]


def classify_agent_evidence(row: dict) -> dict:
    missing: list[str] = []
    if not row.get("implementation_file"):
        missing.append("src/agent/index.ts")
    if not row.get("agent_test_file"):
        missing.append("tests/agent.test.ts")
    if not row.get("mcp_client_test_file"):
        missing.append("tests/mcp-client.test.ts")
    if row.get("stub_files"):
        missing.append("remove stub/TODO/placeholder markers")

    row["missing_evidence"] = missing

    has_any_concrete = any(
        row.get(field)
        for field in ("implementation_file", "agent_test_file", "mcp_client_test_file")
    )
    has_only_docs = bool(row.get("readme_file")) and not has_any_concrete

    if not missing:
        row["status"] = "complete"
        row["recommendation"] = "close"
    elif has_only_docs:
        row["status"] = "stub/docs only"
        row["recommendation"] = "keep open; implementation and tests are missing"
    elif has_any_concrete:
        row["status"] = "partial"
        row["recommendation"] = f"keep open; missing {', '.join(missing)}"
    else:
        row["status"] = "insufficient evidence"
        row["recommendation"] = "keep open; no concrete repo evidence found"

    return row


def findings_from_evidence(evidence_matrix: list[dict], issue_numbers: list[int]) -> list[dict]:
    """
    Convert package evidence into issue findings.

    If exact issue-to-agent mapping is unavailable, pair sorted issue numbers with
    sorted agent packages. Remaining rows are emitted with their agent as the key.
    """
    findings: list[dict] = []
    for idx, row in enumerate(evidence_matrix):
        issue = issue_numbers[idx] if idx < len(issue_numbers) else row["agent"]
        evidence_files = [
            value
            for value in (
                row.get("implementation_file"),
                row.get("agent_test_file"),
                row.get("mcp_client_test_file"),
                row.get("readme_file"),
            )
            if value
        ]
        findings.append(
            {
                "issue": issue,
                "agent": row["agent"],
                "status": row["status"],
                "evidence_files": evidence_files,
                "missing_evidence": row["missing_evidence"],
                "recommendation": row["recommendation"],
            }
        )
    return findings


def insufficient_evidence_findings(issue_numbers: list[int]) -> list[dict]:
    issues = issue_numbers or ["unmapped"]
    return [
        {
            "issue": issue,
            "agent": None,
            "status": "insufficient evidence",
            "evidence_files": [],
            "missing_evidence": ["agent package evidence"],
            "recommendation": "keep open; no concrete agent package evidence found in requested paths",
        }
        for issue in issues
    ]
