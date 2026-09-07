import socket
import subprocess
import sys

import anyio

from acie.cli import main
from acie.mcp_server import _daemon_tool
from acie.daemon.dispatch import DISPATCH_TABLE

from acie.tools.architecture import architecture
from acie.tools.structural_search import structural_search


def _free_port() -> int:
    """Reserves an ephemeral port number, then releases it immediately."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port

def test_every_dispatched_tool_has_a_caller_visible_docstring():
    assert {method for method, tool in DISPATCH_TABLE.items() if not tool.__doc__} == set()


def test_architecture_public_schema_excludes_the_dispatch_only_repo_root_seam():
    # Review finding (P2, this session): `repo_root` was added to
    # `architecture()` as a dispatch-injected seam (C5's layering-
    # violation detection reads `.acie/config.json` off disk via it) but
    # `_DAEMON_INJECTED_PARAMETERS` wasn't updated to exclude it, so the
    # MCP adapter's schema-derivation loop in `_daemon_tool` would have
    # exposed it as a client-settable input despite dispatch.py always
    # overwriting whatever value a client sent. A lightweight unit test
    # against `_daemon_tool` directly (not the flaky full stdio-subprocess
    # integration test below) so this regresses fast and deterministically.
    call = _daemon_tool("architecture", architecture, discovery_path="unused", repo_path="unused")

    public_names = {parameter.name for parameter in call.__signature__.parameters.values()}

    assert public_names == {"root", "granularity", "node_cap", "full"}
    assert "repo_root" not in public_names

def test_structural_search_public_schema_and_docstring_describe_daemon_owned_files():
    call = _daemon_tool(
        "structural_search", structural_search, discovery_path="unused", repo_path="unused"
    )

    public_names = {parameter.name for parameter in call.__signature__.parameters.values()}

    assert "files" not in public_names
    assert call.__doc__ is not None
    doc = " ".join(call.__doc__.split())
    assert "not a parameter this tool accepts" in doc
    assert "caller-supplied" not in doc

def test_structural_search_daemon_wrapper_uses_extended_timeout(monkeypatch):
    timeouts = []

    def fake_request(discovery_path, *, method, repo_path, params, timeout=None):
        timeouts.append((method, timeout))
        return {"ok": True, "result": {}}

    monkeypatch.setattr("acie.mcp_server.request_daemon", fake_request)
    call = _daemon_tool("structural_search", structural_search, "unused", "unused")

    assert call(pattern="(function_definition) @fn") == {}
    assert timeouts == [("structural_search", 10.0)]


def test_other_daemon_tool_wrappers_keep_the_default_timeout(monkeypatch):
    timeouts = []

    def fake_request(discovery_path, *, method, repo_path, params, timeout=None):
        timeouts.append((method, timeout))
        return {"ok": True, "result": {}}

    monkeypatch.setattr("acie.mcp_server.request_daemon", fake_request)
    call = _daemon_tool("architecture", architecture, "unused", "unused")

    assert call() == {}
    assert timeouts == [("architecture", 2.0)]



def test_serve_mcp_exposes_and_routes_the_ten_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")

    async def exercise_stdio_server():
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "acie", "serve-mcp"],
            env={
                "HOME": str(tmp_path / "home"),
                "ACIE_DAEMON_ELECTION_PORT": str(_free_port()),
            },
            cwd=repo,
        )
        async with stdio_client(server) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "find_symbol",
                    "get_definition",
                    "find_references",
                    "list_imports",
                    "structural_search",
                    "graph",
                    "impact_analysis",
                    "explain",
                    "affected_tests",
                    "architecture",
                }

                initial_result = await session.call_tool("find_symbol", {"name": "target"})
                assert initial_result.is_error is True
                assert initial_result.structured_content["error"]["code"] == "INDEX_NOT_READY"

                last_result = None
                for _ in range(200):
                    result = await session.call_tool("find_symbol", {"name": "target"})
                    if result.structured_content and "error" not in result.structured_content:
                        last_result = result
                        break
                    await anyio.sleep(0.01)
                assert last_result is not None, result
                assert last_result.structured_content["results"] == [
                    {
                        "id": "module.py:target#function",
                        "path": "module.py",
                        "qualname": "target",
                        "kind": "function",
                        "start_line": 1,
                        "start_col": 0,
                        "end_line": 2,
                        "end_col": 8,
                    }
                ]

    try:
        anyio.run(exercise_stdio_server)
    finally:
        main(["daemon", "stop"])
