from types import SimpleNamespace

from maxread.cli import _extract_event_supported_inputs, _is_feedback_text, _record_feedback, _should_accept_event
from maxread.db import Store


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


def test_feedback_text_accepts_explicit_prefix_or_feedback_intent():
    assert _is_feedback_text("hello") is False
    assert _is_feedback_text("则呢hello") is False
    assert _is_feedback_text("反馈：图片太少") is True
    assert _is_feedback_text("建议 支持 PDF URL") is True
    assert _is_feedback_text("这个生成的有问题，没有图，我要反馈") is True
    assert _is_feedback_text("我想反馈一下公式渲染") is True
    assert _is_feedback_text('{"text":"问题：公式坏了"}') is True


def test_record_feedback_persists_ai_decision_and_replies(tmp_path):
    class Classifier:
        def responses_text(self, system, user, **kwargs):
            return '{"is_feedback":true,"category":"quality","confidence":0.93}'

    class Feishu:
        def __init__(self):
            self.replies = []

        def reply_text(self, *args, **kwargs):
            self.replies.append((args, kwargs))

    event = SimpleNamespace(
        event_id="evt",
        message_id="om",
        chat_id="oc",
        chat_type="p2p",
        sender_id="ou",
        content="这篇方法框架图不见了",
    )
    store = Store(tmp_path / "maxread.sqlite3")
    feishu = Feishu()

    assert _record_feedback(store, feishu, event, Classifier()) is True
    row = store.list_feedback()[0]
    assert row["feedback_source"] == "ai"
    assert row["feedback_category"] == "quality"
    assert len(feishu.replies) == 1
    store.close()


def test_record_feedback_ignores_greeting_without_ai_call(tmp_path):
    class Classifier:
        def responses_text(self, system, user, **kwargs):
            raise AssertionError("plain greeting should not call the classifier")

    event = SimpleNamespace(content="hello")
    store = Store(tmp_path / "maxread.sqlite3")

    assert _record_feedback(store, SimpleNamespace(), event, Classifier()) is False
    assert store.feedback_count() == 0
    store.close()
