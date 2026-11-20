"""Optional stdio transport. SDK imports stay behind the extra boundary."""
from __future__ import annotations

import asyncio


_progress_tasks: set[asyncio.Task] = set()


def create_server():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp import Context
    from mcp.types import ToolAnnotations
    from mcp.server.fastmcp.exceptions import ToolError
    from openmd import mcp_jobs

    # FastMCP evaluates postponed annotations in the module globals. Keep the
    # optional SDK import lazy while making the Context annotation resolvable.
    globals()["Context"] = Context

    class PrivateFastMCP(FastMCP):
        async def call_tool(self, name, arguments):
            try:
                return await super().call_tool(name, arguments)
            except Exception:
                # SDK validation errors can include the rejected HTML or OS path.
                # Never forward exception text or its chained traceback to clients.
                raise ToolError("openmd_tool_failed: check typed arguments, limits and local readiness") from None

    server = PrivateFastMCP("Openmd", log_level="CRITICAL")
    writes = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
    reads = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    async def watch(ctx: Context, ids: list[str]) -> None:
        total = len(ids)
        if not total:
            return
        pending = set(ids)
        last = -1
        for _ in range(1200):
            done = 0
            for job_id in tuple(pending):
                state = mcp_jobs.job(job_id).get("state")
                if state in {"done", "failed", "cancelled", "unknown", "paused"}:
                    pending.discard(job_id)
                    done += 1
            completed = total - len(pending)
            if completed != last:
                last = completed
                await ctx.report_progress(completed, total, f"Openmd jobs: {completed}/{total}")
            if not pending:
                return
            await asyncio.sleep(0.25)

    def schedule_watch(ctx: Context, result: dict[str, object]) -> None:
        ids = []
        if isinstance(result.get("id"), str):
            ids = [result["id"]]
        elif isinstance(result.get("jobs"), list):
            ids = [item["id"] for item in result["jobs"] if isinstance(item, dict) and isinstance(item.get("id"), str)]
        if not ids:
            return
        task = asyncio.create_task(watch(ctx, ids))
        _progress_tasks.add(task)
        task.add_done_callback(_progress_tasks.discard)

    @server.tool(description="Queue one URL/file or up to 20 sources to an absolute output directory; returns job IDs. Permissions default off. Set ocr_engine='mineru' to use a user-installed local MinerU executable.", annotations=writes, structured_output=True)
    async def openmd_save(ctx: Context, source: str | None = None, output_directory: str = "",
                          sources: list[str] | None = None, network: bool = False, browser: bool = False,
                          media: bool = False, asr: bool = False, cloud: bool = False,
                          ocr_engine: str | None = None) -> dict[str, object]:
        if ocr_engine not in {None, "auto", "mineru"}:
            raise ValueError("unsupported_ocr_engine")
        if sources is not None:
            result = mcp_jobs.save_batch(sources, output_directory, network=network, browser=browser, media=media, asr=asr, cloud=cloud, ocr_engine=ocr_engine)
        elif source is not None:
            result = mcp_jobs.save(source, output_directory, network=network, browser=browser, media=media, asr=asr, cloud=cloud, ocr_engine=ocr_engine)
        else:
            raise ValueError("source_or_sources_required")
        await ctx.report_progress(0, len(result.get("jobs", [result])))
        schedule_watch(ctx, result)
        return result

    @server.tool(description="Queue loaded page HTML or an absolute HTML file with its source URL. Limit: 2 MiB UTF-8.", annotations=writes, structured_output=True)
    async def openmd_save_page(ctx: Context, source_url: str, output_directory: str, html: str | None = None, html_path: str | None = None) -> dict[str, object]:
        result = mcp_jobs.save_page(source_url, output_directory, html=html, html_path=html_path)
        await ctx.report_progress(0, 1)
        schedule_watch(ctx, result)
        return result

    @server.tool(description="Read or cancel a job; optional Markdown is capped at 32000 characters.", annotations=reads, structured_output=True)
    async def openmd_job(ctx: Context, job_id: str, action: str = "status", include_content: bool = False) -> dict[str, object]:
        result = mcp_jobs.job(job_id, action=action, include_content=include_content)
        state = result.get("state")
        await ctx.report_progress(100 if state in {"done", "failed", "cancelled", "unknown"} else 0,
                                  100, f"Openmd job {state}")
        return result

    @server.tool(description="Read a compact local readiness summary without network or browser probes.", annotations=reads, structured_output=True)
    def openmd_doctor() -> dict[str, object]:
        return mcp_jobs.doctor()

    @server.resource("openmd://jobs", name="openmd_jobs", title="Openmd MCP jobs", description="Index of completed Markdown artifacts produced by this MCP session.", mime_type="application/json")
    def openmd_jobs_resource() -> str:
        return mcp_jobs.resource_index()

    @server.resource("openmd://job/{job_id}", name="openmd_job_artifact", title="Openmd MCP job artifact", description="A bounded Markdown artifact produced by an Openmd MCP job.", mime_type="text/markdown")
    def openmd_job_resource(job_id: str) -> str:
        return mcp_jobs.resource_content(job_id)

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
