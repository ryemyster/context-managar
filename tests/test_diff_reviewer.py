import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "context-engine"))

from app import diff_reviewer


def test_build_diff_manifest_uses_full_diff_and_classifies_test_files(monkeypatch):
    monkeypatch.setattr(diff_reviewer.config, "DIFF_MAX_CHARS", 80)
    diff = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -1 +1 @@
-old
+new
""" + ("x" * 200) + """
diff --git a/tests/test_auth.py b/tests/test_auth.py
--- a/tests/test_auth.py
+++ b/tests/test_auth.py
@@ -1 +1 @@
-old
+new
"""

    manifest = diff_reviewer.build_diff_manifest(diff)

    assert manifest["files_touched"] == ["app/auth.py", "tests/test_auth.py"]
    assert manifest["file_count"] == 2
    assert manifest["source_files"] == ["app/auth.py"]
    assert manifest["test_files"] == ["tests/test_auth.py"]
    assert manifest["truncated_for_model"] is True


@pytest.mark.asyncio
async def test_review_diff_returns_deterministic_manifest(monkeypatch):
    monkeypatch.setattr(
        diff_reviewer.inference,
        "generate_reasoning",
        AsyncMock(return_value='{"summary":"Changed auth.","risks":["May miss headers."],"test_recommendations":["Run auth tests."]}'),
    )

    result = await diff_reviewer.review_diff(
        """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -1 +1 @@
-old
+new
"""
    )

    assert result["diff_manifest"]["file_count"] == 1
    assert result["files_touched"] == ["app/auth.py"]
    assert result["risk_findings"] == [
        {"category": "generic_hardening", "description": "May miss headers."}
    ]
