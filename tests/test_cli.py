from types import SimpleNamespace

from maxread.cli import _extract_event_supported_inputs, _is_feedback_text, _should_accept_event


def test_private_event_is_accepted_without_mention():
    event = SimpleNamespace(chat_type="p2p", mentioned_bot=False)
    assert _should_accept_event(event) is True


def test_group_event_requires_bot_mention():
    assert _should_accept_event(SimpleNamespace(chat_type="group", mentioned_bot=False)) is False
    assert _should_accept_event(SimpleNamespace(chat_type="group", mentioned_bot=True)) is True


class _ContextFeishu:
    def fetch_related_message_text(self, event):
        return "原消息 https://arxiv.org/abs/2604.12946"


def test_group_mention_uses_parent_context_when_reply_has_no_link():
    event = SimpleNamespace(chat_type="group", mentioned_bot=True, content="@MaxRead 看看这个")
    refs, web_refs = _extract_event_supported_inputs(_ContextFeishu(), event)
    assert [ref.paper_id for ref in refs] == ["2604.12946"]
    assert web_refs == []


def test_group_mention_without_topic_link_returns_empty_refs():
    class EmptyContextFeishu:
        def fetch_related_message_text(self, event):
            return "@MaxRead"

    event = SimpleNamespace(chat_type="group", mentioned_bot=True, content="@MaxRead")
    refs, web_refs = _extract_event_supported_inputs(EmptyContextFeishu(), event)
    assert refs == []
    assert web_refs == []


def test_feedback_text_requires_explicit_feedback_prefix():
    assert _is_feedback_text("hello") is False
    assert _is_feedback_text("则呢hello") is False
    assert _is_feedback_text("反馈：图片太少") is True
    assert _is_feedback_text("建议 支持 PDF URL") is True
    assert _is_feedback_text('{"text":"问题：公式坏了"}') is True
