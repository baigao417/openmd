"""Optional stdio transport. SDK imports stay behind the extra boundary."""
from __future__ import annotations


def create_server():
    from mcp.server.fastmcp import FastMCP

    return FastMCP("Openmd", log_level="CRITICAL")


def cmd_mcp(args) -> int:
    import sys

    try:
        server = create_server()
    except ModuleNotFoundError as exc:
        if exc.name != "mcp" and not str(exc.name).startswith("mcp."):
            raise
        print('openmd mcp requires the optional extra: pip install "openmd-cli[mcp]"', file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0
