"""Local stdio MCP server used only by the integration test suite."""

import asyncio

from mcp.server import MCPServer


server = MCPServer("job-agent-mcp-test")


@server.tool(structured_output=True)
def echo_text(text: str) -> dict[str, str]:
    """Echo text supplied by the caller."""
    return {"text": text}


@server.tool(structured_output=True)
def add_numbers(left: int, right: int) -> dict[str, int]:
    """Add two integers."""
    return {"sum": left + right}


@server.tool(structured_output=True)
async def slow_echo(text: str, delay_seconds: float) -> dict[str, str]:
    """Delay an echo so timeout behavior can be characterized."""
    await asyncio.sleep(delay_seconds)
    return {"text": text}


if __name__ == "__main__":
    server.run(transport="stdio")
