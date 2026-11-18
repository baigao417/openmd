"""Optional stdio transport. SDK imports stay behind the extra boundary."""
from __future__ import annotations


def create_server():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    from openmd import mcp_jobs

    server = FastMCP("Openmd", log_level="CRITICAL")
    writes = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
    reads = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    @server.tool(description="Queue a URL or absolute local file to an absolute output directory. Permissions default off.", annotations=writes)
    def openmd_save(source: str, output_directory: str, network: bool = False, browser: bool = False,
                    media: bool = False, asr: bool = False, cloud: bool = False) -> dict[str, object]:
        return mcp_jobs.save(source, output_directory, network=network, browser=browser, media=media, asr=asr, cloud=cloud)

    @server.tool(description="Queue loaded page HTML or an absolute HTML file with its source URL. Limit: 2 MiB UTF-8.", annotations=writes)
    def openmd_save_page(source_url: str, output_directory: str, html: str | None = None, html_path: str | None = None) -> dict[str, object]:
        return mcp_jobs.save_page(source_url, output_directory, html=html, html_path=html_path)

    @server.tool(description="Read job state, receipt and artifact path. Optional Markdown is capped at 32000 characters.", annotations=reads)
    def openmd_job(job_id: str, include_content: bool = False) -> dict[str, object]:
        return mcp_jobs.job(job_id, include_content=include_content)

    @server.tool(description="Read a compact local readiness summary without network or browser probes.", annotations=reads)
    def openmd_doctor() -> dict[str, object]:
        return mcp_jobs.doctor()

    return server


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
