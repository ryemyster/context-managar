import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "context-engine"))

from app import repo_reader


def test_normalize_repo_path_accepts_common_caller_forms(monkeypatch, tmp_path):
    repo_root = tmp_path / "owner" / "repo"
    repo_root.mkdir(parents=True)
    monkeypatch.setattr(repo_reader.config, "REPO_ROOT", repo_root)

    assert repo_reader.normalize_repo_path("src/app.py") == "src/app.py"
    assert repo_reader.normalize_repo_path("./src/app.py") == "src/app.py"
    assert repo_reader.normalize_repo_path("repo/src/app.py") == "src/app.py"
    assert repo_reader.normalize_repo_path("owner/repo/src/app.py") == "src/app.py"
    assert repo_reader.normalize_repo_path(str(repo_root / "src" / "app.py")) == "src/app.py"


def test_safe_resolve_rejects_absolute_paths_outside_repo(monkeypatch, tmp_path):
    repo_root = tmp_path / "owner" / "repo"
    repo_root.mkdir(parents=True)
    monkeypatch.setattr(repo_reader.config, "REPO_ROOT", repo_root)

    with pytest.raises(HTTPException):
        repo_reader.safe_resolve(str(tmp_path / "outside.py"))
