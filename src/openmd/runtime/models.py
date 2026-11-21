"""Resolve ASR models from existing local files without calling a model hub."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class ModelSpec:
    logical_id: str
    repo: str
    family: str
    required: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedModel:
    logical_id: str
    path: Path | None
    origin: str
    missing: tuple[str, ...] = ()
    searched: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id,
            "available": self.path is not None,
            "origin": self.origin,
            "missing": list(self.missing),
            "searched": list(self.searched),
        }


MODEL_SPECS = (
    ModelSpec("whisper-large-v3", "Systran/faster-whisper-large-v3", "whisper", ("model.bin", "config.json", "tokenizer.json")),
    ModelSpec("whisper-large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "whisper", ("model.bin", "config.json", "tokenizer.json")),
    ModelSpec("qwen3-asr-1.7b", "Qwen/Qwen3-ASR-1.7B", "qwen3", ("config.json", "tokenizer_config.json", "preprocessor_config.json")),
    ModelSpec("fsmn-vad-zh", "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", "vad", ("model.pt", "config.yaml", "am.mvn")),
)
_SPECS = {spec.logical_id: spec for spec in MODEL_SPECS}
_ALIASES = {spec.repo: spec.logical_id for spec in MODEL_SPECS}
_ALIASES.update({"large-v3": "whisper-large-v3", "large": "whisper-large-v3", "large-v3-turbo": "whisper-large-v3-turbo", "turbo": "whisper-large-v3-turbo"})


class ModelNotFoundError(FileNotFoundError):
    def __init__(self, resolution: ResolvedModel) -> None:
        self.resolution = resolution
        super().__init__(
            f"Local ASR model {resolution.logical_id!r} is missing or incomplete "
            f"({', '.join(resolution.missing)}). Searched: {', '.join(resolution.searched) or 'explicit path'}. "
            "Set OPENMD_MODEL_ROOTS or the model's OPENMD_*_MODEL variable to an installed local directory; no download was attempted."
        )


def checkout_root() -> Path | None:
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() and (root / "src" / "openmd").is_dir() else None


def _spec_for(value: str, family: str | None) -> ModelSpec:
    logical = _ALIASES.get(value, value)
    if logical in _SPECS:
        return _SPECS[logical]
    explicit = Path(value).expanduser()
    if explicit.is_dir() or explicit.is_absolute() or value.startswith((".", "~")) or "\\" in value:
        known_family = family or "whisper"
        template = _SPECS[{"whisper": "whisper-large-v3", "qwen3": "qwen3-asr-1.7b", "vad": "fsmn-vad-zh"}[known_family]]
        return ModelSpec("local-" + explicit.name, value, known_family, template.required)
    if family == "whisper":
        # Preserve explicit legacy faster-whisper sizes while staying offline.
        repo = value if "/" in value else f"Systran/faster-whisper-{value}"
        return ModelSpec(value, repo, "whisper", ("model.bin", "config.json", "tokenizer.json"))
    fallback = _SPECS["qwen3-asr-1.7b" if family == "qwen3" else "fsmn-vad-zh"]
    return ModelSpec(value, value, family or fallback.family, fallback.required)


def model_label(value: str) -> str:
    """Keep logical model names useful while omitting a machine's full path."""
    path = Path(value).expanduser()
    return "local-" + path.name if path.is_dir() or path.is_absolute() or value.startswith((".", "~")) or "\\" in value else value


def _missing(path: Path, spec: ModelSpec) -> tuple[str, ...]:
    if not path.is_dir():
        return ("directory",)
    missing = [name for name in spec.required if not (path / name).is_file() or (path / name).stat().st_size == 0]
    if spec.family == "qwen3":
        weights = list(path.glob("*.safetensors"))
        if not weights or any(weight.stat().st_size == 0 for weight in weights):
            missing.append("*.safetensors")
        index = path / "model.safetensors.index.json"
        if index.is_file():
            try:
                weight_map = json.loads(index.read_text(encoding="utf-8"))["weight_map"]
                if not isinstance(weight_map, dict) or not weight_map:
                    raise ValueError("empty weight map")
                for name in sorted(set(weight_map.values())):
                    candidate = (path / name).resolve()
                    if not candidate.is_relative_to(path.resolve()) or not candidate.is_file() or candidate.stat().st_size == 0:
                        missing.append("weight shard")
            except (OSError, ValueError, KeyError, TypeError):
                missing.append("valid model.safetensors.index.json")
        if not (path / "tokenizer.json").is_file() and not all((path / name).is_file() for name in ("vocab.json", "merges.txt")):
            missing.append("tokenizer.json or vocab.json + merges.txt")
    return tuple(dict.fromkeys(missing))


def _snapshot_paths(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    yield root
    refs = root / "refs" / "main"
    if refs.is_file():
        revision = refs.read_text(encoding="utf-8").strip()
        snapshot = (root / "snapshots" / revision).resolve()
        if snapshot.is_relative_to(root.resolve()):
            yield snapshot
    yield from sorted((root / "snapshots").glob("*"), reverse=True)


def _local_config() -> dict:
    from openmd.core.local_config import load_local_config

    return load_local_config()


def resolve_model(value: str, *, family: str | None = None) -> ResolvedModel:
    """Return a validated directory or an explicit missing result; never download."""
    spec = _spec_for(value, family)
    explicit = Path(value).expanduser()
    if explicit.is_dir() or explicit.is_absolute() or value.startswith((".", "~")) or "\\" in value:
        missing = _missing(explicit, spec)
        return ResolvedModel(spec.logical_id, explicit.resolve() if not missing else None, "explicit_path", missing, ("explicit_path",))

    candidates: list[tuple[Path, str]] = []
    repo_parts = spec.repo.split("/")
    compact = spec.repo.replace("/", "--")
    configured = _local_config().get("model_roots", [])
    roots = [*(Path(part).expanduser() for part in os.getenv("OPENMD_MODEL_ROOTS", "").split(os.pathsep) if part), *(Path(part).expanduser() for part in configured)]
    if os.getenv("OPENMD_DATA_DIR"):
        roots.append(Path(os.environ["OPENMD_DATA_DIR"]).expanduser() / "models")
    for root in roots:
        candidates.extend((path, "configured_root") for path in (root / spec.logical_id, root / compact, root / "models" / compact, root / ("models--" + compact), root.joinpath(*repo_parts), root / "models" / Path(*repo_parts)))
    checkout = checkout_root()
    if checkout:
        candidates.append((checkout / "out" / "asr-bench" / "models" / "models" / compact, "checkout_model_cache"))
    hf_roots = [Path(os.environ["HF_HUB_CACHE"]).expanduser()] if os.getenv("HF_HUB_CACHE") else []
    if os.getenv("HF_HOME"):
        hf_roots.append(Path(os.environ["HF_HOME"]).expanduser() / "hub")
    hf_roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    candidates.extend((root / ("models--" + compact), "huggingface_cache") for root in hf_roots)
    ms_roots = [Path(os.environ["MODELSCOPE_CACHE"]).expanduser()] if os.getenv("MODELSCOPE_CACHE") else []
    ms_roots.append(Path.home() / ".cache" / "modelscope" / "hub")
    for root in ms_roots:
        candidates.extend((path, "modelscope_cache") for path in (root.joinpath(*repo_parts), root / "models" / Path(*repo_parts), root / "models" / compact))

    searched: list[str] = []
    incomplete: tuple[str, ...] = ()
    for root, origin in candidates:
        if origin not in searched:
            searched.append(origin)
        for candidate in _snapshot_paths(root):
            missing = _missing(candidate, spec)
            if not missing:
                return ResolvedModel(spec.logical_id, candidate.resolve(), origin, (), tuple(searched))
            if candidate.is_dir() and missing != ("directory",):
                incomplete = missing
    return ResolvedModel(spec.logical_id, None, "missing", incomplete or spec.required, tuple(searched))


def require_model(value: str, *, family: str) -> Path:
    resolved = resolve_model(value, family=family)
    if resolved.path is None:
        raise ModelNotFoundError(resolved)
    return resolved.path


def configured_models() -> dict[str, ResolvedModel]:
    return {
        "whisper-large-v3": resolve_model(os.getenv("OPENMD_FASTER_WHISPER_MODEL") or "large-v3", family="whisper"),
        "whisper-large-v3-turbo": resolve_model(os.getenv("OPENMD_ASR_AUTO_EN_MODEL") or "large-v3-turbo", family="whisper"),
        "qwen3-asr-1.7b": resolve_model(os.getenv("OPENMD_QWEN3_MODEL") or "Qwen/Qwen3-ASR-1.7B", family="qwen3"),
        "fsmn-vad-zh": resolve_model(os.getenv("OPENMD_QWEN3_VAD_MODEL") or "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", family="vad"),
    }


def _runnable_whisper_paths() -> list[tuple[Path, str, bool]]:
    configured = configured_models()
    known_paths = {model.path.resolve(): key for key, model in configured.items() if model.path}
    roots = [Path(part).expanduser() for part in os.getenv("OPENMD_MODEL_ROOTS", "").split(os.pathsep) if part]
    roots.extend(Path(item).expanduser() for item in _local_config().get("model_roots", []) if isinstance(item, str))
    roots.extend(Path(part).expanduser() for part in (os.getenv("HF_HUB_CACHE", ""), os.getenv("HF_HOME", "")) if part)
    roots.extend(Path(part).expanduser() for part in (os.getenv("MODELSCOPE_CACHE", ""),) if part)
    data = os.getenv("OPENMD_DATA_DIR")
    if data:
        roots.append(Path(data).expanduser() / "models")
    found: dict[Path, tuple[str, bool]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates = [root, *root.glob("**/model.bin")]
            for candidate in candidates:
                directory = candidate.parent if candidate.name == "model.bin" else candidate
                if directory in found or not directory.is_dir():
                    continue
                if _missing(directory, _spec_for(str(directory), "whisper")):
                    continue
                resolved = directory.resolve()
                logical = known_paths.get(resolved)
                found[resolved] = (logical or directory.name, bool(logical))
        except OSError:
            continue
    return [(path, name, evaluated) for path, (name, evaluated) in sorted(found.items(), key=lambda item: (not item[1][1], item[1][0].casefold()))]


def discover_models() -> list[dict[str, object]]:
    """Classify visible CTranslate2 model directories without opening weights."""
    return [{"name": name, "origin": "evaluated" if evaluated else "unverified", "evaluated": evaluated, "runnable": True}
            for _, name, evaluated in _runnable_whisper_paths()]


@contextmanager
def offline_model_loading() -> Iterator[None]:
    keys = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "MODELSCOPE_OFFLINE")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update({key: "1" for key in keys})
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
