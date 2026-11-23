"""Shared local OCR first-use confirmation for CLI and file-manager callers."""
from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable


def first_use_question(plan: dict) -> str:
    total = int(plan.get("estimated_size_bytes") or 0)
    size_text = f"约 {total / 1024**3:.1f} GiB（链路合计估算）" if total else "体积由安装进度显示"
    runtime = plan.get("runtime") or {}
    runtime_text = (f"layout {runtime.get('selected_device')} runtime {runtime.get('estimated_download')}，"
                    "安装在 D 盘独立 venv。" if runtime else "")
    return (f"尚未安装扫描件 OCR 模型：Ovis3、MiniCPM 与 PP-DocLayoutV3，{size_text}；"
            f"GGUF 文件与版面权重将放在 {plan['root']}，Ollama 模型由本机 Ollama 管理；{runtime_text}现在安装并继续吗？")


def install_on_first_use(*, confirm: Callable[[str], bool] | None = None,
                         interactive: bool = False) -> bool:
    """Ask once, then invoke the canonical installer without prompting twice."""
    from openmd.runtime.recommendations import cmd_models_install, install_plan

    plan = install_plan("ocr:chain")
    if confirm is not None:
        accepted = bool(confirm(first_use_question(plan)))
    elif interactive:
        try:
            accepted = input(first_use_question(plan) + "[y/N] ").strip().casefold() in {"y", "yes"}
        except EOFError:
            accepted = False
    else:
        return False
    if not accepted:
        return False
    return cmd_models_install(Namespace(tier="ocr:chain", root=None, dry_run=False,
                                        device=None, _confirmed=True)) == 0
