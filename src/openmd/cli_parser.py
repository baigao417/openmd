"""CLI parser responsibility; compatibility imports resolve at call time."""
from __future__ import annotations

def build_parser() -> argparse.ArgumentParser:
    from .cli import ADVANCED_COMMANDS, DOCTOR_PROFILES, GO_DAILY_OPTIONS, PLATFORM_IDS, _add_shared_options, argparse, cmd_batch, cmd_capabilities, cmd_collect, cmd_doctor, cmd_ingest, cmd_plan, cmd_report_weixin_author_batch, cmd_routes

    parser = argparse.ArgumentParser(
        prog="openmd",
        usage="openmd [链接]  |  openmd batch <链接清单.txt> --out <目录>",
        description="把链接保存成干净的 Markdown 原文 / 逐字稿。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "日常用法：\n"
            "  openmd                       粘贴链接，回车，选保存位置\n"
            "  openmd \"https://...\"         直接处理这个链接\n"
            "  openmd \"https://...\" -o <目录>   脚本 / Agent：不提问，直接保存\n"
            "  openmd batch urls.txt --out <目录>   一次处理多个链接\n"
            "  openmd doctor                能处理哪些内容、缺什么怎么装\n"
            "\n"
            "浏览器插件：运行 openmd browser-setup，按提示在 Chrome / Edge 加载它打开的文件夹。\n"
            f"高级命令（自动化 / 排障）：{', '.join(ADVANCED_COMMANDS)}；用 openmd <命令> -h 查看。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="{go,batch}", prog="openmd")

    from openmd.runtime.recommendations import cmd_models_install, matrix
    models = subparsers.add_parser('models', description='Local model installation plans; never downloads')
    model_commands = models.add_subparsers(dest='models_command', required=True)
    install = model_commands.add_parser('install', description='Print downloads, estimated sizes and destinations only')
    install.add_argument('tier', choices=sorted(matrix()['tiers']))
    from pathlib import Path
    install.add_argument('--root', type=Path, help='planned model destination; no directory is created')
    install.set_defaults(func=cmd_models_install)

    from .cli_commands import cmd_opencli
    passthrough = subparsers.add_parser("opencli", description="Managed OpenCLI; read-only unless --allow-write")
    passthrough.add_argument("--allow-write", action="store_true")
    passthrough.add_argument("opencli_args", nargs=argparse.REMAINDER)
    passthrough.set_defaults(func=cmd_opencli)

    from openmd.runtime.prepared_models import cmd_prepare_models
    asr = subparsers.add_parser("asr", description="Prepare existing local ASR weights without downloading models")
    asr_commands = asr.add_subparsers(dest="asr_command", required=True)
    prepare = asr_commands.add_parser("prepare-models", description="Write a verified small-shard copy; originals are preserved")
    prepare.add_argument("--model", help="existing model directory or model ID; defaults to OPENMD_QWEN3_MODEL or Qwen/Qwen3-ASR-1.7B")
    prepare.add_argument("--max-shard-mb", type=int, default=512, help="maximum file size in MiB, including header (default 512)")
    prepare.add_argument("--prepared-root", help="prepared model root; configure asr_models.prepared_root to load it later")
    prepare.set_defaults(func=cmd_prepare_models)

    # Advanced commands are registered without help text so the daily help stays short;
    # they keep working exactly as before.
    ingest = subparsers.add_parser("ingest", description="ingest one input")
    ingest.add_argument("url")
    _add_shared_options(ingest)
    ingest.set_defaults(func=cmd_ingest)

    from openmd.interactive import cmd_go

    go = subparsers.add_parser("go", help="处理一个链接，然后问保存位置（默认命令）", description="处理一个链接，然后问保存位置。")
    go.add_argument("url", nargs="?", help="链接或本地文件；不填则提示粘贴")
    _add_shared_options(go, output_required=False, default_limit=1)
    go.add_argument("-o", "--save-to", dest="save_to", help="保存目录；不填则询问（回车用上次目录）")
    go.add_argument("--yes", action="store_true", help="compatibility no-op: a destination is the grant; capability flags narrow it")
    go.add_argument("--json", action="store_true", help="输出机器可读的 JSON 结果")
    go.add_argument("--verbose", action="store_true", help="显示用了哪条工具路线和诊断信息")
    go.add_argument("--video", choices=("note", "text", "file"), default="note", help="帖子带视频时（目前 X）：note 只注明有视频（默认）；text 附上视频逐字稿；file 再保留视频文件")
    go.add_argument("--as-snapshot", action="store_true", help="把本地 HTML 当作浏览器当前页快照处理")
    go.add_argument("--url", dest="snapshot_url", help="快照对应的原始 http(s) URL")
    for action in go._actions:
        if action.dest == "raw":
            action.help = "保留原始逐行时间戳 / 表格，不合并段落"
        elif action.dest not in GO_DAILY_OPTIONS:
            action.help = argparse.SUPPRESS  # still accepted; hidden from daily help
    go.set_defaults(func=cmd_go)

    batch = subparsers.add_parser("batch", help="一次处理清单文件里的多个链接", description="ingest inputs from a text file")
    batch.add_argument("urls_file")
    _add_shared_options(batch)
    batch.set_defaults(func=cmd_batch)

    collect = subparsers.add_parser("collect", description="discover a target, then optionally ingest its items")
    collect.add_argument("url")
    _add_shared_options(
        collect,
        default_limit=20,
        include_manifest_only=True,
        include_discovery_backend=True,
    )
    collect.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="controlled extraction slice size after full discovery; 0 processes the remaining collection",
    )
    collect.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="zero-based extraction slice start after full discovery; default: 0",
    )
    collect.add_argument(
        "--progress",
        action="store_true",
        help="write sanitized per-item progress heartbeats to stderr",
    )
    collect.set_defaults(func=cmd_collect)

    capabilities = subparsers.add_parser("capabilities", description="show read-only capability bindings")
    capabilities.add_argument("--source", help="filter by normalized source id")
    capabilities.add_argument("--format", choices=("json", "table"), default="table")
    capabilities.set_defaults(func=cmd_capabilities)

    plan = subparsers.add_parser("plan", description="build a read-only capability plan")
    plan.add_argument("input")
    plan.add_argument("--input-kind", choices=("auto", "single", "list", "collection", "file", "folder"), default="auto")
    plan.add_argument("--all", action="store_true", help="request source-exhausted planning only")
    plan.add_argument("--gate", action="append", default=[], help="declare a planning gate without executing a provider")
    plan.add_argument("--format", choices=("json", "table"), default="table")
    plan.set_defaults(func=cmd_plan)

    doctor = subparsers.add_parser(
        "doctor",
        description="print local entry and safe session-boundary diagnostics",
    )
    doctor.add_argument("--profile", choices=DOCTOR_PROFILES, default="all")
    doctor.add_argument("--json", action="store_true", help="machine-readable report (default when stdout is not a terminal)")
    doctor.add_argument("--probe-commands", action="store_true", help="explicitly probe installed tool versions")
    doctor.add_argument("--clear-gpu-quarantine", action="store_true", help="explicitly clear only the local GPU quarantine marker; never unload models or contact Ollama")
    doctor.set_defaults(func=cmd_doctor)

    from openmd.probe import cmd_probe

    probe = subparsers.add_parser("probe", description="用本机配置的单条样本探活平台；不下载媒体或运行 ASR。")
    probe.add_argument("--platform", choices=PLATFORM_IDS)
    probe.add_argument("--config", help="本机样本 JSON；默认 OPENMD_PROBE_CONFIG 或 out/probe-config.json")
    probe.add_argument("--out", default="out/probe", help="脱敏 JSON 报告目录（默认 out/probe/<date>/）")
    probe.add_argument("--timeout", type=int, choices=range(1, 301), metavar="1..300", default=90, help="每个平台的最长秒数；默认 90，超时回收该平台调用子树并继续")
    probe.add_argument("--enable-network-backends", action="store_true", help="显式允许现有公开网络读取路线")
    probe.add_argument("--enable-browser-backends", action="store_true", help="显式允许现有浏览器/登录态读取路线")
    probe.add_argument("--json", action="store_true", help="输出脱敏 JSON 回执")
    probe.set_defaults(func=cmd_probe)

    routes = subparsers.add_parser("routes", description="show effective routes or manage benchmark evidence")
    routes.add_argument("action", nargs="?", choices=("bench", "demote", "restore"))
    routes.add_argument("capability_id", nargs="?")
    routes.add_argument("--source")
    routes.add_argument("--capability")
    routes.add_argument("--sample", help="fixed sample identifier for this bounded benchmark")
    routes.add_argument("--samples-file", help="explicit sample registry, including existing local PDF/image fixtures")
    routes.add_argument("--artifacts", help="benchmark artifacts directory")
    routes.add_argument("--format", choices=("json", "table", "markdown"), default="table")
    routes.add_argument("--candidates", help="comma-separated backend capability identifiers")
    routes.add_argument("--evidence", action="append", default=[])
    routes.add_argument("--ledger")
    routes.add_argument("--overlay")
    routes.add_argument("--note", default="")
    for gate in ("network-backends", "browser-backends", "media-download", "asr", "cloud-backends"):
        routes.add_argument(f"--enable-{gate}", action="store_true")
    routes.set_defaults(func=cmd_routes)

    from openmd.browser_setup import cmd_browser_setup

    browser_setup = subparsers.add_parser("browser-setup", description="连接 Chrome / Edge 插件与本机 openmd（只写当前用户设置）")
    browser_setup.add_argument("--uninstall", action="store_true", help="移除本地连接")
    browser_setup.set_defaults(func=cmd_browser_setup)

    report = subparsers.add_parser("report", description="build reports from sanitized runlogs")
    report_subparsers = report.add_subparsers(dest="report_command")
    weixin_author_batch = report_subparsers.add_parser(
        "weixin-author-batch",
        help="summarize Weixin author-batch coverage, recovered fallback, and missing items",
    )
    weixin_author_batch.add_argument(
        "--runlog",
        action="append",
        required=True,
        help="sanitized runlog JSONL path; repeat to merge multiple runs",
    )
    weixin_author_batch.add_argument("--out", required=True, help="report output directory")
    weixin_author_batch.add_argument(
        "--format",
        choices=("both", "json", "markdown"),
        default="both",
        help="report artifact format",
    )
    weixin_author_batch.set_defaults(func=cmd_report_weixin_author_batch)
    return parser


def _add_shared_options(
    parser: argparse.ArgumentParser,
    *,
    default_limit: int | None = None,
    include_manifest_only: bool = False,
    include_discovery_backend: bool = False,
    output_required: bool = True,
) -> None:
    from .cli import DEFAULT_GUARDRAIL_FAILURE_STREAK, DEFAULT_GUARDRAIL_ITEM_THRESHOLD, argparse

    parser.add_argument("--out", required=output_required, help="output directory")
    parser.add_argument("--backend", help="backend name override")
    parser.add_argument("--fallback", dest="fallback", action="store_true", default=True)
    parser.add_argument("--no-fallback", dest="fallback", action="store_false")
    parser.add_argument("--limit", type=int, default=default_limit)
    parser.add_argument(
        "--all",
        action="store_true",
        help="full collection; only runs for explicitly gated providers",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--package",
        action="store_true",
        help="write V1 item.openmd/ package artifacts instead of V0 single Markdown",
    )
    parser.add_argument(
        "--package-root",
        help="package library root; defaults to <out>/packages when --package is used",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="skip existing package_id matches; package mode uses this behavior by default",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="update an existing package in place when package_id already exists",
    )
    parser.add_argument(
        "--new-version",
        action="store_true",
        help="create a versioned package snapshot instead of updating/skipping",
    )
    parser.add_argument("--download-assets", action="store_true")
    parser.add_argument("--raw", action="store_true", help="retain original transcript lines and timestamps without readable grouping")
    parser.add_argument("--keep-raw", action="store_true")
    parser.add_argument("--keep-media", action="store_true")
    parser.add_argument(
        "--enable-media-download",
        action="store_true",
        help="opt in to media download fallback backends; raw media is still deleted unless --keep-media is set",
    )
    parser.add_argument(
        "--enable-asr",
        action="store_true",
        help="opt in to ASR fallback backends after native text/subtitle paths fail",
    )
    parser.add_argument(
        "--enable-cloud-backends",
        action="store_true",
        help="explicitly allow cloud ASR/OCR providers; --enable-asr alone does not allow upload",
    )
    parser.add_argument(
        "--guardrail-item-threshold",
        type=int,
        default=DEFAULT_GUARDRAIL_ITEM_THRESHOLD,
        help="emit a runlog guardrail signal when a batch/collection reaches this item count; 0 disables",
    )
    parser.add_argument(
        "--guardrail-failure-streak",
        type=int,
        default=DEFAULT_GUARDRAIL_FAILURE_STREAK,
        help="pause batch/collection after this many consecutive failed items; 0 disables",
    )
    if include_manifest_only:
        parser.add_argument(
            "--manifest-only",
            action="store_true",
            help="write discovery manifest and skip item extraction",
        )
    if include_discovery_backend:
        parser.add_argument(
            "--discovery-backend",
            help="discovery provider override for collect, independent from --backend",
        )
    parser.add_argument("--runlog", help="runlog JSONL path; defaults to <out>/runlog.jsonl")
    parser.add_argument(
        "--receipt",
        choices=("compact",),
        help="write a bounded per-run receipt and print one compact JSON line",
    )
    parser.add_argument(
        "--enable-network-backends",
        action="store_true",
        help="opt in to real network backends and LibreOffice fetching Office external resources",
    )
    parser.add_argument(
        "--enable-browser-backends",
        action="store_true",
        help="opt in to browser-context backends such as OpenCLI for matching collection inputs",
    )
