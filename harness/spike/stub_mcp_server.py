import os
from mcp.server.mcpserver import MCPServer
mcp = MCPServer("harness")

@mcp.tool()
def describe_sources() -> dict:
    """List the data sources the user can observe. Call this when the user asks what data exists."""
    return {"session": os.environ.get("HARNESS_SESSION"), "sources": [
        {"id": "twitter_firehose", "coverage": "2026-08-17 to 2026-09-17", "tweets": 377270972},
        {"id": "congress", "coverage": "1999-11-29 to 2026-08-24", "tweets": 5095245}]}

if __name__ == "__main__":
    mcp.run()
