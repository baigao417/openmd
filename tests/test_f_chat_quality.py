"""Quality regressions use synthetic text only; no live browser or accounts."""
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from openmd.backends import loaded_chat
from openmd.core.completeness import check_completeness


URLS = {
    "chatgpt": "https://chatgpt.com/c/demo",
    "grok": "https://grok.com/c/demo",
    "deepseek": "https://chat.deepseek.com/a/chat/s/demo",
    "doubao": "https://www.doubao.com/chat/demo",
    "gemini": "https://gemini.google.com/app/demo",
    "qwen": "https://www.qianwen.com/chat/demo",
    "yuanbao": "https://yuanbao.tencent.com/chat/agent/demo",
}

FIXTURES = Path(__file__).parent / "fixtures" / "loaded_chat"


def answer_html(site, content):
    source = (FIXTURES / f"{site}.html").read_text(encoding="utf-8")
    return source.replace("Synthetic answer.", content, 1)


def extract(html, site="chatgpt", **options):
    item = loaded_chat.snapshot_item(Path("synthetic.html"), URLS[site])
    item.raw_meta.update(options)
    return check_completeness(item, loaded_chat.extract_loaded_chat(html, item, backend="page_snapshot.defuddle"))


@pytest.mark.parametrize("attribute", ['data-author="user"', 'data-user-message-bubble', 'data-chatgpt-search-unit-key="turn:user"'])
def test_chatgpt_user_layouts(attribute):
    result = extract(f'<main><div {attribute}>Synthetic question.</div><div data-message-author-role="assistant">Synthetic answer.</div></main>')
    assert [turn.role for turn in result.blocks] == ["user", "assistant"]
    assert result.metadata["independent_user_turn_count"] == 1
    assert result.status == "verified"


def test_independent_user_signal_catches_a_parser_miss(monkeypatch):
    original = loaded_chat._role_content
    monkeypatch.setattr(loaded_chat, "_role_content", lambda site, node: ("unknown", node) if node.has_attr("data-user-message-bubble") else original(site, node))
    result = extract('<main><div data-user-message-bubble>Synthetic question.</div><div data-message-author-role="assistant">Synthetic answer.</div></main>')
    assert result.status == "partial"
    assert result.metadata["completeness"]["status"] == "downgrade"
    assert any(check["reason"] == "conversation_user_turns_missing" for check in result.metadata["completeness"]["checks"])


def test_chatgpt_nested_hidden_user_is_not_double_counted():
    result = extract('<main><article data-testid="conversation-turn-1"><div data-author="user"><div data-user-message-bubble>Synthetic question.</div></div></article><div hidden data-user-message-bubble>Hidden</div><div data-message-author-role="assistant">Synthetic answer.</div></main>')
    assert len(result.blocks) == 2
    assert result.metadata["independent_user_turn_count"] == 1


@pytest.mark.parametrize("site", URLS)
def test_math_preserves_inline_and_display_tex_once(site):
    inline = '<span class="katex"><span class="katex-mathml"><math><semantics><mfrac><mi>a</mi><mi>b</mi></mfrac><annotation encoding="application/x-tex">\\frac{a}{b}</annotation></semantics></math></span><span class="katex-html" aria-hidden="true">ab</span></span>'
    block = '<span class="katex-display">' + inline.replace('\\frac{a}{b}', '\\frac{8 \\times 3600}{600}') + '</span>'
    result = extract(answer_html(site, '<p>Inline ' + inline + '.</p>' + block), site)
    assert '$\\frac{a}{b}$' in result.text
    assert '$$\n\\frac{8 \\times 3600}{600}\n$$' in result.text
    assert result.text.count('\\frac{a}{b}') == 1
    assert "ab" not in result.blocks[1].text


def test_math_without_tex_keeps_mathml_and_warns():
    result = extract('<div data-message-author-role="assistant"><math display="block"><mfrac><mn>8</mn><mn>600</mn></mfrac></math><span class="katex">visible equation</span></div>')
    assert '<mfrac>' in result.text and '<mn>600</mn>' in result.text
    assert 'visible equation' in result.text
    assert any('公式可能失真' in note for note in result.source_notes)
    assert result.metadata['math_fallback_count'] == 2


@pytest.mark.parametrize("site", URLS)
def test_embedded_large_images_are_counted_and_external_images_preserved(site):
    large = 'data:image/svg+xml;base64,' + 'A' * 9000
    html = '<figure><img src="' + large + '" alt="Synthetic chart"><figcaption>Synthetic caption</figcaption></figure><img src="data:image/png;base64,AAAA" width="16" height="16" alt="icon"><img src="https://example.org/chart.png" alt="External chart">'
    result = extract(answer_html(site, html), site)
    assert large not in result.text
    assert '[图片：内嵌 SVG，已省略]' in result.text
    assert 'Synthetic chart' in result.text and 'Synthetic caption' in result.text
    assert 'data:image/png' not in result.text
    assert 'https://example.org/chart.png' in result.text
    assert result.metadata['embedded_image_omitted_count'] == 1
    assert result.metadata['embedded_icon_removed_count'] == 1
    assert any('已省略内嵌图片：1' in note for note in result.source_notes)


def test_small_meaningful_embedded_image_is_preserved():
    result = extract('<div data-message-author-role="assistant"><img src="data:image/png;base64,AAAA" width="300" alt="Synthetic plot"></div>')
    assert 'data:image/png;base64,AAAA' in result.text


THINKING_MARKUP = {
    "chatgpt": '<div data-testid="reasoning-summary">Synthetic reasoning.</div>',
    "deepseek": '<div class="ds-markdown ds-markdown--think">Synthetic reasoning.</div>',
    "gemini": '<model-thoughts><div class="thoughts-content">Synthetic reasoning.</div></model-thoughts>',
    "doubao": '<div data-testid="thinking_content">Synthetic reasoning.</div>',
    "qwen": '<div data-chat-thinking-wrap>Synthetic reasoning.</div>',
    "yuanbao": '<div class="hyc-component-reasoner">Synthetic reasoning.</div>',
}


@pytest.mark.parametrize("site", THINKING_MARKUP)
def test_thinking_default_omits_and_option_restores_before_own_answer(site):
    soup = BeautifulSoup(answer_html(site, 'Synthetic final answer.'), 'html.parser')
    root = loaded_chat._outermost(list(soup.select(loaded_chat.SELECTORS[site])))[1]
    # Thinking can be a sibling of the selected final-answer markdown region.
    root.insert(0, BeautifulSoup(THINKING_MARKUP[site], 'html.parser'))
    default = extract(str(soup), site)
    assert 'Synthetic reasoning.' not in default.text
    assert 'Synthetic final answer.' in default.blocks[1].text
    assert default.metadata['thinking_omitted_count'] == 1
    assert any('已省略思考过程：1 段' in note for note in default.source_notes)
    included = extract(str(soup), site, include_thinking=True)
    assert included.blocks[1].text.index('#### 思考过程') < included.blocks[1].text.index('Synthetic reasoning.') < included.blocks[1].text.index('Synthetic final answer.')
    assert 'Synthetic reasoning.' not in included.blocks[3].text
    assert len(included.blocks) == len(default.blocks) == 4
    assert included.metadata['thinking_included_count'] == 1


def test_unknown_thinking_prose_and_user_example_are_never_guessed_away():
    result = extract('<div data-message-author-role="user"><div class="thinking-container">Synthetic user example.</div></div><div data-message-author-role="assistant"><p>Thought for 3 seconds. Synthetic ordinary answer.</p></div>')
    assert 'Synthetic user example.' in result.blocks[0].text
    assert 'Thought for 3 seconds.' in result.blocks[1].text
    assert result.metadata['thinking_omitted_count'] == 0


def test_cli_thinking_flag_and_snapshot_use_same_option(tmp_path, capsys):
    import json
    from openmd.cli import main
    page = tmp_path / 'page.html'
    page.write_text('<div data-message-author-role="user">Synthetic question.</div><div data-message-author-role="assistant"><div class="thinking-container">Synthetic reasoning.</div><p>Synthetic final answer.</p></div>', encoding='utf-8')
    command = ['go', str(page), '--as-snapshot', '--url', URLS['chatgpt'], '--include-thinking', '-o', str(tmp_path / 'out'), '--json']
    assert main(command) == 0
    receipt = json.loads(capsys.readouterr().out)
    saved = Path(receipt['saved_to']).read_text(encoding='utf-8')
    assert '#### 思考过程' in saved and 'Synthetic reasoning.' in saved


def test_snapshot_host_forwards_validated_thinking_option(tmp_path, monkeypatch):
    import subprocess
    from openmd import browser_host
    monkeypatch.setenv('OPENMD_DATA_DIR', str(tmp_path / 'data'))
    job = browser_host.start_snapshot_job('<p>Synthetic</p>', URLS['chatgpt'], save_to=str(tmp_path / 'out'), include_thinking=True, spawn=lambda *a, **k: None)
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, '{"ok":true,"saved_to":"synthetic.md"}', '')
    browser_host.run_job(Path(job['snapshot_path']).parent, runner=runner)
    assert '--include-thinking' in commands[0]
    with pytest.raises(ValueError, match='invalid_include_thinking'):
        browser_host.start_snapshot_job('<p>Synthetic</p>', URLS['chatgpt'], save_to=str(tmp_path / 'out'), include_thinking='false', spawn=lambda *a, **k: None)


def test_detail_explicit_thinking_field_obeys_same_option():
    from openmd.backends.ai_chat import parse_ai_chat_output
    payload = {'messages': [{'role': 'assistant', 'text': 'Synthetic final answer.', 'thinking': 'Synthetic reasoning.'}]}
    turns, meta = parse_ai_chat_output(payload, site='deepseek', source_url=URLS['deepseek'])
    assert turns[0].text == 'Synthetic final answer.'
    assert meta['thinking_omitted_count'] == 1
    turns, meta = parse_ai_chat_output(payload, site='deepseek', source_url=URLS['deepseek'], include_thinking=True)
    assert turns[0].text.index('#### 思考过程') < turns[0].text.index('Synthetic final answer.')
    assert meta['thinking_included_count'] == 1


@pytest.mark.parametrize(('site', 'page_title'), [
    ('chatgpt', 'Synthetic topic - ChatGPT'), ('grok', 'Synthetic topic | Grok'),
    ('deepseek', 'Synthetic topic - DeepSeek'), ('doubao', 'Synthetic topic - 豆包'),
    ('gemini', 'Synthetic topic - Google Gemini'), ('qwen', 'Synthetic topic - 千问'),
    ('yuanbao', 'Synthetic topic - 腾讯元宝'),
])
def test_snapshot_and_detail_strip_same_site_suffix(site, page_title):
    import json
    from openmd.backends.ai_chat import AI_CHAT_BACKENDS, OpenCliAiChatProvider
    from openmd.backends.base import BackendExtractRequest
    html = answer_html(site, 'Synthetic answer.').replace('Synthetic conversation', page_title)
    snapshot = extract(html, site)
    item = loaded_chat.snapshot_item(Path('synthetic.html'), URLS[site])
    payload = {'title': page_title, 'messages': [{'role': 'user', 'text': 'Synthetic question.'}, {'role': 'assistant', 'text': 'Synthetic answer.'}]}
    detail = OpenCliAiChatProvider(site, AI_CHAT_BACKENDS[site], runner=lambda *_: (0, json.dumps(payload), '')).extract(BackendExtractRequest(item=item, backend_chain=(AI_CHAT_BACKENDS[site],)))
    assert snapshot.title == detail.title == 'Synthetic topic'


@pytest.mark.parametrize('header', ['<header><h1>Synthetic session title</h1></header>', '<aside><a aria-current="page" href="/chat/demo">Synthetic session title</a></aside>', '<nav><a href="/chat/other">Other chat</a><a href="/chat/demo">Synthetic session title</a></nav>'])
def test_generic_site_title_prefers_current_dom_title(header):
    html = answer_html('qwen', 'Synthetic answer.').replace('Synthetic conversation', '千问-阿里 AI 助手').replace('<main>', header + '<main>')
    assert extract(html, 'qwen').title == 'Synthetic session title'


def test_generic_site_title_falls_back_to_first_user_message_and_preserves_real_title():
    question = '这是一个用于合成回归测试的问题标题文本需要保留三十个字然后截断'
    html = answer_html('qwen', 'Synthetic answer.').replace('Synthetic conversation', '千问-阿里 AI 助手').replace('Synthetic question.', question)
    assert extract(html, 'qwen').title == question[:30] + '…'
    html = html.replace('千问-阿里 AI 助手', 'Synthetic-hyphen topic')
    assert extract(html, 'qwen').title == 'Synthetic-hyphen topic'


def test_detail_generic_title_uses_conversation_title_then_first_user():
    from openmd.backends.ai_chat import parse_ai_chat_output
    payload = {'title': '千问-阿里 AI 助手', 'conversation_title': 'Synthetic session title', 'messages': [{'role': 'user', 'text': 'Synthetic question.'}]}
    assert parse_ai_chat_output(payload, site='qwen', source_url=URLS['qwen'])[1]['title'] == 'Synthetic session title'
    del payload['conversation_title']
    assert parse_ai_chat_output(payload, site='qwen', source_url=URLS['qwen'])[1]['title'] == 'Synthetic question.'


@pytest.mark.parametrize('role', ['User', 'Assistant', 'SYSTEM', ' Tool '])
def test_turnblock_normalizes_standard_roles_at_construction(role):
    from openmd.core.models import TurnBlock
    block = TurnBlock(role, 'Synthetic text.')
    assert block.role == role.strip().lower()
    assert block.as_dict()['role'] == role.strip().lower()


def test_detail_and_snapshot_render_identical_lowercase_role_headings():
    from openmd.backends.ai_chat import parse_ai_chat_output
    from openmd.core.renderer import render_conversation_blocks
    payload = {'messages': [{'Role': 'User', 'Text': 'Synthetic question.'}, {'Role': 'Assistant', 'Text': 'Synthetic answer.'}]}
    turns, _ = parse_ai_chat_output(payload, site='chatgpt', source_url=URLS['chatgpt'])
    snapshot = extract('<div data-message-author-role="user">Synthetic question.</div><div data-message-author-role="assistant">Synthetic answer.</div>')
    options = dict(source_url=URLS['chatgpt'], platform='chatgpt', exported_at='synthetic')
    assert [turn.role for turn in turns] == ['user', 'assistant']
    assert render_conversation_blocks(turns, **options) == render_conversation_blocks(snapshot.blocks, **options)


def test_legacy_dict_turns_render_standard_roles_consistently():
    from openmd.core.renderer import render_conversation_blocks
    text = render_conversation_blocks([{'type': 'turn', 'role': 'Assistant', 'text': 'Synthetic answer.'}], source_url=URLS['chatgpt'], platform='chatgpt', exported_at='synthetic')
    assert '### assistant\n' in text and '### Assistant' not in text


def test_chatgpt_testid_user_container_is_exported_not_only_counted():
    result = extract('<div data-testid="user-message">Synthetic question.</div><div data-message-author-role="assistant">Synthetic answer.</div>')
    assert [turn.role for turn in result.blocks] == ['user', 'assistant']
    assert result.status == 'verified'


def test_chatgpt_article_with_only_user_bubble_attribute():
    result = extract('<article data-testid="conversation-turn-1"><div data-user-message-bubble>Synthetic question.</div></article><article data-testid="conversation-turn-2"><div data-message-author-role="assistant">Synthetic answer.</div></article>')
    assert [turn.role for turn in result.blocks] == ['user', 'assistant']
    assert result.status == 'verified'


def test_code_text_is_preserved_when_its_dom_contains_math_markup():
    result = extract('<div data-message-author-role="assistant"><pre><code>literal <span class="katex"><math><annotation encoding="application/x-tex">x</annotation></math></span></code></pre></div>')
    assert '```\nliteral x\n```' in result.text
    assert 'OPENMDLOADEDLITERAL' not in result.text


def test_generic_title_skips_inactive_sidebar_conversation_title():
    html = answer_html('qwen', 'Synthetic answer.').replace('Synthetic conversation', '千问-阿里 AI 助手')
    html = html.replace('<main>', '<aside><a href="/chat/other"><span class="conversation-title">Other chat</span></a><a aria-current="page" href="/chat/demo"><span class="conversation-title">Synthetic current title</span></a></aside><main>')
    assert extract(html, 'qwen').title == 'Synthetic current title'


def test_generic_title_accepts_spacing_around_site_tagline():
    html = answer_html('qwen', 'Synthetic answer.').replace('Synthetic conversation', '千问 - 阿里 AI 助手')
    assert extract(html, 'qwen').title == 'Synthetic question.'


def test_gemini_thinking_sibling_stays_with_its_answer():
    html = '<user-query><div class="query-text">Synthetic question.</div></user-query><model-response><model-thoughts>Synthetic reasoning.</model-thoughts><div class="model-response-text">Synthetic final answer.</div></model-response>'
    result = extract(html, 'gemini', include_thinking=True)
    assert [turn.role for turn in result.blocks] == ['user', 'assistant']
    assert 'Synthetic reasoning.' in result.blocks[1].text
    assert result.metadata['thinking_included_count'] == 1


def test_thinking_option_is_visible_in_cli_help(capsys):
    from openmd.cli import main
    assert main(['go', '--help']) == 0
    assert '--include-thinking' in capsys.readouterr().out


def test_quality_notes_are_in_final_markdown_source_section(tmp_path):
    from openmd.core.renderer import render_document, render_markdown_document
    item = loaded_chat.snapshot_item(tmp_path / 'synthetic.html', URLS['chatgpt'])
    html = '<div data-message-author-role="assistant"><div class="thinking-container">Synthetic reasoning.</div><p>Synthetic answer.</p><math><mi>x</mi></math><img alt="Synthetic plot" src="data:image/png;base64,' + 'A' * 9000 + '"></div>'
    result = loaded_chat.extract_loaded_chat(html, item, backend='page_snapshot.defuddle')
    saved = render_document(render_markdown_document(item, result))
    notes = saved.split('来源说明：', 1)[1]
    assert '已省略思考过程：1 段' in notes
    assert '已省略内嵌图片：1 张' in notes
    assert '公式可能失真：1 处' in notes
    assert 'Synthetic reasoning.' not in saved


@pytest.mark.parametrize('include', [False, True])
def test_yuanbao_live_deepsearch_shape_keeps_final_answer_and_counts_thinking(include):
    html = (FIXTURES / 'yuanbao-deepsearch.html').read_text(encoding='utf-8')
    result = extract(html, 'yuanbao', include_thinking=include)
    assert [turn.role for turn in result.blocks] == ['user', 'assistant']
    answer = result.blocks[1].text
    assert 'Synthetic final answer.' in answer and 'Synthetic final list.' in answer
    if include:
        assert answer.index('#### 思考过程') < answer.index('Synthetic reasoning one.') < answer.index('Synthetic reasoning two.') < answer.index('#### 最终回答') < answer.index('Synthetic final answer.')
        assert result.metadata['thinking_included_count'] == 1
    else:
        assert 'Synthetic reasoning' not in answer and 'Synthetic thinking header.' not in answer
        assert result.metadata['thinking_omitted_count'] == 1
        assert any('已省略思考过程：1 段' in note for note in result.source_notes)


def test_chatgpt_live_assistant_only_dom_cannot_certify_question_answer_coverage():
    # Exact semantic structure from the authorized page; every word is synthetic.
    html = '<main><div data-turn-key="synthetic"><div data-content-search-turn-key="synthetic"><div data-chatgpt-search-unit-key="synthetic:assistant"><h4 class="sr-only" data-conversation-role="assistant">Assistant</h4><div data-response-body><div data-markdown-text-style="assistant-message"><div data-markdown-copy="contents"><p>Synthetic answer.</p></div></div></div></div></div></div></main>'
    result = extract(html)
    assert [turn.role for turn in result.blocks] == ['assistant']
    assert result.status == 'partial'
    assert result.error_code == 'ai_chat_user_turn_unavailable'
    assert result.metadata['completeness']['status'] == 'unknown'
    assert any('未发现可读取的用户轮' in note for note in result.source_notes)


def test_chatgpt_print_role_heading_supplies_independent_missing_user_evidence():
    html = '<div><h4 class="sr-only" data-conversation-role="user">You</h4><p>Synthetic unrecognized user bubble.</p></div><div data-chatgpt-search-unit-key="synthetic:assistant">Synthetic answer.</div>'
    result = extract(html)
    assert result.metadata['independent_user_turn_count'] == 1
    assert result.status == 'partial'
    assert result.metadata['completeness']['status'] == 'downgrade'
