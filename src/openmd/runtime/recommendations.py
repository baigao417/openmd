"""Read-only hardware detection and explicitly unverified recommendation cells."""
from __future__ import annotations

import json
import hashlib
import os
import platform
from importlib.resources import files
from pathlib import Path
import subprocess
import sys


def hardware() -> dict:
    from .interpreters import run_interruptibly
    result = {'os': platform.system(), 'cpu': platform.processor(), 'ram_gib': None, 'gpus': [], 'errors': []}
    try:
        import psutil
        result['ram_gib'] = round(psutil.virtual_memory().total / 2**30, 2)
        result['ram_method'] = 'psutil.virtual_memory'
    except ImportError:
        try:
            if os.name == 'nt':
                import ctypes
                class Memory(ctypes.Structure):
                    _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                        *[(key, ctypes.c_ulonglong) for key in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]]
                status = Memory()
                status.length = ctypes.sizeof(status)
                if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    raise OSError('memory_probe_failed')
                total = status.total
            else:
                total = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
            result['ram_gib'] = round(total / 2**30, 2)
            result['ram_method'] = 'native_physical_memory'
        except (AttributeError, OSError, ValueError, KeyError, TypeError):
            result['errors'].append('ram_unavailable')
    try:
        proc = run_interruptibly(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=3)
        if proc.returncode != 0:
            raise ValueError('gpu_probe_failed')
        for line in proc.stdout.splitlines():
            name, memory = line.rsplit(',', 1)
            result['gpus'].append({'name': name.strip(), 'vram_mib': float(memory.strip())})
    except (OSError, ValueError, subprocess.SubprocessError):
        result['errors'].append('nvidia_gpu_unavailable')
    return result


def matrix() -> dict:
    return json.loads(files('openmd.runtime').joinpath('recommendations.json').read_text(encoding='utf-8'))


def recommend(machine: dict) -> dict:
    memory = machine.get('ram_gib')
    if memory is None:
        return {'ocr': 'ocr:baseline', 'asr': 'asr:baseline', 'validation': '未验证', 'evidence': [], 'reason': 'Physical memory could not be measured.'}
    vram = max((gpu.get('vram_mib', 0) for gpu in machine.get('gpus', [])), default=0)
    vb = '0-4' if vram < 4096 else '4-8' if vram < 8192 else '8-12' if vram < 12288 else '12+'
    rb = '0-16' if memory < 16 else '16-32' if memory < 32 else '32+'
    cell = next((cell for cell in matrix()['cells'] if (cell['os'], cell['vram_gib'], cell['ram_gib']) == (machine['os'], vb, rb)), None)
    if cell is None:
        return {'ocr': 'ocr:baseline', 'asr': 'asr:baseline', 'validation': '未验证', 'evidence': [], 'reason': 'No matching operating system cell.'}
    names = {gpu['name'] for gpu in machine.get('gpus', [])}
    evidence = [entry for entry in cell['evidence'] if entry.get('gpu') in names and abs(memory - entry.get('ram_gib', 0)) < 1]
    return {**cell, 'evidence': evidence, 'validation': '部分实测' if evidence else '未验证',
        'ocr_validation': cell['ocr_validation'] if evidence else '未验证',
        'asr_validation': cell['asr_validation'] if evidence else '未验证',
        'reason': 'Conservative local baseline. Cell evidence applies only to its named machine and measured task; ASR and other devices remain unverified.'}


def install_plan(tier: str, *, root: Path | None = None) -> dict:
    catalog = matrix()['tiers']
    if tier not in catalog:
        raise ValueError('model_tier_unknown')
    from openmd.core.preferences import data_directory

    root = Path(root or os.getenv('OPENMD_MODEL_INSTALL_ROOT') or data_directory() / 'models').expanduser()
    return {'schema': 'openmd.model-install-plan/v1', 'tier': tier, 'plan_only': True, 'download_started': False,
        'root': str(root), 'downloads': [{**entry, 'destination': str(root / entry['directory'])} for entry in catalog[tier]],
        'next_action': 'Review model and dependency licenses, then install explicitly in a local runtime. This command only prints a plan.'}


_PROBE_BYTES = 2 * 1024 * 1024


def _probe_throughput(url: str, timeout: float = 10.0) -> float | None:
    """Bytes/second over the first ~2 MB of ``url``; None when nothing arrives."""
    import time
    import urllib.request

    request = urllib.request.Request(url, headers={"Range": f"bytes=0-{_PROBE_BYTES - 1}", "User-Agent": "openmd-model-probe"})
    start = time.monotonic()
    received = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while received < _PROBE_BYTES and time.monotonic() - start < timeout:
                chunk = response.read(65536)
                if not chunk:
                    break
                received += len(chunk)
    except Exception:
        pass
    return received / max(time.monotonic() - start, 1e-3) if received else None


def rank_sources(item: dict, *, modelscope_allowed: bool) -> list[tuple[str, float | None]]:
    """Order download sources by measured speed, slow or unreachable ones last.

    Failure-only fallback left new users in China on a 10 KB/s HuggingFace
    route for hours while hf-mirror or ModelScope was fast (round14, 2026-11-22).
    """
    from concurrent.futures import ThreadPoolExecutor

    endpoints = ["https://huggingface.co", "https://hf-mirror.com"]
    configured = os.environ.get("HF_ENDPOINT", "").rstrip("/")
    if configured and configured not in endpoints:
        endpoints.insert(0, configured)
    probe_file = item.get("probe_file", "model.bin")
    urls = {endpoint: f"{endpoint}/{item['repository']}/resolve/{item.get('revision') or 'main'}/{probe_file}" for endpoint in endpoints}
    if modelscope_allowed:
        urls["modelscope"] = (f"https://www.modelscope.cn/models/{item['modelscope_repository']}/resolve/"
                              f"{item.get('modelscope_revision') or 'master'}/{probe_file}")
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        speeds = dict(zip(urls, pool.map(_probe_throughput, urls.values())))
    order = list(urls)
    return sorted(((name, speeds[name]) for name in order),
                  key=lambda pair: (pair[1] is None, -(pair[1] or 0), order.index(pair[0])))


def cmd_models_install(args) -> int:
    plan = install_plan(args.tier, root=args.root)
    if getattr(args, "dry_run", False):
        print(json.dumps(plan, ensure_ascii=True, sort_keys=True))
        return 0
    if not args.tier.startswith("asr:"):
        raise ValueError("model_tier_download_unsupported: only ASR model downloads are currently supported")
    if not sys.stdin.isatty() and not getattr(args, "_confirmed", False):
        print(json.dumps({**plan, "next_action": f"Run interactively: openmd models install {args.tier}"}, ensure_ascii=True, sort_keys=True))
        return 2
    total = sum(int(item.get("size_bytes", 0)) for item in plan["downloads"])
    print(f"将下载约 {total / 1024**3:.1f} GiB 到 {plan['root']}。模型会在本机处理音频。")
    if not getattr(args, "_confirmed", False):
        try:
            answer = input("继续？[y/N] ")
        except EOFError:
            print(json.dumps({**plan, "next_action": f"Run interactively: openmd models install {args.tier}"}, ensure_ascii=True, sort_keys=True))
            return 2
        if answer.strip().lower() not in {"y", "yes"}:
            print(f"以后可运行：openmd models install {args.tier}")
            return 1
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError('安装模型需要 ASR extra：pip install "openmd-cli[asr]"') from exc
    try:
        from modelscope_hub.compat import snapshot_download as modelscope_snapshot_download
    except ImportError:
        modelscope_snapshot_download = None
    receipts = []
    for item in plan["downloads"]:
        destination = Path(item["destination"])
        destination.mkdir(parents=True, exist_ok=True)
        last_error = None
        attempts = []
        source = None
        expected_sha256 = item.get("sha256")
        modelscope_repository = item.get("modelscope_repository")
        # ModelScope is a different host for the same weights, so it is only
        # used when model.bin can be checked against the pinned SHA-256.
        modelscope_allowed = (modelscope_snapshot_download is not None
                              and isinstance(modelscope_repository, str)
                              and isinstance(expected_sha256, str)
                              and len(expected_sha256) == 64
                              and all(ch in "0123456789abcdefABCDEF" for ch in expected_sha256))
        ranked = rank_sources(item, modelscope_allowed=modelscope_allowed)
        print("下载源测速：" + "，".join(f"{name.removeprefix('https://')} "
                                     + (f"{speed / 1e6:.1f} MB/s" if speed else "不可达") for name, speed in ranked),
              file=sys.stderr, flush=True)
        for name, _speed in ranked:
            attempts.append(name.removeprefix("https://"))
            try:
                if name == "modelscope":
                    modelscope_snapshot_download(model_id=modelscope_repository,
                                                 revision=item.get("modelscope_revision", item.get("revision")),
                                                 local_dir=str(destination))
                else:
                    snapshot_download(repo_id=item["repository"], revision=item.get("revision"), local_dir=str(destination), endpoint=name)
                last_error = None
                source = name.removeprefix("https://")
                break
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            routes = ", ".join(attempts) or "no available mirror"
            raise RuntimeError(f"model_download_failed: {item['repository']}; tried {routes}; retry with openmd models install {args.tier}") from last_error
        from .models import _missing, _spec_for

        missing = _missing(destination, _spec_for(item["repository"], "whisper"))
        if missing:
            raise RuntimeError(f"model_integrity_check_failed: {item['repository']} missing {', '.join(missing)}")
        verified = "required model files present"
        if isinstance(expected_sha256, str) and len(expected_sha256) == 64:
            model_file = destination / "model.bin"
            digest = hashlib.sha256()
            with model_file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual_sha256 = digest.hexdigest()
            if actual_sha256.casefold() != expected_sha256.casefold():
                raise RuntimeError(f"model_integrity_check_failed: {item['repository']} model.bin sha256 mismatch")
            verified = "required model files and model.bin sha256"
        receipts.append({"repository": item["repository"], "destination": str(destination), "verified": verified,
                         "source": source, "attempts": attempts,
                         **({"model_sha256": expected_sha256.lower()} if verified.endswith("sha256") else {})})
    print(json.dumps({"schema": "openmd.model-install-receipt/v1", "tier": args.tier, "downloads": receipts}, ensure_ascii=True, sort_keys=True))
    return 0
