"""Offline safetensors resharding with bounded byte copies and verified publication.

No tensor framework is imported: dtype, shape and raw bytes are preserved. A tensor
is indivisible; an individual tensor exceeding the cap fails before publication.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import tempfile
from pathlib import Path
from typing import BinaryIO

DEFAULT_SHARD_BYTES = 512 * 1024**2
RECEIPT = "openmd-prepared.json"
SCHEMA = "openmd.prepared-model/v1"
_DTYPES = {"BOOL": 1, "U8": 1, "I8": 1, "F8_E4M3": 1, "F8_E5M2": 1,
           "I16": 2, "U16": 2, "F16": 2, "BF16": 2, "I32": 4, "U32": 4,
           "F32": 4, "I64": 8, "U64": 8, "F64": 8}
_AUXILIARY = {"config.json", "configuration.json", "chat_template.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
              "preprocessor_config.json", "processor_config.json", "vocab.json", "merges.txt",
              "added_tokens.json", "special_tokens_map.json", "chat_template.jinja", "tokenizer.model"}


def prepared_root() -> Path:
    from openmd.core.local_config import load_local_config
    setting = load_local_config().get("asr_models", {})
    return Path(os.getenv("OPENMD_PREPARED_MODEL_ROOT") or setting.get("prepared_root")
                or Path.home() / ".openmd/models/prepared").expanduser().resolve()


def prepared_path(source: Path, label: str) -> Path:
    # Explicit models with the same basename still have distinct destinations.
    suffix = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:12]
    name = re.sub(r"[^a-zA-Z0-9_.-]", "-", label)[:80].strip(".-") or "model"
    return prepared_root() / f"{name}-{suffix}-v1"


def _child(root: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or "\\" in name or "/" in name:
        raise ValueError("invalid model file name")
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("model file is missing or outside its directory")
    return path


def _json(path: Path) -> dict:
    if path.stat().st_size > 32 * 1024**2:
        raise ValueError("model metadata exceeds size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid model metadata")
    return value


def _header(path: Path) -> tuple[dict, int]:
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ValueError("invalid safetensors prefix")
        size = struct.unpack("<Q", prefix)[0]
        if size > 32 * 1024**2 or size > path.stat().st_size - 8:
            raise ValueError("invalid safetensors header size")
        header = json.loads(stream.read(size))
    if not isinstance(header, dict):
        raise ValueError("invalid safetensors header")
    return header, size + 8


def _inventory(root: Path) -> tuple[dict, dict]:
    index = root / "model.safetensors.index.json"
    mapping = _json(index).get("weight_map") if index.is_file() else None
    if index.is_file() and (not isinstance(mapping, dict) or not mapping):
        raise ValueError("invalid safetensors index")
    names = sorted(set(mapping.values())) if mapping else ["model.safetensors"]
    tensors, metadata = {}, {}
    for name in names:
        path = _child(root, name)
        header, base = _header(path)
        meta = header.pop("__metadata__", {})
        if not isinstance(meta, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in meta.items()):
            raise ValueError("invalid safetensors metadata")
        if metadata and meta != metadata:
            raise ValueError("inconsistent safetensors metadata")
        metadata = meta
        spans = []
        for key, info in header.items():
            if key in tensors or not isinstance(info, dict):
                raise ValueError("duplicate or invalid tensor")
            dtype, shape, offsets = info.get("dtype"), info.get("shape"), info.get("data_offsets")
            if (dtype not in _DTYPES or not isinstance(shape, list)
                    or any(type(n) is not int or n < 0 for n in shape)
                    or not isinstance(offsets, list) or len(offsets) != 2
                    or any(type(n) is not int for n in offsets)):
                raise ValueError("invalid tensor dtype, shape or offsets")
            start, end = offsets
            if start < 0 or end < start or end - start != math.prod(shape) * _DTYPES[dtype]:
                raise ValueError("invalid tensor byte length")
            spans.append((start, end))
            tensors[key] = {"dtype": dtype, "shape": shape, "file": name,
                            "offset": base + start, "bytes": end - start}
        position = 0
        for start, end in sorted(spans):
            if start != position:
                raise ValueError("safetensors data has gaps or overlaps")
            position = end
        if base + position != path.stat().st_size:
            raise ValueError("safetensors data length mismatch")
    if not tensors or (mapping is not None and mapping != {k: v["file"] for k, v in tensors.items()}):
        raise ValueError("safetensors index does not match tensors")
    return tensors, metadata


def _copy_range(stream: BinaryIO, offset: int, size: int, output: BinaryIO | None = None) -> str:
    stream.seek(offset)
    digest = hashlib.sha256()
    while size:
        data = stream.read(min(size, 1024**2))
        if not data:
            raise ValueError("truncated model data")
        digest.update(data)
        if output is not None:
            output.write(data)
        size -= len(data)
    return digest.hexdigest()


def _signature(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def _files(root: Path) -> dict[str, list[int]]:
    return {p.name: _signature(p) for p in sorted(root.iterdir()) if p.is_file() and p.name != RECEIPT}


def _encoded_header(group: list[tuple[str, dict]], metadata: dict) -> bytes:
    header, position = {"__metadata__": metadata}, 0
    for key, tensor in group:
        header[key] = {"dtype": tensor["dtype"], "shape": tensor["shape"],
                       "data_offsets": [position, position + tensor["bytes"]]}
        position += tensor["bytes"]
    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode()
    return raw + b" " * (-len(raw) % 8)


def validate_prepared(source: Path, destination: Path, *, verify_hashes: bool = True) -> bool:
    """Reject interrupted, changed, malformed or corrupted copies. Never mutate."""
    try:
        receipt = _json(destination / RECEIPT)
        if (receipt.get("schema") != SCHEMA or receipt.get("source") != str(source.resolve())
                or receipt.get("source_files") != _files(source)
                or receipt.get("files") != _files(destination)):
            return False
        limit = receipt.get("max_shard_bytes")
        if type(limit) is not int or limit <= 0 or any(p.stat().st_size > limit for p in destination.glob("*.safetensors")):
            return False
        tensors, _ = _inventory(destination)
        auxiliaries = {p.name for p in source.iterdir() if p.is_file() and p.name in _AUXILIARY}
        if set(receipt.get("auxiliary", {})) != auxiliaries:
            return False
        for name in auxiliaries:
            path = _child(destination, name)
            if verify_hashes:
                with path.open("rb") as stream:
                    if _copy_range(stream, 0, path.stat().st_size) != receipt["auxiliary"][name]:
                        return False
        expected = receipt["tensors"]
        if set(tensors) != set(expected):
            return False
        for key, info in tensors.items():
            if any(info[k] != expected[key][k] for k in ("shape", "dtype", "bytes")):
                return False
            if verify_hashes:
                with (destination / info["file"]).open("rb") as stream:
                    if _copy_range(stream, info["offset"], info["bytes"]) != expected[key]["sha256"]:
                        return False
        return True
    except (OSError, ValueError, TypeError, KeyError, struct.error):
        return False


def select_model(source: Path, label: str, *, verify_hashes: bool = True) -> tuple[Path, str]:
    destination = prepared_path(source, label)
    if validate_prepared(source, destination, verify_hashes=verify_hashes):
        return destination, "prepared"
    return source, "original"


def prepare_model(source: Path, destination: Path, *, max_shard_bytes: int = DEFAULT_SHARD_BYTES) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if type(max_shard_bytes) is not int or max_shard_bytes <= 0:
        raise ValueError("shard limit must be a positive byte count")
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("destination must be separate from the original model")
    if destination.exists():
        if validate_prepared(source, destination):
            if any(p.stat().st_size > max_shard_bytes for p in destination.glob("*.safetensors")):
                raise ValueError("existing prepared destination exceeds requested shard limit; choose another destination or raise the limit")
            return {"status": "already_prepared", "destination": str(destination)}
        raise ValueError("destination exists but is not verified; choose another destination")
    initial = _files(source)
    tensors, metadata = _inventory(source)
    if max((source / info["file"]).stat().st_size for info in tensors.values()) <= max_shard_bytes:
        return {"status": "not_needed", "destination": None}
    groups, group = [], []
    for item in sorted(tensors.items()):
        trial = [*group, item]
        size = 8 + len(_encoded_header(trial, metadata)) + sum(t["bytes"] for _, t in trial)
        if size > max_shard_bytes and group:
            groups.append(group)
            group = []
        group.append(item)
        file_bytes = 8 + len(_encoded_header(group, metadata)) + sum(t["bytes"] for _, t in group)
        if file_bytes > max_shard_bytes:
            minimum_mb = (file_bytes + 1024**2 - 1) // 1024**2
            raise ValueError(f"individual tensor exceeds shard limit; use --max-shard-mb {minimum_mb} or higher")
    if group:
        groups.append(group)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < sum(p.stat().st_size for p in source.iterdir() if p.is_file()) + 64 * 1024**2:
        raise ValueError("insufficient disk space for prepared model")
    # Unique task-owned staging makes interruption/retry and concurrent invocations
    # independent. A killed process's partial directory is never considered loadable.
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent) as staging:
        temp = Path(staging)
        auxiliary = {}
        for path in source.iterdir():
            if path.is_file() and path.name in _AUXILIARY:
                shutil.copy2(path, temp / path.name)
                with path.open("rb") as stream:
                    auxiliary[path.name] = _copy_range(stream, 0, path.stat().st_size)
        weight_map, verified = {}, {}
        for index, group in enumerate(groups, 1):
            name = f"model-{index:05d}-of-{len(groups):05d}.safetensors"
            header = _encoded_header(group, metadata)
            with (temp / name).open("wb") as output:
                output.write(struct.pack("<Q", len(header)))
                output.write(header)
                for key, info in group:
                    with (source / info["file"]).open("rb") as stream:
                        digest = _copy_range(stream, info["offset"], info["bytes"], output)
                    weight_map[key] = name
                    verified[key] = {k: info[k] for k in ("dtype", "shape", "bytes")}
                    verified[key]["sha256"] = digest
                output.flush()
                os.fsync(output.fileno())
        (temp / "model.safetensors.index.json").write_text(json.dumps({"metadata": {"total_size": sum(t["bytes"] for t in tensors.values())}, "weight_map": weight_map}, indent=2), encoding="utf-8")
        if initial != _files(source):
            raise ValueError("original model changed during preparation")
        receipt = {"schema": SCHEMA, "source": str(source), "source_files": initial,
                   "files": _files(temp), "max_shard_bytes": max_shard_bytes, "tensors": verified, "auxiliary": auxiliary}
        with (temp / RECEIPT).open("w", encoding="utf-8") as output:
            json.dump(receipt, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        if not validate_prepared(source, temp):
            raise ValueError("prepared tensor verification failed")
        # Rename the fully verified directory, not individual files into a live model.
        temp.rename(destination)
        # TemporaryDirectory tolerates its now absent task-owned staging path.
    return {"status": "prepared", "destination": str(destination), "shards": len(groups), "tensors": len(tensors)}


def preparation_status(source: Path, label: str) -> dict:
    path, origin = select_model(source, label, verify_hashes=False)
    largest = max((p.stat().st_size for p in path.glob("*.safetensors")), default=0)
    return {"model_origin": origin, "largest_shard_bytes": largest,
            "prepare_recommended": origin == "original" and largest > DEFAULT_SHARD_BYTES,
            "prepare_command": "openmd asr prepare-models" if origin == "original" and largest > DEFAULT_SHARD_BYTES else None}


def cmd_prepare_models(args) -> int:
    from .models import require_model
    source = require_model(args.model or os.getenv("OPENMD_QWEN3_MODEL") or "Qwen/Qwen3-ASR-1.7B", family="qwen3")
    destination = prepared_path(source, "qwen3-asr-1.7b")
    if args.prepared_root:
        destination = Path(args.prepared_root).expanduser().resolve() / destination.name
    result = prepare_model(source, destination, max_shard_bytes=args.max_shard_mb * 1024**2)
    print(json.dumps(result, ensure_ascii=True))
    return 0
