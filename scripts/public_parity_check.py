"""Build and consume a clean wheel against fixed public and generated local samples.

The receipt stores status and counts only; extracted Markdown and source URLs stay
inside the isolated runtime/output tree and are never copied into the report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import venv
import zipfile


PUBLIC_SAMPLES = {
    "web": "https://docs.python.org/3/tutorial/classes.html",
    "youtube": "https://www.youtube.com/watch?v=5YIT-byVUOY",
    "bilibili": "https://www.bilibili.com/video/BV1KLGE6FEoJ",
    "weixin": "https://mp.weixin.qq.com/s/jql2L3TiI-8kVeRy3BRy_Q",
    "x": "https://x.com/servasyy_ai/status/2118217737034145917",
}


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, check=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _local_samples(root: Path) -> dict[str, Path]:
    local = root / "local-inputs"
    local.mkdir(parents=True, exist_ok=True)
    docx = local / "public-sample.docx"
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>Openmd public parity sample</w:t></w:r></w:p>
<w:p><w:r><w:t>This synthetic local sample contains no user content.</w:t></w:r></w:p>
<w:sectPr/></w:body></w:document>"""
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"""
    package_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>"""
    with zipfile.ZipFile(docx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("word/document.xml", document_xml)
    image = local / "public-sample.png"
    from PIL import Image, ImageDraw
    canvas = Image.new("RGB", (640, 220), "white")
    ImageDraw.Draw(canvas).text((24, 80), "Openmd synthetic OCR sample", fill="black")
    canvas.save(image)
    pdf = local / "public-sample.pdf"
    # Minimal one-page PDF with a literal text operator; no extra PDF writer needed.
    body = b"BT /F1 18 Tf 72 760 Td (Openmd synthetic PDF sample) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(payload))
        payload += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(payload)
    payload += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        payload += f"{offset:010d} 00000 n \n".encode()
    payload += f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    pdf.write_bytes(payload)
    return {"pdf": pdf, "docx": docx, "image": image}


def run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    output = Path(args.output).resolve()
    runtime = output / "runtime"
    stage = output / "staging"
    wheelhouse = output / "wheelhouse"
    for path in (runtime, stage, wheelhouse):
        path.mkdir(parents=True, exist_ok=True)
    py = Path(args.python or sys.executable).resolve()
    build = _run([str(py), "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), str(repo)], cwd=stage, env=os.environ.copy())
    if build.returncode:
        print("wheel_build_failed", file=sys.stderr)
        return build.returncode
    wheels = sorted(wheelhouse.glob("openmd_cli-*.whl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not wheels:
        print("wheel_missing", file=sys.stderr)
        return 1
    wheel = wheels[0]
    digest = _sha256(wheel)
    venv_root = runtime / "verify-venv"
    venv.EnvBuilder(with_pip=True).create(venv_root)
    scripts = venv_root / ("Scripts" if os.name == "nt" else "bin")
    cli = scripts / ("openmd.exe" if os.name == "nt" else "openmd")
    pyvenv = scripts / ("python.exe" if os.name == "nt" else "python")
    install_env = os.environ.copy()
    install_env.update(ROUND10_GROUP="H", OPENMD_RUN_DIR=str(output), PIP_DISABLE_PIP_VERSION_CHECK="1")
    index = os.environ.get("OPENMD_PYPI_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple")
    install = _run([str(pyvenv), "-m", "pip", "install", "--index-url", index, f"{wheel}[all]"], cwd=stage, env=install_env, timeout=5400)
    if install.returncode:
        print("wheel_install_failed", file=sys.stderr)
        return install.returncode
    config = runtime / "empty-config.json"
    config.write_text('{"schema":"openmd.local/v1"}\n', encoding="utf-8")
    user = runtime / "user"
    data = runtime / "data"
    hf = runtime / "hf"
    modelscope = runtime / "modelscope"
    for path in (user, data, hf, modelscope):
        path.mkdir(parents=True, exist_ok=True)
    systemroot = os.environ.get("SystemRoot", r"C:\Windows")
    temp = runtime / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.pathsep.join((str(scripts), str(Path(systemroot) / "System32"), systemroot)),
        "SystemRoot": systemroot, "WINDIR": systemroot,
        "TEMP": str(temp), "TMP": str(temp),
        "COMSPEC": os.environ.get("COMSPEC", str(Path(systemroot) / "System32" / "cmd.exe")),
        "USERPROFILE": str(user), "HOME": str(user), "OPENMD_DATA_DIR": str(data),
        "OPENMD_CONFIG": str(config), "HF_HOME": str(hf), "MODELSCOPE_CACHE": str(modelscope),
        "ROUND10_GROUP": "H", "OPENMD_RUN_DIR": str(output), "PYTHONNOUSERSITE": "1",
        "PIP_INDEX_URL": index, "OPENMD_NPM_REGISTRY": os.environ.get("OPENMD_NPM_REGISTRY", "https://registry.npmmirror.com"),
        "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"),
    }
    doctor = _run([str(cli), "doctor", "--profile", "core", "--json"], cwd=stage, env=env, timeout=300)
    try:
        card = json.loads(doctor.stdout)
    except (ValueError, IndexError):
        card = {"ok": False, "error_code": "doctor_invalid_json"}
    card_path = output / "capability-card.json"
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    local = _local_samples(runtime)
    samples = [(key, url) for key, url in PUBLIC_SAMPLES.items()] + [(key, str(path)) for key, path in local.items()]
    results = []
    for name, source in samples:
        destination = runtime / "outputs" / name
        destination.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        completed = _run([str(cli), source, "--save-to", str(destination), "--yes", "--enable-network-backends", "--enable-browser-backends", "--json"], cwd=stage, env=env, timeout=600)
        markdown = list(destination.rglob("*.md"))
        results.append({"sample": name, "exit_code": completed.returncode, "markdown_files": len(markdown), "elapsed_seconds": round(time.monotonic() - started, 2)})
    receipt = {
        "schema": "openmd.public-parity/v1", "ok": doctor.returncode == 0 and all(row["exit_code"] == 0 and row["markdown_files"] > 0 for row in results),
        "wheel": wheel.name, "wheel_sha256": digest, "python": platform.python_version(), "venv": str(venv_root),
        "doctor_ok": doctor.returncode == 0, "capability_card": str(card_path), "samples": results,
        "private_content_in_report": False,
    }
    receipt_path = output / "public-parity-receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in receipt.items() if k not in {"samples"}}, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", help="Python used only to build and create the isolated venv")
    parser.add_argument("--output", required=True, help="task-owned output directory outside the checkout")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
