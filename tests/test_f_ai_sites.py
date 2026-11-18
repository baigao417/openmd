import json
import subprocess

import pytest

from openmd.backends.ai_chat import OpenCliAiChatProvider, AiChatError, build_opencli_detail_args, parse_ai_chat_output
from openmd.core.models import ContentItem
from openmd.backends.base import BackendExtractRequest

SITES={"doubao":"https://www.doubao.com/chat/123456", "gemini":"https://gemini.google.com/u/1/app/0123456789abcdef", "qwen":"https://www.qianwen.com/chat/0123456789abcdef0123456789abcdef", "yuanbao":"https://yuanbao.tencent.com/chat/naQivTmsDa/12345678-1234-1234-1234-123456789abc"}


@pytest.mark.parametrize("site,url",SITES.items())
def test_new_sites_have_real_provider_and_url_specific_plan(site,url):
    from openmd.backends.registry import BackendRuntimeOptions,create_runtime_backend_provider_registry
    from openmd.core.planner import build_plan
    provider=create_runtime_backend_provider_registry("ai_chat",BackendRuntimeOptions(enable_browser_backends=True)).provider_for(f"opencli.{site}.detail")
    assert isinstance(provider,OpenCliAiChatProvider)
    plan=build_plan(url,available_gates={"browser","account"})
    assert plan.planned_provider==plan.runtime_provider==f"opencli.{site}.detail"
    assert plan.binding_state=="implemented"
    assert plan.implementation_state=="experimental"


@pytest.mark.parametrize("site",["grok","deepseek","chatgpt",*SITES])
def test_every_detail_uses_scoped_background_loading(monkeypatch,site):
    from openmd.backends import ai_chat
    calls=[]
    monkeypatch.setattr(ai_chat,"run_interruptibly",lambda cmd,**kw: calls.append((cmd,kw)) or subprocess.CompletedProcess(cmd,0,"[]",""))
    ai_chat._run_command(build_opencli_detail_args(site,"synthetic"),180)
    assert "--import=data:" in calls[0][1]["env"]["NODE_OPTIONS"]


def test_only_markdown_capable_sites_receive_markdown_flag():
    for site in ["grok","qwen","chatgpt"]:
        assert "--markdown" in build_opencli_detail_args(site,"synthetic")
    for site in ["doubao","gemini","deepseek","yuanbao"]:
        assert "--markdown" not in build_opencli_detail_args(site,"synthetic")
    assert "--wait" in build_opencli_detail_args("chatgpt","synthetic")


def test_empty_placeholder_is_not_a_conversation():
    with pytest.raises(AiChatError):
        parse_ai_chat_output([{"Role":"System","Text":"No messages found. Verify the conversation ID."}],site="doubao",source_url=SITES["doubao"])
    # Legitimate source prose mentioning sign-in is never scanned as auth.
    turns,_=parse_ai_chat_output([{"Role":"Assistant","Text":"Discuss login and sign in."}],site="doubao",source_url=SITES["doubao"])
    assert turns[0].text=="Discuss login and sign in."


@pytest.mark.parametrize("rows,expected", [
    ([{"Login":"No","Status":"Page not ready","Url":"https://chatgpt.com/auth/login"}],False),
    ([{"Login":"No (guest mode)","Status":"Connected"}],False),
    ([{"Login":"Yes","Status":"Connected"}],True),
    ([{"Login":"No","Status":"Page not ready"}],None),
])
def test_structured_status_distinguishes_login_from_page_not_ready(rows,expected):
    from openmd.backends.ai_chat import login_status_from_output
    assert login_status_from_output(rows) is expected
