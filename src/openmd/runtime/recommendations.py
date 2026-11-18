"""Read-only hardware detection and explicitly unverified recommendation cells."""
from __future__ import annotations

import json
import os
import platform
from importlib.resources import files
from pathlib import Path
import subprocess


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
    from openmd.core.local_config import load_local_config
    catalog = matrix()['tiers']
    if tier not in catalog:
        raise ValueError('model_tier_unknown')
    configured = load_local_config().get('model_roots', [])
    root = Path(root or os.getenv('OPENMD_MODEL_INSTALL_ROOT') or (configured[0] if configured else Path.home() / '.openmd' / 'models')).expanduser()
    return {'schema': 'openmd.model-install-plan/v1', 'tier': tier, 'plan_only': True, 'download_started': False,
        'root': str(root), 'downloads': [{**entry, 'destination': str(root / entry['directory'])} for entry in catalog[tier]],
        'next_action': 'Review model and dependency licenses, then install explicitly in a local runtime. This command only prints a plan.'}


def cmd_models_install(args) -> int:
    print(json.dumps(install_plan(args.tier, root=args.root), ensure_ascii=True, sort_keys=True))
    return 0
