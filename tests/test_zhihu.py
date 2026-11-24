from openmd.core.detector import detect_platform


def test_zhihu_shared_links_are_unwrapped_and_classified():
    from openmd.adapters.zhihu import normalize_zhihu_url

    cases = {
        "https://www.zhihu.com/question/123?utm_source=share": "https://www.zhihu.com/question/123",
        "https://link.zhihu.com/?target=https%3A%2F%2Fzhuanlan.zhihu.com%2Fp%2F456%3Fshare%3D1": "https://zhuanlan.zhihu.com/p/456",
    }
    for source, expected in cases.items():
        assert detect_platform(source) == "zhihu"
        assert normalize_zhihu_url(source) == expected


def test_zhihu_detector_rejects_untrusted_share_redirect():
    from openmd.adapters.zhihu import normalize_zhihu_url

    assert normalize_zhihu_url("https://link.zhihu.com/?target=https%3A%2F%2Fevil.example%2F") is None


def test_zhihu_question_route_and_browser_fallback_are_declared():
    from openmd.routing.loader import default_backend_chains

    chain = default_backend_chains()["zhihu"]
    assert chain[0] == "opencli.zhihu.read"
    assert "browser.kaze.snapshot" in chain


def test_zhihu_does_not_patch_or_overlay_managed_opencli_adapter():
    from openmd.runtime.opencli_patches import PATCHES

    assert not [p for p in PATCHES if p.target.startswith("clis/zhihu/")]


def test_zhihu_renderer_preserves_math_images_code_and_answer_boundaries():
    from openmd.backends.zhihu import html_to_markdown

    rendered = html_to_markdown('<p>rate <img src="https://zhihu.com/equation/eeimg" alt="x^2"/></p><pre><code class="language-python">print(1)</code></pre><p><img src="https://img.example/a.png" alt="diagram"/></p>')
    assert "$x^2$" in rendered
    assert "```python" in rendered and "print(1)" in rendered
    assert "![diagram](https://img.example/a.png)" in rendered


def test_zhihu_opencli_runner_refuses_all_write_commands(monkeypatch):
    from openmd.backends.zhihu import _run_zhihu

    assert _run_zhihu(["opencli", "zhihu", "like", "answer:1:2"])[0] == 2


def test_zhihu_question_payload_keeps_full_answers_and_metadata():
    from openmd.backends.zhihu import ZhihuOpenCliProvider

    payload = [{"rank": 1, "id": "42", "question_title": "A question", "question_detail": "<p>Question details</p>", "declared_total": 1, "author": "Author", "votes": 9,
                "url": "https://www.zhihu.com/question/1/answer/42", "content": "<p>Full body</p>"}]
    text, title, author, note = ZhihuOpenCliProvider._read_payload("question", payload, "https://www.zhihu.com/question/1", None)
    assert title == "A question" and "Question details" in text and "Full body" in text and "Author" in text and "9" in text


def test_zhihu_pin_and_collection_expansion_use_read_only_download(tmp_path):
    from openmd.backends.zhihu import ZhihuOpenCliProvider

    args = ZhihuOpenCliProvider._args("pin", ["pin", "123"], "https://www.zhihu.com/pin/123", tmp_path)
    assert args is None  # OpenCLI has no pin detail reader; executor may use browser snapshot fallback.


def test_zhihu_question_is_partial_without_declared_total_or_when_truncated():
    from openmd.backends.zhihu import _question_coverage_partial

    assert _question_coverage_partial(20, None)
    assert _question_coverage_partial(20, 21)
    assert not _question_coverage_partial(20, 20)
    assert _question_coverage_partial(1000, 1000)
