"""Tests for the thin MCP-to-REST context-engine adapter."""

import importlib.util
import json
import os

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


_spec = importlib.util.spec_from_file_location(
    "mcp_server",
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "context-engine",
        "mcp_server.py",
    ),
)
mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp)


@pytest.mark.asyncio
async def test_initialize_prioritizes_delegation():
    response = await mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )

    result = response["result"]
    assert result["protocolVersion"] == mcp.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "context-engine"
    assert "Prefer investigate_codebase" in result["instructions"]


@pytest.mark.asyncio
async def test_tools_list_has_primary_tools_first_and_advanced_tools_last():
    response = await mcp.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )

    tools = response["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names[:4] == [
        "investigate_codebase",
        "load_context",
        "review_diff",
        "audit_issue",
    ]
    assert names[4:] == [
        "scan_directory",
        "find_in_code",
        "summarize_file",
        "dependency_analysis",
        "vector_search",
    ]
    assert "PRIMARY DELEGATION TOOL" in tools[0]["description"]
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


@pytest.mark.asyncio
async def test_investigate_codebase_starts_and_polls_agent_run():
    responses = [
        {"run_id": "run-1", "status": "running"},
        {
            "run_id": "run-1",
            "status": "complete",
            "final_answer": "All agent endpoints are protected.",
            "tool_calls_made": [{"name": "grep"}],
            "verification": {"passed": True},
            "iterations": 3,
            "plan_state": {"goal": "Inspect auth"},
        },
    ]

    with (
        patch.object(
            mcp, "_request_json", new_callable=AsyncMock, side_effect=responses
        ) as request,
        patch.object(mcp.asyncio, "sleep", new_callable=AsyncMock),
    ):
        result = await mcp._call_tool(
            "investigate_codebase",
            {"task": "Determine whether auth protects all agent endpoints"},
        )

    assert result["verification"]["passed"] is True
    assert result["tool_calls_made"] == [{"name": "grep"}]
    assert request.await_args_list[0].args == ("POST", "/agents/run")
    assert request.await_args_list[1].args == (
        "GET",
        "/agents/run/status/run-1",
    )


@pytest.mark.asyncio
async def test_audit_issue_starts_and_polls_auditor():
    responses = [
        {"run_id": "audit-1", "status": "running"},
        {"run_id": "audit-1", "status": "complete", "findings": [{"issue": 42}]},
    ]

    with (
        patch.object(
            mcp, "_request_json", new_callable=AsyncMock, side_effect=responses
        ) as request,
        patch.object(mcp.asyncio, "sleep", new_callable=AsyncMock),
    ):
        result = await mcp._call_tool(
            "audit_issue",
            {
                "task": "Audit issue 42",
                "repo": "ryemyster/context-manager",
                "paths": ["context-engine/app"],
            },
        )

    assert result["findings"] == [{"issue": 42}]
    assert request.await_args_list[0].args == (
        "POST",
        "/agents/issue-auditor/run",
    )
    assert request.await_args_list[1].args == (
        "GET",
        "/agents/issue-auditor/status/audit-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "endpoint"),
    [
        ("load_context", "/context"),
        ("review_diff", "/diff-summary"),
        ("scan_directory", "/scan"),
        ("find_in_code", "/find"),
        ("summarize_file", "/summarize"),
        ("dependency_analysis", "/dependencies"),
        ("vector_search", "/vector-search"),
    ],
)
async def test_sync_tools_route_to_existing_rest_endpoints(tool, endpoint):
    with patch.object(
        mcp,
        "_request_json",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as request:
        result = await mcp._call_tool(tool, {"value": "unchanged"})

    assert result == {"ok": True}
    request.assert_awaited_once_with(
        "POST",
        endpoint,
        payload={"value": "unchanged"},
    )


@pytest.mark.asyncio
async def test_tools_call_returns_json_text_content():
    payload = {
        "final_answer": "Evidence-based answer",
        "tool_calls_made": [],
        "verification": {"passed": True},
        "iterations": 1,
        "plan_state": {},
    }
    with patch.object(
        mcp, "_call_tool", new_callable=AsyncMock, return_value=payload
    ):
        response = await mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "investigate_codebase",
                    "arguments": {"task": "Investigate"},
                },
            }
        )

    result = response["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == payload


@pytest.mark.asyncio
async def test_tools_call_adapter_error_sets_is_error():
    with patch.object(
        mcp,
        "_call_tool",
        new_callable=AsyncMock,
        side_effect=mcp.AdapterError("engine unavailable"),
    ):
        response = await mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "load_context", "arguments": {"task": "x"}},
            }
        )

    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "engine unavailable"


@pytest.mark.asyncio
async def test_tools_call_rejects_unknown_tool():
    response = await mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {}},
        }
    )

    assert response["error"]["code"] == -32602
    assert "unknown tool" in response["error"]["message"]


@pytest.mark.asyncio
async def test_request_json_uses_api_key_and_returns_dict():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"ok": True}

    with (
        patch.object(mcp, "ENGINE_API_KEY", "secret"),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.request.return_value = response
        client_cls.return_value.__aenter__.return_value = client

        result = await mcp._request_json("POST", "/context", payload={"task": "x"})

    assert result == {"ok": True}
    assert client_cls.call_args.kwargs["headers"]["X-API-Key"] == "secret"
    client.request.assert_awaited_once_with(
        "POST",
        "/context",
        json={"task": "x"},
    )


@pytest.mark.asyncio
async def test_request_json_converts_http_error_to_adapter_error():
    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.request.side_effect = httpx.ConnectError("refused")
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(mcp.AdapterError, match="unreachable"):
            await mcp._request_json("GET", "/healthcheck")


@pytest.mark.asyncio
async def test_ping_notification_and_unknown_method():
    ping = await mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "ping"})
    notification = await mcp.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    unknown = await mcp.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "unknown/method"}
    )

    assert ping["result"] == {}
    assert notification is None
    assert unknown["error"]["code"] == -32601
