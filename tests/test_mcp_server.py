import subprocess
import sys

import anyio

from acie.cli import main


def test_serve_mcp_exposes_and_routes_the_eight_tools(monkeypatch, tmp_path):
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
