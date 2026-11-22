"""OCR providers shared by screenshot and document route chains."""

from __future__ import annotations

from pathlib import Path
import sys
from argparse import Namespace

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendHealth
from openmd.ocr.pipeline import LocalOCRBackend


class OCRRouterProvider:
    def __init__(self, name: str, options) -> None:
        self.name, self.options = name, options

    def descriptor(self):
        return BackendDescriptor(name=self.name, layer="local", provider="openmd.ocr", description="Local OCR using the selected engine with page receipts.", capabilities=(BackendCapability(platforms=("screenshot", "pdf"), source_types=("image_text", "document"), input_types=("single", "batch", "collection", "generic"), source_levels=("ocr",), requires_network=self.options.ocr_engine != "mineru"),))

    def health(self):
        if self.options.ocr_engine == "mineru":
            from openmd.engines.tools import MinerUEngine
            report = MinerUEngine().available()
            return BackendHealth(ok=report["ok"], dependencies={"mineru": str(report.get("version"))},
                error_code=None if report["ok"] else "engine_unavailable", error_summary=report["reason"])
        if not self.options.enable_network_backends:
            return BackendHealth(ok=False, error_code="ocr_permission_required", error_summary="Enable network backends explicitly to call the configured Ollama API.")
        from openmd.ocr.config import settings
        from openmd.ocr.ollama import probe_ollama

        report = probe_ollama(settings())
        key = "minicpm_model" if self.name == "ocr.minicpm" else "ovis_model"
        ready = bool(report["models"].get(key, {}).get("available"))
        return BackendHealth(ok=ready, dependencies={"ollama": str(report.get("version"))}, error_code=None if ready else report.get("error_code"))

    def extract(self, request):
        if self.options.ocr_engine == "mineru":
            source = request.item.raw_meta.get("local_path") or request.item.url
            from openmd.engines.tools import MinerUEngine
            from openmd.engines.base import EngineLimits
            from openmd.core.local_config import load_local_config
            configured = load_local_config().get("tools", {}).get("mineru", {})
            tier = configured.get("tier", "basic") if isinstance(configured, dict) else "basic"
            return MinerUEngine().convert(Path(source), EngineLimits(), tier=tier)
        if request.item.platform == "pdf":
            from openmd.ocr.documents import extract_pdf
            result = extract_pdf(request.item, backend=self.name, allow_ocr=self.options.enable_network_backends)
            pages = result.metadata.get("pages", []) if isinstance(result.metadata, dict) else []
            missing = result.error_code == "ollama_model_missing" or any(
                "ollama_model_missing" in str(page.get("reason", "")) for page in pages if isinstance(page, dict)
            )
            unreachable = result.error_code == "ollama_not_reachable" or any(
                "ollama_not_reachable" in str(page.get("reason", "")) for page in pages if isinstance(page, dict)
            )
            if missing and self.name == "ocr.chain":
                from openmd.runtime.recommendations import cmd_models_install, install_plan

                install_command = "openmd models install ocr:chain"
                plan = install_plan("ocr:chain")
                total = int(plan.get("estimated_size_bytes") or 0)
                size_text = f"约 {total / 1024**3:.1f} GiB（链路合计估算）" if total else "体积由 Ollama 下载进度显示"
                question = (f"尚未安装扫描件 OCR 模型：Ovis3、MiniCPM 与 PP-DocLayoutV3，{size_text}；"
                            f"布局权重将放在 {plan['root']}，Ollama 模型由 Ollama 自身管理。布局 CPU runtime 需单独安装到 D 盘。现在安装并继续吗？[y/N] ")
                if sys.stdin.isatty():
                    try:
                        answer = input(question)
                    except EOFError:
                        answer = ""
                    if answer.strip().lower() in {"y", "yes"}:
                        if cmd_models_install(Namespace(tier="ocr:chain", root=None, dry_run=False, _confirmed=True)) == 0:
                            return extract_pdf(request.item, backend=self.name, allow_ocr=self.options.enable_network_backends)
                else:
                    result.source_notes.append(f"Install local OCR models and retry: {install_command}")
            elif unreachable:
                result.source_notes.append("Install and start Ollama locally, then rerun openmd doctor --profile documents.")
            return result
        backend = LocalOCRBackend(self.name)
        if not self.options.enable_network_backends:
            return backend.failed("ocr_permission_required", request.item.title or "screenshot", "Use --enable-network-backends to call the configured Ollama API.")
        source = request.item.raw_meta.get("local_path") or request.item.url
        return backend.recognize(source, title=request.item.title or "screenshot")


def register_ocr_providers(registry, options):
    for name in ("ocr.ovis3", "ocr.minicpm", "ocr.chain"):
        registry.register(name, OCRRouterProvider(name, options))
