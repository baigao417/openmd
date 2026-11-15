"""Discourse engine reader: public JSON, then an owned same-origin browser lease."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlparse

from openmd.core.content_validation import validate_extraction_content
from openmd.core.models import TranscriptResult
from openmd.core.opencli_output import load_opencli_json


DISCOURSE_BACKEND = "web_article.discourse"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class TopicTarget:
    origin: str
    prefix: str
    topic_id: int
    post_number: int

    @property
    def json_path(self):
        floor = f"/{self.post_number}" if self.post_number != 1 else ""
        return f"{self.prefix}/t/{self.topic_id}{floor}.json?include_raw=true"


def topic_target(url: str) -> TopicTarget | None:
    parsed = urlparse(url)
    match = re.fullmatch(r"(.*?)/t/(?:[^/]+/)?(\d+)(?:/(\d+))?/?", parsed.path)
    # Greedy optional slug can swallow numeric IDs in /t/id/floor.
    parts = parsed.path.strip("/").split("/")
    if "t" not in parts:
        return None
    index = parts.index("t")
    tail = parts[index + 1:]
    if tail and tail[0].isdigit():
        numbers = tail
    elif len(tail) >= 2 and tail[1].isdigit():
        numbers = tail[1:]
    else:
        return None
    if not match or len(numbers) not in {1, 2} or any(not value.isdigit() for value in numbers):
        return None
    return TopicTarget(f"{parsed.scheme}://{parsed.netloc}", "/" + "/".join(parts[:index]) if index else "", int(numbers[0]), int(numbers[1]) if len(numbers) == 2 else 1)


def discourse_html_evidence(body: str) -> bool:
    parser = _EngineMarkers()
    parser.feed(body)
    return parser.generator or (parser.application and parser.setup)


class _EngineMarkers(HTMLParser):
    generator = False
    application = False
    setup = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name", "").lower() == "generator" and "discourse" in attrs.get("content", "").lower():
            self.generator = True
        if attrs.get("id") == "discourse-application":
            self.application = True
        if "data-discourse-setup" in attrs or attrs.get("id") == "data-preloaded":
            self.setup = True


def _public_fetch(url: str) -> tuple[int, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/json,text/html;q=0.5", "User-Agent": "Openmd/0.0.1"})
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("discourse_response_oversized")
        body = data.decode("utf-8", errors="replace")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return response.code, body


class BrowserJsonLease:
    """Read JSON in the page's origin; release only this named session."""

    def __init__(self, url: str, *, runner=None):
        self.url = url
        if runner is None:
            # Loaded lazily: web_article registers this provider and already
            # owns the resolved executable / process-lifetime wrapper.
            from openmd.backends.web_article import _run_command

            def runner(command, *, timeout):
                return _run_command(command, timeout)
        self.runner = runner
        self.session = "openmd-discourse-" + uuid.uuid4().hex
        self.opened = False
        self.engine = False

    def read(self, path: str) -> tuple[int, object]:
        if not self.opened:
            self.opened = True
            code, _stdout, _stderr = self.runner(["opencli", "browser", "open", self.url, "--session", self.session, "--window", "background"], timeout=90)
            if code:
                raise ValueError("discourse_browser_open_failed")
        origin = f"{urlparse(self.url).scheme}://{urlparse(self.url).netloc}"
        marker_script = """(() => {
          const generator = document.querySelector('meta[name="generator"]')?.content || '';
          const engine = /discourse/i.test(generator) || Boolean(document.querySelector('#discourse-application') && document.querySelector('[data-discourse-setup], #data-preloaded'));
          return {engine};
        })()"""
        # Keep engine evidence independently of the larger JSON read. A fetch
        # exception must not let later generic readers accept a known UI shell.
        marker_data = self._eval(" ".join(marker_script.splitlines()))
        self.engine = self.engine or marker_data.get("engine") is True
        script = """(async () => {
          if (location.origin !== ORIGIN) return {status: 403, payload: null};
          try {
            const response = await fetch(PATH, {credentials: 'same-origin', headers: {'Accept':'application/json'}});
            const body = await response.text();
            if (body.length > 16000000) return {status: 413, payload: null};
            let payload = null; try { payload = JSON.parse(body); } catch {}
            return {status: response.status, payload};
          } catch { return {status: 0, payload: null}; }
        })()""".replace("ORIGIN", json.dumps(origin)).replace("PATH", json.dumps(path))
        data = self._eval(" ".join(script.splitlines()))
        self.engine = self.engine or data.get("engine") is True
        return int(data.get("status") or 0), data.get("payload")

    def _eval(self, script):
        code, stdout, _stderr = self.runner(["opencli", "browser", "eval", script, "--session", self.session, "--window", "background"], timeout=90)
        if code:
            raise ValueError("discourse_browser_read_failed")
        data = load_opencli_json(stdout)
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("discourse_browser_invalid_result")
        return data

    def close(self):
        if self.opened:
            try:
                self.runner(["opencli", "browser", "close", "--session", self.session, "--window", "background"], timeout=30)
            except Exception:
                pass


class DiscourseProvider:
    name = DISCOURSE_BACKEND

    def __init__(self, options, *, fetcher=None, browser_factory=None, scope: str = "target_post", chunk_size: int = 20):
        self.options = options
        self.fetcher = fetcher or _public_fetch
        self.browser_factory = browser_factory or BrowserJsonLease
        self.scope = scope
        self.chunk_size = max(1, min(50, chunk_size))

    def extract(self, request):
        target = topic_target(request.item.url)
        if target is None:
            return self._failed("discourse_not_applicable", "The URL has no topic selector.", engine=False)
        browser = self.browser_factory(request.item.url) if self.options.enable_browser_backends else None
        detected = request.item.raw_meta.get("site_engine") == "discourse"
        branch = "public_json"
        request_count = 0
        chunk_errors = []
        def read(path):
            nonlocal detected, branch, request_count
            request_count += 1
            status, payload = 0, None
            if self.options.enable_network_backends:
                try:
                    status, payload = self.fetcher(target.origin + path)
                    if isinstance(payload, str):
                        detected = detected or discourse_html_evidence(payload)
                except Exception:
                    pass
            if request_count == 1 and not detected and not _posts_payload(payload) and self.options.enable_network_backends:
                try:
                    _html_status, page = self.fetcher(request.item.url)
                    detected = isinstance(page, str) and discourse_html_evidence(page)
                except Exception:
                    pass
            if status == 429:
                return status, payload
            if not _posts_payload(payload) and browser is not None:
                status, payload = browser.read(path)
                detected = detected or browser.engine
                branch = "browser_same_origin"
            if _posts_payload(payload):
                detected = True
            return status, payload
        try:
            status, payload = read(target.json_path)
            if not detected and self.options.enable_network_backends:
                try:
                    _html_status, page = self.fetcher(request.item.url)
                    detected = isinstance(page, str) and discourse_html_evidence(page)
                except Exception:
                    pass
            if not detected:
                return self._failed("discourse_not_detected", "No Discourse engine evidence was found.", engine=False)
            if not _posts_payload(payload):
                return self._failed(_access_code(status), "The detected Discourse engine did not return accessible post JSON.")
            if payload.get("id") is not None and int(payload["id"]) != target.topic_id:
                return self._failed("discourse_topic_id_mismatch", "The topic JSON does not match the requested topic ID.")
            posts = {int(post["id"]): post for post in payload["post_stream"]["posts"] if isinstance(post, dict) and str(post.get("id", "")).isdigit()}
            initial_count = len(posts)
            all_posts = request.item.raw_meta.get("discourse_scope", self.scope) == "all_posts"
            stream = [int(value) for value in payload["post_stream"].get("stream", []) if str(value).isdigit()]
            if all_posts:
                if not stream:
                    return self._failed("discourse_stream_missing", "All-post extraction requires the topic stream ID list.")
                if len(stream) > 10000:
                    return self._failed("discourse_topic_scope_limit", "The explicit all-post scope exceeds this reader's 10000-post bound.")
                remaining = [value for value in dict.fromkeys(stream) if value not in posts]
                for offset in range(0, len(remaining), self.chunk_size):
                    if offset:
                        time.sleep(0.5)
                    chunk = remaining[offset:offset + self.chunk_size]
                    path = f"{target.prefix}/t/{target.topic_id}/posts.json?" + urlencode([("post_ids[]", value) for value in chunk]) + "&include_raw=true"
                    chunk_status, batch = read(path)
                    if not _posts_payload(batch):
                        chunk_errors.append(_access_code(chunk_status))
                        continue
                    for post in batch["post_stream"]["posts"]:
                        if isinstance(post, dict) and str(post.get("id", "")).isdigit() and int(post["id"]) in stream:
                            posts[int(post["id"])] = post
                posts = {key: post for key, post in posts.items() if key in stream}
            selected = sorted(posts.values(), key=lambda post: (int(post.get("post_number") or 0), int(post["id"])))
            if not all_posts:
                selected = [post for post in selected if post.get("post_number") == target.post_number]
            if not selected:
                return self._failed("discourse_target_post_missing", "The requested post number was not present in the accessible topic JSON.")
            main = next((post for post in selected if post.get("post_number") == (1 if all_posts else target.post_number)), None)
            if main is None:
                return self._failed("discourse_target_post_missing", "The requested main/target post is not visible in the selected topic scope.")
            bodies = [(post, _post_body(post, target.origin + target.prefix + "/")) for post in selected]
            missing = list(dict.fromkeys([value for value in stream if value not in posts] + [int(post["id"]) for post, body in bodies if not body.strip()])) if all_posts else []
            if not all_posts and not bodies[0][1].strip():
                return self._failed("discourse_post_body_missing", "The requested post body is empty.")
            if all_posts and not _post_body(main, target.origin + target.prefix + "/").strip():
                return self._failed("discourse_post_body_missing", "The main post body is empty.")
            text = "\n\n".join((f"## #{post['post_number']} {post.get('username', '')}\n\n" if all_posts else "") + body for post, body in bodies if body.strip())
            metadata = {"engine": "discourse", "topic_id": target.topic_id, "post_id": main["id"], "target_post_id": main["id"], "post_body_present": bool(text.strip()), "scope": "all_posts" if all_posts else "main_post" if target.post_number == 1 else "target_post", "coverage": {"expected_count": len(set(stream)) if all_posts else 1, "fetched_count": sum(bool(body.strip()) for _, body in bodies), "missing_count": len(missing), "initial_count": initial_count, "request_count": request_count, "missing_post_ids": missing, "complete": not missing}, "strategy": branch}
            return validate_extraction_content(TranscriptResult(text=text.rstrip() + "\n", status="partial" if missing else "verified", source_level="native_text", backend=self.name, title=str(payload.get("title") or request.item.title or target.topic_id), author=main.get("username"), published_at=main.get("created_at"), metadata=metadata, error_code=(chunk_errors[-1] if chunk_errors else "discourse_posts_incomplete") if missing else None, error_summary=f"{len(missing)} visible-stream post IDs could not be retrieved with a nonempty body." if missing else None, source_notes=["Discourse structured post bodies only; raw Markdown preferred, navigation and suggested topics excluded.", "Coverage is limited to posts visible to the current public/session reader."]), request.item)
        except Exception:
            return self._failed("discourse_read_failed", "The bounded Discourse read could not complete.", engine=detected or bool(browser and browser.engine))
        finally:
            if browser is not None:
                browser.close()

    def _failed(self, code, summary, *, engine=True):
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=self.name, error_code=code, error_summary=summary, metadata={"engine": "discourse", "failure_class": "user_resolvable" if code in {"discourse_auth_required", "discourse_permission_denied"} else "runtime_transient"} if engine else {})


def _posts_payload(payload):
    return isinstance(payload, dict) and isinstance(payload.get("post_stream"), dict) and isinstance(payload["post_stream"].get("posts"), list)


def _access_code(status):
    return {401: "discourse_auth_required", 403: "discourse_permission_denied", 429: "discourse_rate_limited", 404: "discourse_topic_unavailable"}.get(status, "discourse_post_json_unavailable")


def _post_body(post, base_url):
    if isinstance(post.get("raw"), str) and post["raw"].strip():
        return _absolute_markdown_links(post["raw"], base_url)
    cooked = post.get("cooked")
    if not isinstance(cooked, str) or not cooked.strip():
        return ""
    parser = _CookedMarkdown(base_url)
    parser.feed(cooked)
    return parser.markdown()


def _absolute_markdown_links(text, base_url):
    def absolute(value):
        if value.startswith("upload://"):
            return urljoin(base_url, "uploads/short-url/" + value.removeprefix("upload://"))
        return urljoin(base_url, value)
    output = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker[1]
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            output.append(line)
            continue
        if fence is not None or line.startswith(("    ", "\t")):
            output.append(line)
            continue
        # Inline code examples also remain literal source text.
        pieces = re.split(r"(`+[^`]*`+)", line)
        for index in range(0, len(pieces), 2):
            pieces[index] = re.sub(r"(!?\[[^\]\n]*\]\()([^\s)]+)([^)]*\))", lambda match: match[1] + absolute(match[2]) + match[3], pieces[index])
        output.append("".join(pieces))
    return "".join(output)


class _CookedMarkdown(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.parts = []
        self.stack = []
        self.hidden = 0
        self.pre = False
        self.code_fence = "```"
        self.rows = []
        self.row = None
        self.cell = None
        self.quotes = []

    def emit(self, value):
        if self.cell is not None:
            self.cell.append(value)
        else:
            self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.stack.append((tag, attrs))
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "table":
            self.rows = []
        elif tag == "tr":
            self.row = []
        elif tag in {"th", "td"}:
            self.cell = []
        elif tag == "pre":
            self.pre = True
            self.emit("\n\n```\n")
        elif tag == "code":
            if self.pre:
                language = next((value.removeprefix("language-") for value in attrs.get("class", "").split() if value.startswith("language-")), "")
                if language and self.parts and self.parts[-1] == "\n\n```\n":
                    self.parts[-1] = "\n\n```" + language + "\n"
            else:
                self.emit("`")
        elif tag == "a":
            self.emit("[")
        elif tag == "img":
            self.emit(f"![{attrs.get('alt', '')}]({urljoin(self.base, attrs.get('src', ''))})")
        elif tag in {"strong", "b"}:
            self.emit("**")
        elif tag in {"em", "i"}:
            self.emit("*")
        elif tag == "br":
            self.emit("\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.emit("\n- ")
        elif tag in {"p", "div", "blockquote", "aside"}:
            self.emit("\n\n")
            if tag in {"blockquote", "aside"}:
                self.quotes.append(len(self.parts))

    def handle_endtag(self, tag):
        attrs = {}
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                attrs = self.stack.pop(index)[1]
                break
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append("".join(self.cell).strip().replace("|", "\\|").replace("\n", "<br>"))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.rows:
            width = max(len(row) for row in self.rows)
            rows = [row + [""] * (width - len(row)) for row in self.rows]
            rows.insert(1, ["---"] * width)
            self.emit("\n\n" + "\n".join("| " + " | ".join(row) + " |" for row in rows) + "\n\n")
        elif tag == "pre":
            self.emit("\n```\n\n")
            self.pre = False
        elif tag == "code" and not self.pre:
            self.emit("`")
        elif tag == "a":
            self.emit("](" + urljoin(self.base, attrs.get("href", "")) + ")")
        elif tag in {"strong", "b"}:
            self.emit("**")
        elif tag in {"em", "i"}:
            self.emit("*")
        elif tag in {"blockquote", "aside"} and self.quotes:
            start = self.quotes.pop()
            quote = "".join(self.parts[start:]).strip()
            self.parts[start:] = ["\n".join("> " + line for line in quote.splitlines()) + "\n\n"]
        elif tag in {"p", "div"} or re.fullmatch(r"h[1-6]", tag):
            self.emit("\n\n")

    def handle_data(self, data):
        if not self.hidden:
            self.emit(data if self.pre else re.sub(r"\s+", " ", data))

    def markdown(self):
        return "".join(self.parts).strip()
