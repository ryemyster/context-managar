"""
diff_reviewer.py — git diff analysis.

Deterministic: extracts touched files from diff headers.
Model: one reasoning call (qwen3.5:9b, think=false) for summary, risks, test recommendations.
Risk analysis and test recommendations are judgment — not code pattern matching.
"""

import re
import time
from . import config
from .inference import inference

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

    Returns timing_ms breakdown: prompt_build_ms, model_ms.
    Returns model_error=True if the model returned an error string instead of JSON.
    """
    t_prompt = time.monotonic()

    # Truncate to context budget — file header extraction gets full budget, model gets DIFF_MAX_CHARS
    truncated = diff_text[:config.DIFF_MAX_CHARS]
    files_touched = extract_touched_files(truncated)

    prompt = f"""Analyze this git diff. Respond with JSON only — no markdown fences.

{truncated}

Respond with exactly:
{{
  "summary": "2-3 sentences describing what changed and why it matters",
  "risks": ["potential regression or issue", "..."],
  "test_recommendations": ["what to verify", "..."]
}}"""

    prompt_build_ms = int((time.monotonic() - t_prompt) * 1000)

    t_model = time.monotonic()
    raw = await inference.generate_reasoning(prompt)
    model_ms = int((time.monotonic() - t_model) * 1000)

    model_error = raw.startswith("[")
    parsed = inference.parse_json_response(raw)

    return {
        "summary":              parsed.get("summary", raw[:300] if model_error else ""),
        "risks":                parsed.get("risks", []),
        "files_touched":        files_touched,
        "test_recommendations": parsed.get("test_recommendations", []),
        "model_error":          model_error,
        "timing_ms": {
            "prompt_build": prompt_build_ms,
            "model":        model_ms,
        },
    }
