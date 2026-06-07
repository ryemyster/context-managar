#!/usr/bin/env python3
"""
mcp_server.py — MCP stdio wrapper for the context-engine REST API.

Editors (Zed, Cursor, Neovim, VS Code) spawn this as a subprocess.
It speaks MCP (JSON-RPC 2.0 over stdio) and translates tool calls into
REST calls to localhost:8088. The REST engine keeps running independently —
this is just a protocol adapter.

Usage — Zed settings.json:
  "context_servers": {
    "context-engine": {
      "command": {
        "path": "/Users/<you>/Repos/ryemyster/context-manager/context-engine/.venv/bin/python",
        "args": ["/Users/<you>/Repos/ryemyster/context-manager/context-engine/mcp_server.py"]
      }
    }
  }

Requires: httpx (already in requirements.txt). No other deps.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import httpx

ENGINE_BASE = "http://localhost:8088"
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "context-engine"
SERVER_VERSION = "1.0.0"

# Tool call timeout — read_file and grep on large repos can take a few seconds
TOOL_TIMEOUT = 30.0


# ── Protocol helpers ──────────────────────────────────────────────────────────

def _ok(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _write(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


# ── Engine calls ──────────────────────────────────────────────────────────────

async def _fetch_tools() -> list[dict]:
    """
    Fetch tool schemas from GET /agents/tools and convert to MCP format.
    Falls back to empty list if the engine is down.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{ENGINE_BASE}/agents/tools")
            if r.status_code == 200:
                return [
                    {
                        "name":        t["function"]["name"],
                        "description": t["function"]["description"],
                        "inputSchema": t["function"]["parameters"],
                    }
                    for t in r.json().get("tools", [])
                ]
    except Exception:
        pass
    return []


async def _call_tool(name: str, arguments: dict) -> str:
    """POST /tools/call — returns plain string result."""
    try:
        async with httpx.AsyncClient(timeout=TOOL_TIMEOUT) as c:
            r = await c.post(
                f"{ENGINE_BASE}/tools/call",
                json={"name": name, "arguments": arguments},
            )
            if r.status_code == 200:
                return r.json().get("result", "")
            return f"[error: engine returned HTTP {r.status_code}: {r.text[:200]}]"
    except httpx.TimeoutException:
        return f"[error: tool '{name}' timed out after {TOOL_TIMEOUT}s — engine may be busy]"
    except Exception as e:
        return f"[error calling '{name}': {e}]"


# ── Message handlers ──────────────────────────────────────────────────────────

async def handle(msg: dict, tools_cache: list[dict]) -> dict | None:
    """Dispatch a JSON-RPC message. Returns response dict or None for notifications."""
    method = msg.get("method", "")
    id_ = msg.get("id")          # None for notifications
    params = msg.get("params") or {}

    # Notifications — no response
    if id_ is None:
        return None

    if method == "initialize":
        return _ok(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return _ok(id_, {})

    if method == "tools/list":
        # Refresh cache on every list call so new tools appear without restart
        tools_cache.clear()
        tools_cache.extend(await _fetch_tools())
        return _ok(id_, {"tools": tools_cache})

    if method == "tools/call":
        name      = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not name:
            return _err(id_, -32602, "'name' is required")
        result = await _call_tool(name, arguments)
        return _ok(id_, {
            "content": [{"type": "text", "text": result}],
            "isError": result.startswith("[error"),
        })

    # Unknown method
    return _err(id_, -32601, f"method not found: {method!r}")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main() -> None:
    loop = asyncio.get_event_loop()

    stdin_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin_reader),
        sys.stdin,
    )

    # Warm the tools cache before the first tools/list call
    tools_cache: list[dict] = await _fetch_tools()

    while True:
        try:
            line = await stdin_reader.readline()
        except Exception:
            break
        if not line:
            break

        raw = line.decode(errors="replace").strip()
        if not raw:
            continue

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            _write(_err(None, -32700, f"parse error: {e}"))
            continue

        try:
            response = await handle(msg, tools_cache)
        except Exception as e:
            _write(_err(msg.get("id"), -32603, f"internal error: {e}"))
            continue

        if response is not None:
            _write(response)


if __name__ == "__main__":
    asyncio.run(main())
