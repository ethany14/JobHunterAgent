"""Harmless local MCP server for manual JobHunterAgent development tests."""

from mcp.server import MCPServer


server = MCPServer("jobhunteragent-local-test")


@server.tool(structured_output=True)
def echo_text(text: str) -> dict[str, str]:
    """Return the supplied text unchanged."""
    return {"text": text}


@server.tool(structured_output=True)
def add_numbers(left: int, right: int) -> dict[str, int]:
    """Return the sum of two integers."""
    return {"sum": left + right}


if __name__ == "__main__":
    server.run(transport="stdio")
