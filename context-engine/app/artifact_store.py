"""
artifact_store.py — canonical local artifact ledger.

Markdown files are useful for humans and agent recovery, but they should not be
the source of truth. This module writes structured JSON records first, appends a
small JSONL event, and optionally writes a markdown view.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_default(value: Any) -> str:
    return str(value)


def _hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _ensure_dirs() -> dict[str, Path]:
    root = config.OUTPUT_DIR
    dirs = {
        "root": root,
        "records": root / "records",
        "markdown": root / "markdown",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def write_pending_record(
    *,
    tool: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Write a 'running' placeholder record. Returns event_id + metadata for use as run_id."""
    dirs = _ensure_dirs()
    timestamp = utc_ts()
    seed = {"tool": tool, "request": request, "timestamp": timestamp}
    event_id = f"{tool.replace('/', '-').strip('-')}-{_hash_payload(seed)}"

    record = {
        "event_id": event_id,
        "tool": tool,
        "status": "running",
        "timestamp": timestamp,
        "request": request,
        "response": {},
    }
    record_path = dirs["records"] / f"{event_id}.json"
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )

    event = {
        "event_id": event_id,
        "tool": tool,
        "status": "running",
        "timestamp": timestamp,
        "record": str(record_path),
        "markdown": None,
    }
    with (dirs["root"] / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")

    return {
        "event_id":  event_id,
        "record":    str(record_path),
        "markdown":  None,
        "event_log": str(dirs["root"] / "events.jsonl"),
    }


def write_record(
    *,
    tool: str,
    request: dict[str, Any],
    response: dict[str, Any],
    markdown: str | None = None,
    status: str = "complete",
    event_id: str | None = None,
) -> dict[str, Any]:
    """
    Persist a canonical run record and return artifact metadata.

    The event id is content-addressed enough for dedupe/debugging while still
    including a timestamp so repeated runs remain distinct in the event log.
    """
    dirs = _ensure_dirs()
    timestamp = utc_ts()
    if event_id is None:
        seed = {"tool": tool, "request": request, "response": response, "timestamp": timestamp}
        event_id = f"{tool.replace('/', '-').strip('-')}-{_hash_payload(seed)}"

    record = {
        "event_id": event_id,
        "tool": tool,
        "status": status,
        "timestamp": timestamp,
        "request": request,
        "response": response,
    }

    record_path = dirs["records"] / f"{event_id}.json"
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )

    markdown_path = None
    if markdown is not None:
        markdown_path = dirs["markdown"] / f"{event_id}.md"
        markdown_path.write_text(markdown, encoding="utf-8")

    event = {
        "event_id": event_id,
        "tool": tool,
        "status": status,
        "timestamp": timestamp,
        "record": str(record_path),
        "markdown": str(markdown_path) if markdown_path else None,
    }
    with (dirs["root"] / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")

    return {
        "event_id": event_id,
        "record": str(record_path),
        "markdown": str(markdown_path) if markdown_path else None,
        "event_log": str(dirs["root"] / "events.jsonl"),
    }
