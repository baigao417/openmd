from openmd.core.input_validation import valid_daily_input


def test_wechat_selector_is_valid_daily_input():
    assert valid_daily_input("wechat:Fixture?scope=chat")
    assert not valid_daily_input("wechat:")
