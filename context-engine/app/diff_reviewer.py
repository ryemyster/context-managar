"""
diff_reviewer.py — git diff analysis.

Deterministic: extracts touched files from diff headers.
Model: one call for summary, risks, test recommendations.
"""

import re
from hashlib import md5
from . import config
from .repo_reader import build_snippet_block
from . import ollama_client

FILE_HEADER_PATTERN = re.compile(r'^(?:---|\+\+\+)\s+(?:[ab]/)?(.+)$', re.MULTILINE)


def extract_touched_files(diff_text: str) -> list[str]:
    """Extract file paths from diff --- and +++ headers."""
    raw = FILE_HEADER_PATTERN.findall(diff_text)
    return list(dict.fromkeys(
        f for f in raw
        if f not in ("/dev/null", "null") and not f.startswith("/dev/null")
    ))


async def review_diff(diff_text: str) -> dict:
    """
    Analyze a git diff.

    Steps:
    1. Extract touched files (deterministic)
    2. One model call for summary/risks/test recs
    """
    # Truncate to context budget
    truncated = diff_text[:config.MAX_TOTAL_CHARS * 2]   # diffs get a bigger budget
    files_touched = extract_touched_files(truncated)

    prompt = f"""Analyze this git diff. Respond with JSON only — no markdown fences.

{truncated[:config.MAX_TOTAL_CHARS]}

Respond with exactly:
{{
  "summary": "2-3 sentences describing what changed and why it matters",
  "risks": ["potential regression or issue", "..."],
  "test_recommendations": ["what to verify", "..."]
}}"""

    raw    = await ollama_client.generate(prompt)
    parsed = ollama_client.parse_json_response(raw)

    return {
        "summary":              parsed.get("summary",              raw[:300] if not parsed else ""),
        "risks":                parsed.get("risks",                []),
        "files_touched":        files_touched,
        "test_recommendations": parsed.get("test_recommendations", []),
    }
