"""Read-only model discovery and bounded, interruptible Ollama inference.

No server lifecycle or model download operations exist in this module.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import math
import queue
import socket
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from .breakers import breaker_reason, estimate_expected_chars
from .endpoint import validate_ollama_url


class OllamaError(RuntimeError):
    pass


def api_json(url: str, path: str, payload: dict | None = None, *, timeout: float = 3) -> dict:
    url = validate_ollama_url(url)
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json"})
    # Local inference must not be routed through an ambient proxy.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise OllamaError("ocr_ollama_redirect_rejected")

    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("error"):
        raise OllamaError("ollama_invalid_response")
    return result


def model_identity(tags: dict, name: str) -> dict:
    matches = [m for m in tags.get("models", []) if str(m.get("name", "")).removesuffix(":latest") == name.removesuffix(":latest")]
    if len(matches) != 1 or not isinstance(matches[0].get("digest"), str) or not matches[0]["digest"]:
        raise OllamaError("ollama_model_missing: " + name)
    return {"name": matches[0]["name"], "digest": matches[0]["digest"]}


def probe_ollama(config: dict) -> dict:
    report = {"url": config["ollama_url"], "reachable": False, "models": {}, "ready": False}
    try:
        tags = api_json(config["ollama_url"], "/api/tags")
        report["reachable"] = True
        report["version"] = api_json(config["ollama_url"], "/api/version").get("version")
        for key in ("ovis_model", "minicpm_model"):
            try:
                report["models"][key] = {**model_identity(tags, config[key]), "available": True}
            except OllamaError:
                report["models"][key] = {"name": config[key], "available": False, "digest": None}
        report["ready"] = all(m["available"] for m in report["models"].values())
        if not report["ready"]:
            report["error_code"] = "ollama_model_missing"
    except (OSError, ValueError, OllamaError) as exc:
        report["error_code"] = getattr(exc, "code", "ollama_not_reachable")
    return report


def running_models(payload: dict) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list):
        raise OllamaError("ollama_ps_invalid")
    names = []
    for model in models:
        if not isinstance(model, dict):
            raise OllamaError("ollama_ps_invalid")
        name = model.get("name", model.get("model"))
        if not isinstance(name, str) or not name.strip():
            raise OllamaError("ollama_ps_invalid")
        names.append(name)
    return names


def unload_model(config: dict, name: str) -> None:
    """Wait for unload before another GPU engine can start."""
    api_json(config["ollama_url"], "/api/generate", {"model": name, "keep_alive": 0, "stream": False}, timeout=15)
    deadline = time.monotonic() + 15
    while True:
        running = running_models(api_json(config["ollama_url"], "/api/ps"))
        if not any(m.removesuffix(":latest") == name.removesuffix(":latest") for m in running):
            return
        if time.monotonic() >= deadline:
            raise OllamaError("ollama_unload_unconfirmed")
        time.sleep(0.1)


def stream_recognize(image, *, model: str, prompt: str, config: dict, deadline: float) -> dict:
    """Read the stream on a disposable I/O thread; poll on the main thread.

    Shutdown the socket on timeout/Ctrl+C/breaker before issuing unload. This
    avoids Windows blocking socket reads masking Ctrl+C for a whole page budget.
    """
    config = {**config, "ollama_url": validate_ollama_url(config["ollama_url"])}
    from PIL import Image

    budget = estimate_expected_chars(image, config)
    prepared = image.copy()
    if prepared.width * prepared.height > config["max_pixels"]:
        factor = math.sqrt(config["max_pixels"] / (prepared.width * prepared.height))
        prepared = prepared.resize((max(1, int(prepared.width * factor)), max(1, int(prepared.height * factor))), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    prepared.save(buffer, format="PNG")
    payload = {"model": model, "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(buffer.getvalue()).decode("ascii")]}], "stream": True, "keep_alive": "10m", "think": False, "options": {"temperature": 0, "top_p": 0.001, "top_k": 1, "repeat_penalty": 1, "num_ctx": config["num_ctx"], "num_predict": config["num_predict"]}}
    address = urlsplit(config["ollama_url"])
    events: queue.Queue = queue.Queue()
    sockets: list = []
    canceled = threading.Event()

    def read():
        connection = None
        try:
            remaining = max(0.1, deadline - time.monotonic())
            cls = http.client.HTTPSConnection if address.scheme == "https" else http.client.HTTPConnection
            connection = cls(address.hostname, address.port, timeout=remaining)
            connection.connect()
            sockets.append(connection.sock)
            if canceled.is_set():
                return
            connection.request("POST", address.path.rstrip("/") + "/api/chat", body=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise OllamaError(f"ollama_http_{response.status}")
            while not canceled.is_set():
                line = response.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise OllamaError("ollama_inference_error")
                events.put(("chunk", chunk))
                if chunk.get("done"):
                    break
        except Exception as exc:
            events.put(("error", exc))
        finally:
            if connection is not None:
                connection.close()
            events.put(("end", None))

    start = time.monotonic()
    thread = threading.Thread(target=read, name="openmd-ollama-stream", daemon=True)
    thread.start()
    parts, final = [], {}
    reason = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = {"rule": "page_deadline"}
                break
            try:
                kind, value = events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if kind == "error":
                if time.monotonic() >= deadline or isinstance(value, TimeoutError):
                    reason = {"rule": "page_deadline"}
                    break
                raise OllamaError("ollama_stream_failed") from value
            if kind == "end":
                if not final.get("done"):
                    reason = {"rule": "stream_incomplete"}
                break
            content = value.get("message", {}).get("content", "")
            if not isinstance(content, str):
                raise OllamaError("ollama_invalid_content")
            parts.append(content)
            reason = breaker_reason("".join(parts), budget, config)
            if reason:
                break
            if value.get("done"):
                final = value
                if value.get("done_reason") == "length" or value.get("eval_count", 0) >= config["num_predict"]:
                    reason = {"rule": "token_limit", "num_predict": config["num_predict"]}
                break
    finally:
        canceled.set()
        for sock in sockets:
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        thread.join(timeout=2)
    if thread.is_alive():
        raise OllamaError("ollama_stream_cancel_unconfirmed")
    return {"text": "".join(parts), "seconds": round(time.monotonic() - start, 3), "breaker": reason, "tokens_generated": final.get("eval_count"), "input_size": list(prepared.size), "capacity": budget}
