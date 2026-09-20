# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2", "duckdb==1.5.5", "jsonschema>=4.23,<5"]
# ///
"""Proposal-only tool server. It has no collector, inference, or submission tools."""
from mcp.server.mcpserver import MCPServer
import automation_tools

mcp = MCPServer("harness")
automation_tools.register(mcp)

if __name__ == "__main__":
    mcp.run()
