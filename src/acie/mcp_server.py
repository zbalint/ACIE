"""Stdio MCP adapter that forwards ACIE's ten tools to the local daemon."""

import inspect
import json
import os
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from acie.daemon.client import request_daemon
from acie.daemon.dispatch import DISPATCH_TABLE

_DAEMON_INJECTED_PARAMETERS = {
    "symbol_store",
    "relation_store",
    "index_meta_store",
    "files",
    "observed_at",
}


def create_mcp_server(*, discovery_path: str, repo_path: str, log_level: str) -> MCPServer:
    """Create an MCP server for one captured working-directory repository."""
    server = MCPServer("acie", log_level=log_level)
    annotations = ToolAnnotations(read_only_hint=True)
    for method, tool in DISPATCH_TABLE.items():
        server.tool(name=method, annotations=annotations, structured_output=True)(
            _daemon_tool(method, tool, discovery_path, repo_path)
        )
    return server


def _daemon_tool(
    method: str, tool: Callable[..., dict], discovery_path: str, repo_path: str
) -> Callable[..., dict]:
    def call(**params) -> dict | CallToolResult:
        response = request_daemon(
            discovery_path, method=method, repo_path=repo_path, params=params
        )
        if response is None:
            return _error_result(
                {"code": "DAEMON_UNAVAILABLE", "message": "ACIE daemon is unavailable"}
            )
        if response.get("ok") is True:
            return response["result"]
        return _error_result(
            response.get(
                "error", {"code": "INTERNAL_ERROR", "message": "invalid daemon response"}
            )
        )

    public_parameters = [
        parameter
        for name, parameter in inspect.signature(tool).parameters.items()
        if name not in _DAEMON_INJECTED_PARAMETERS
    ]
    call.__name__ = method
    call.__doc__ = tool.__doc__ or f"Run ACIE's {method} code-intelligence query."
    call.__signature__ = inspect.Signature(
        public_parameters, return_annotation=dict[str, object]
    )
    return call


def _error_result(error: dict) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(error, sort_keys=True))],
        structured_content={"error": error},
        is_error=True,
    )


def run_stdio_server(*, discovery_path: str, log_level: str) -> None:
    """Run one MCP stdio session, bound to the working directory at startup."""
    repo_path = os.path.realpath(os.getcwd())
    create_mcp_server(
        discovery_path=discovery_path, repo_path=repo_path, log_level=log_level
    ).run("stdio")
