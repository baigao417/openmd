"""Regression protection for browser error screens across backend providers."""

import pytest

from openmd.backends.executor import BackendExecutor
from openmd.core.content_validation import browser_error_page_reason, validate_extraction_content
from openmd.core.models import ContentItem, ExtractionComponent, TranscriptResult
from openmd.core.secrets import sanitize_runlog_entry, sanitize_url


@pytest.mark.parametrize('body', [
    "This site can't provide a secure connection\nERR_SSL_PROTOCOL_ERROR",
    "<h1>This site can’t be reached</h1><p>ERR_CONNECTION_RESET</p>",
    "您的连接不是私密连接\nERR_CERT_AUTHORITY_INVALID",
    "ERR_NAME_NOT_RESOLVED",
    "example.com sent an invalid response.\nERR_SSL_PROTOCOL_ERROR\nReload",
])
def test_browser_error_screen_is_failed_and_has_no_usable_components(body):
    result = validate_extraction_content(TranscriptResult(
        text=body, status='verified', source_level='native_text', backend='fixture',
        components=[ExtractionComponent(id='content', kind='text', status='verified', source_level='native_text', backend='fixture')],
    ))
    assert result.status == 'failed'
    assert result.error_code == 'browser_error_page'
    assert not result.has_usable_text
    assert all(component.status != 'verified' for component in result.normalized_components)


def test_technical_article_can_mention_browser_errors():
    body = "# TLS troubleshooting\n\nERR_SSL_PROTOCOL_ERROR\n\nThis diagnostic indicates a failed TLS handshake."
    assert browser_error_page_reason(body) is None
    assert browser_error_page_reason("The browser says: This site can't provide a secure connection.") is None
    assert browser_error_page_reason("# TLS troubleshooting\n\nERR_SSL_PROTOCOL_ERROR\n\nAfter fixing your certificate chain, reload the page.") is None


@pytest.mark.parametrize(("platform", "body", "error_code"), [
    ("weixin", "# 微信公众平台\n\n> 原文链接: https://mp.weixin.qq.com/s/missing\n\n---\n\n参数错误\n\n： ，视频 小程序 赞 分享 收藏", "weixin_error_page"),
    ("web_article", "# 你似乎来到了没有知识存在的荒原\n\n4 秒后自动跳转至首页\n\n![page error](https://example.test/error.svg)\n\n登录后查看内容", "browser_error_page"),
    ("web_article", "# NOWSECURE\n\n## by nodriver\n\n## NOWSECURE\n\n### by nodriver", "browser_challenge_page"),
    ("web_article", "# Not Found\n\n## Page Not Found\n\n来自 iframe", "browser_error_page"),
    ("web_article", "# Example Domain\n\nThis domain is for use in documentation examples without needing permission. This is not a service.", "browser_error_page"),
    ("zhihu", "无法访问当前页面", "browser_error_page"),
])
def test_real_bench_status_screens_are_rejected(platform, body, error_code):
    item = ContentItem(platform=platform, input_type="single", source_type="article", url="https://example.test/status")
    rejected = validate_extraction_content(TranscriptResult(
        text=body, status="verified", source_level="third_party_extractor", backend="fixture",
        title=("nowsecure.nl" if "NOWSECURE" in body else "Example Domain" if "Example Domain" in body else "Not Found" if "Page Not Found" in body else None),
    ), item)
    assert rejected.status == "failed"
    assert rejected.error_code == error_code
    assert not rejected.has_usable_text


def test_technical_article_can_use_a_browser_error_as_its_heading():
    body = "# Secure connection failed\n\n" + "A TLS handshake has several steps. " * 150
    assert browser_error_page_reason(body, "Secure connection failed") is None
    assert browser_error_page_reason("# Secure connection failed\n\nA TLS guide explains certificate chains.") is None
    assert browser_error_page_reason("# Secure connection failed\n\nThis guide explains how to install a trusted certificate. Reload your browser after updating the trust store.") is None


def test_zhihu_article_can_discuss_unavailable_pages_without_being_rejected():
    item = ContentItem(platform="zhihu", input_type="single", source_type="article", url="https://zhuanlan.zhihu.com/p/123")
    body = "# 页面加载问题\n\n遇到无法访问的情况时，可以先检查网络连接和目标链接是否有效。"
    result = validate_extraction_content(TranscriptResult(
        text=body, status="verified", source_level="third_party_extractor", backend="fixture",
    ), item)
    assert result.status == "verified"


def test_long_runlog_url_with_parenthesis_is_sanitized_without_losing_identity():
    url = "https://example.com/" + "a" * 300 + ")b?token=secret&resource_id=123"
    saved = sanitize_runlog_entry({"input_url": url})["input_url"]
    assert "secret" not in saved
    assert len(saved) > 240
    assert saved == sanitize_url(url)


def test_generic_executor_rejects_error_screen_and_tries_fallback():
    class Provider:
        def __init__(self, text):
            self.text = text

        def extract(self, request):
            return TranscriptResult(text=self.text, status='verified', source_level='native_text', backend=request.backend_chain[0])

    executor = BackendExecutor(providers={
        'browser': Provider("This site can't be reached\nERR_CONNECTION_RESET"),
        'fallback': Provider('Actual article body'),
    })
    item = ContentItem(platform='web_article', input_type='single', url='https://example.com/article')
    failed = executor.extract(item, ('browser',))
    assert failed.status == 'failed' and failed.error_code == 'browser_error_page'
    result = executor.extract(item, ('browser', 'fallback'))
    assert result.backend == 'fallback' and result.text == 'Actual article body'
    assert 'fallback_attempt: browser failed with browser_error_page' in result.source_notes
