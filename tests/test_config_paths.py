from pathlib import Path
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "context-engine"))

from app import config


def test_resolve_output_dir_strips_matching_quotes():
    repo_root = Path("/tmp/repos")

    output = config.resolve_output_dir(
        '"/tmp/context-store/artifacts"',
        repo_root,
    )

    assert output == Path("/tmp/context-store/artifacts").resolve()


def test_resolve_output_dir_rejects_repo_internal_artifacts():
    repo_root = Path("/tmp/repos")

    with pytest.raises(RuntimeError, match="OUTPUT_DIR must be outside REPO_ROOT"):
        config.resolve_output_dir(
            "/tmp/repos/owner/repo/context-engine/artifacts",
            repo_root,
        )
