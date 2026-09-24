"""Smoke-test project-configured MCP servers without calling the Codex CLI."""

from __future__ import annotations

import asyncio
import os
import threading
import tomllib
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[2]


class SmokePage(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<!doctype html><title>Codex Playwright smoke test</title><h1>Browser MCP is working</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


@asynccontextmanager
async def connect_server(config: dict[str, object]):
    params = StdioServerParameters(
        command=str(config["command"]),
        args=[str(value) for value in config.get("args", [])],
        env={**os.environ, **config.get("env", {})},
        cwd=str(config["cwd"]) if config.get("cwd") else None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def main() -> None:
    with (ROOT / ".codex" / "config.toml").open("rb") as handle:
        servers = tomllib.load(handle)["mcp_servers"]

    async with connect_server(servers["serena"]) as serena:
        serena_tools = {tool.name: tool for tool in (await serena.list_tools()).tools}
        expected = {"initial_instructions", "get_symbols_overview", "find_symbol"}
        missing = expected - serena_tools.keys()
        if missing:
            raise AssertionError(f"Serena is missing expected tools: {sorted(missing)}")
        result = await serena.call_tool("initial_instructions", {})
        if result.isError:
            raise AssertionError(f"Serena initial_instructions failed: {result.content}")
        print(f"PASS Serena MCP: {len(serena_tools)} tools; initial_instructions succeeded")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), SmokePage)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        async with connect_server(servers["playwright"]) as playwright:
            playwright_tools = {tool.name for tool in (await playwright.list_tools()).tools}
            if not {"browser_navigate", "browser_snapshot"}.issubset(playwright_tools):
                raise AssertionError("Playwright MCP did not expose navigation and snapshot tools")
            url = f"http://127.0.0.1:{httpd.server_port}/"
            navigation = await playwright.call_tool("browser_navigate", {"url": url})
            if navigation.isError:
                raise AssertionError(f"Playwright navigation failed: {navigation.content}")
            snapshot = await playwright.call_tool("browser_snapshot", {})
            if snapshot.isError:
                raise AssertionError(f"Playwright snapshot failed: {snapshot.content}")
            snapshot_text = "\n".join(getattr(item, "text", "") for item in snapshot.content)
            if "Browser MCP is working" not in snapshot_text:
                raise AssertionError(f"Smoke page was not present in browser snapshot: {snapshot_text[:500]}")
            print(f"PASS Playwright MCP: {len(playwright_tools)} tools; local page navigation and snapshot succeeded")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    asyncio.run(main())
