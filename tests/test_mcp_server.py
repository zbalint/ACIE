import subprocess
import sys

import anyio

from acie.cli import main
from acie.mcp_server import _daemon_tool
from acie.tools.architecture import architecture


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
            env={"HOME": str(tmp_path / "home")},
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
