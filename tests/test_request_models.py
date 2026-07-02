import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "context-engine"))

from app.models import ContextRequest, FindRequest, ReadRequest, ScanRequest


def test_context_request_requires_owner_repo_prefixed_paths():
    with pytest.raises(ValidationError, match="owner/repo prefix"):
        ContextRequest(task="inspect clients", paths=["app/clients/[id]"])


def test_context_request_accepts_owner_repo_prefixed_paths():
    req = ContextRequest(
        task="inspect clients",
        paths=["ascendvent/checkin-ascendvent/app/clients/[id]"],
    )

    assert req.paths == ["ascendvent/checkin-ascendvent/app/clients/[id]"]


def test_scan_request_rejects_bare_and_absolute_paths():
    with pytest.raises(ValidationError, match="owner/repo prefix"):
        ScanRequest(path="app/api")

    with pytest.raises(ValidationError, match="owner/repo prefix"):
        ScanRequest(path="/Users/name/Repos/owner/repo/app")


def test_find_request_allows_default_root_but_rejects_bare_scope():
    assert FindRequest(query="auth").path == "."

    with pytest.raises(ValidationError, match="owner/repo prefix"):
        FindRequest(query="auth", path="app/api")


def test_read_request_requires_owner_repo_prefixed_path():
    with pytest.raises(ValidationError, match="owner/repo prefix"):
        ReadRequest(path="app/api/route.ts")
