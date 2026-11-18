from dataclasses import replace

import pytest

from openmd.core.completeness import check_completeness
from openmd.core.models import ContentItem, TranscriptResult, TurnBlock


@pytest.mark.parametrize("expected,status", [(3,"partial"),(2,"verified"),(None,"verified")])
def test_independent_turn_count_only_downgrades(expected,status):
    item=ContentItem("ai_chat","single","https://grok.com/c/fixture",source_type="conversation")
    result=TranscriptResult("body","verified","browser_rendered","fixture",blocks=[TurnBlock("user","fixture"),TurnBlock("assistant","answer")])
    signals={} if expected is None else {"conversation_turn_count":expected}
    checked=check_completeness(item,result,signals=signals)
    assert checked.status==status
    assert checked.metadata["completeness"]["status"] == ("unknown" if expected is None else "downgrade" if expected==3 else "ok")
    assert check_completeness(item,replace(result,status="partial"),signals={"conversation_turn_count":2}).status=="partial"


def test_provider_preserves_independent_page_signal_and_generating_rows():
    from openmd.backends.ai_chat import parse_ai_chat_output, AiChatError
    rows=[{"Role":"User","Text":"fixture","OpenmdPageTurnCount":4},{"Role":"Assistant","Text":"answer","OpenmdPageTurnCount":4}]
    blocks, meta=parse_ai_chat_output(rows,site="grok",source_url="https://grok.com/c/fixture")
    assert meta["independent_turn_count"] == 4
    with pytest.raises(AiChatError,match="生成"):
        parse_ai_chat_output([{"Role":"assistant","Text":"streaming fixture","Generating":True}],site="chatgpt",source_url="https://chatgpt.com/c/fixture")
