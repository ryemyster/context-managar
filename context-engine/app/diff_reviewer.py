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

DIFF_HEADER_PATTERN = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
FILE_HEADER_PATTERN = re.compile(r'^(?:---|\+\+\+)\s+(?:[ab]/)?(.+)$', re.MULTILINE)


def extract_touched_files(diff_text: str) -> list[str]:
    """Extract file paths from diff headers."""
    raw: list[str] = []
    for before, after in DIFF_HEADER_PATTERN.findall(diff_text):
        raw.extend([before, after])
    raw.extend(FILE_HEADER_PATTERN.findall(diff_text))
    return list(dict.fromkeys(
        f for f in raw
        if f not in ("/dev/null", "null") and not f.startswith("/dev/null")
    ))


def build_diff_manifest(diff_text: str) -> dict:
    """Return deterministic diff metadata that must not be model-generated."""
    files = extract_touched_files(diff_text)
    source_files = [
        path for path in files
        if not re.search(r"(^|/)(tests?|__tests__|specs?)/|[._-](test|spec)\.", path)
    ]
    test_files = [path for path in files if path not in source_files]
    return {
        "files_touched": files,
        "file_count": len(files),
        "source_files": source_files,
        "source_file_count": len(source_files),
        "test_files": test_files,
        "test_file_count": len(test_files),
        "truncated_for_model": len(diff_text) > config.DIFF_MAX_CHARS,
    }


def classify_risk(text: str) -> dict:
    """Classify model risk prose so generic or follow-up findings are visible but de-emphasized."""
    lowered = text.lower()
    if any(token in lowered for token in ("out of scope", "follow-up", "future", "hardening")):
        bucket = "follow_up"
    elif any(token in lowered for token in ("may", "might", "could", "potential", "consider")):
        bucket = "generic_hardening"
    else:
        bucket = "in_scope"
    return {
        "category": bucket,
        "description": text,
    }


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

    # Truncate only the model prompt. Deterministic metadata always sees the full diff.
    manifest = build_diff_manifest(diff_text)
    truncated = diff_text[:config.DIFF_MAX_CHARS]

    prompt = f"""Analyze this git diff. Respond with JSON only — no markdown fences.

Deterministic diff manifest, computed outside the model:
{manifest}

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
    risks = parsed.get("risks", [])
    risk_findings = [classify_risk(str(risk)) for risk in risks]

    return {
        "summary":              parsed.get("summary", raw[:300] if model_error else ""),
        "risks":                risks,
        "risk_findings":        risk_findings,
        "files_touched":        manifest["files_touched"],
        "diff_manifest":        manifest,
        "test_recommendations": parsed.get("test_recommendations", []),
        "model_error":          model_error,
        "timing_ms": {
            "prompt_build": prompt_build_ms,
            "model":        model_ms,
        },
    }
