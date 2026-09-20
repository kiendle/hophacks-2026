# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "duckdb==1.5.5",
#   "jsonschema>=4.23,<5",
#   "httpx>=0.28,<0.29",
#   "aiohttp>=3.12,<4",
#   "websockets>=15,<16",
#   "mcp>=1.28,<2",
#   "pydantic>=2.11,<3",
# ]
# ///
"""Local shared-firehose automation CLI and MCP server.

Run `uv run bluesky-automation/automation.py --help` for commands.
The foreground `serve` command owns the only upstream connection.
"""

from frontend import main


if __name__ == "__main__":
    raise SystemExit(main())
