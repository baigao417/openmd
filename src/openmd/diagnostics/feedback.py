"""Create a reviewable, sanitized problem package for maintainers."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from openmd.core.detector import detect_platform
from openmd.core.secrets import sanitize_error_summary

from .sanitize import sanitize_snapshot, sanitize_url


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("openmd-cli")
    except Exception:
        return "source-checkout"


def _doctor_summary() -> dict[str, Any]:
    try:
        from openmd.core.doctor import build_doctor_report
        report = build_doctor_report("core")
        return {
            "profile": report.get("profile"),
            "ready": report.get("ready"),
            "manifest_valid": report.get("manifest_valid"),
            "browser_host": {"ready": (report.get("browser_host") or {}).get("ready")},
            "route_verification": {"ready": (report.get("route_verification") or {}).get("ready")},
        }
    except Exception as exc:
        return {"status": "unavailable", "error_code": type(exc).__name__}


def _receipt(status: dict[str, Any], *, source_url: str) -> dict[str, Any]:
    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    return {
        "openmd_version": _version(),
        "status": result.get("status") or ("verified" if status.get("state") == "done" else "failed"),
        "source_level": result.get("source_level") or "browser_rendered",
        "completeness": result.get("completeness") or status.get("completeness") or "unknown",
        "error_code": result.get("error_code") or status.get("error_code"),
        "error_summary": sanitize_error_summary(result.get("error_summary") or status.get("error")),
        "next_action": result.get("next_action") or status.get("next_action") or "请维护者使用结构样本回归测试；提交前由用户确认脱敏内容。",
        "source_url": source_url,
        "source": source_url,
    }


def _receipt_only_manifest(status: dict[str, Any], *, source_url: str) -> dict[str, Any]:
    platform = detect_platform(source_url)
    return {
        "openmd_version": _version(),
        "page_structure": "not_captured",
        "reason": "link_route" if not status.get("snapshot") else "snapshot_not_captured",
        "route": "link_route" if not status.get("snapshot") else "snapshot_route",
        "platform": platform,
        "site": (source_url.split("/", 3)[2] if "/" in source_url else "").lower(),
        "source_url": sanitize_url(source_url),
    }


def _receipt_only(status: dict[str, Any], *, source_url: str, platform: str | None = None) -> dict[str, Any]:
    result = _receipt(status, source_url=source_url)
    result.update({
        "route": "link_route" if not status.get("snapshot") else "snapshot_route",
        "platform": platform or detect_platform(source_url),
    })
    return result


def build_feedback_package(
    source: str | Path,
    *,
    url: str | None = None,
    output: str | Path,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_path = Path(source)
    status = dict(status or {})
    if source_path.is_file():
        html = source_path.read_text(encoding="utf-8", errors="replace")
        source_url = url or "https://example.invalid/"
    else:
        if not status:
            from openmd.browser_host import read_status
            status = read_status(str(source))
        snapshot_path = Path(str(status.get("sanitized_snapshot_path") or status.get("snapshot_path") or ""))
        if not snapshot_path.is_file():
            source_url = url or str(status.get("url") or "")
            if not source_url:
                raise ValueError("feedback_url_required")
            out = Path(output)
            out.mkdir(parents=True, exist_ok=True)
            manifest = _receipt_only_manifest(status, source_url=source_url)
            receipt = _receipt_only(status, source_url=manifest["source_url"], platform=manifest["platform"])
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {"output": str(out), "files": ["manifest.json", "receipt.json"], "manifest": manifest, "receipt": receipt}
        html = snapshot_path.read_text(encoding="utf-8", errors="replace")
        source_url = url or str(status.get("url") or "")
    if not source_url:
        raise ValueError("feedback_url_required")
    sanitized = sanitize_snapshot(html, source_url)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    # Only these four files are written. The source snapshot is never copied.
    (out / "structure.html").write_text(sanitized.html, encoding="utf-8")
    manifest = {**sanitized.manifest, "openmd_version": _version()}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = _receipt(status, source_url=sanitized.manifest["source_url"])
    (out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    doctor = _doctor_summary()
    (out / "doctor.json").write_text(json.dumps(doctor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "README.txt").write_text(
        "这是 Openmd 脱敏问题包。请在附加到 issue 前检查 structure.html、manifest.json、receipt.json 和 doctor.json。\n"
        "包内不应包含原始页面、cookie、账号或正文。维护者可将 structure.html 转为回归 fixture。\n",
        encoding="utf-8",
    )
    return {"output": str(out), "files": [p.name for p in sorted(out.iterdir()) if p.is_file()], "manifest": manifest, "receipt": receipt}


def build_feedback_package_for_job(job_id: str, output: str | Path) -> dict[str, Any]:
    return build_feedback_package(job_id, output=output)
