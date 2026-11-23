import json

from openmd.adapters.registry import create_default_registry
from openmd.backends.ai_chat import AiChatError, OpenCliAiChatProvider
from openmd.backends.base import BackendExtractRequest
from openmd.core.detector import detect_platform
from openmd.core.models import ContentItem
from openmd.core.planner import build_plan
from openmd.runtime import managed_tools


def test_ai_chat_plan_reports_unimplemented_site_and_provider():
    result = build_plan("https://claude.ai/chat/fixture", available_gates={"browser", "account"})
    assert result.error_code == "ai_chat_site_not_implemented"
    assert result.planned_provider == "opencli.claude.detail"
    assert result.runtime_provider is None
    assert result.binding_state == "unimplemented"
    assert "claude" in (result.next_action or "")


def test_ai_chat_selection_is_url_specific():
    registry = create_default_registry()
    item = ContentItem(platform="ai_chat", input_type="single", url="https://chat.deepseek.com/a/chat/s/fixture", source_type="conversation", raw_meta={"chat_site": "deepseek"})
    assert registry.select_backend_for_item(item).chain == ("opencli.deepseek.detail",)


def test_ai_chat_detail_login_page_is_login_required():
    from openmd.backends.ai_chat import parse_ai_chat_output

    try:
        parse_ai_chat_output({"url": "https://chatgpt.com/auth/login", "messages": []}, site="chatgpt", source_url="https://chatgpt.com/c/fixture")
    except AiChatError as exc:
        assert exc.code == "login_required"
    else:
        raise AssertionError("login page must not be treated as a conversation")


def test_chatgpt_retries_until_assistant_turn_is_present():
    payloads = [
        {"messages": [{"role": "User", "text": "question"}]},
        {"messages": [{"role": "User", "text": "question"}, {"role": "Assistant", "text": "answer"}]},
    ]
    calls = []

    def runner(command, timeout):
        calls.append(command)
        return 0, json.dumps(payloads[min(len(calls) - 1, 1)]), ""

    item = ContentItem(platform="ai_chat", input_type="single", url="https://chatgpt.com/c/fixture", source_type="conversation", raw_meta={"chat_site": "chatgpt"})
    result = OpenCliAiChatProvider("chatgpt", "opencli.chatgpt.detail", runner=runner).extract(BackendExtractRequest(item=item, backend_chain=("opencli.chatgpt.detail",)))
    assert result.status == "verified"
    assert result.metadata["assistant_turn_count"] == 1
    assert len(calls) == 2


def test_opencli_read_only_builtin_commands_are_allowed(monkeypatch):
    assert managed_tools.command_access(["doctor"]) == "read"
    assert managed_tools.command_access(["status"]) == "read"
    assert managed_tools.command_access(["daemon", "status"]) == "read"
    assert managed_tools.command_access(["douyin", "--help"]) == "read"
    assert managed_tools.command_access(["unknown-site", "--help"]) == "unknown"


def test_opencli_read_only_x_browser_bridge_methods_are_allowlisted():
    assert managed_tools.command_access(["browser", "chrome", "call", "tabs.query", "[{}]"]) == "read"
    assert managed_tools.command_access(["browser", "chrome", "call", "openmd.x.read", "[51,\"123\"]"]) == "read"
    assert managed_tools.command_access(["browser", "chrome", "call", "tabs.remove", "[51]"]) == "unknown"
    assert managed_tools.command_access(["browser", "chrome", "call", "cookies.getAll", "[]"]) == "unknown"
