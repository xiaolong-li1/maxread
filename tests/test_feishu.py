import os
import tempfile
from pathlib import Path

from maxread.feishu import FeishuClient, _related_message_ids, _related_thread_ids, _safe_relative_path, doc_token_from_url, parse_event, progress_emoji_type


class CapturingFeishu(FeishuClient):
    def __init__(self):
        super().__init__(cli="lark-cli", identity="bot")
        self.args = []

    def _json(self, args):
        self.args = args
        return type("Result", (), {"data": {"ok": True}, "stdout": "{}"})()


class ThreadContextFeishu(FeishuClient):
    def __init__(self):
        super().__init__(cli="lark-cli", identity="bot")
        self.calls = []

    def _json(self, args):
        self.calls.append(args)
        if "+messages-mget" in args:
            return type("Result", (), {"data": {"messages": [{"message_id": "om_reply", "thread_id": "omt_topic", "content": '{"text":"@MaxRead"}'}]}, "stdout": "{}"})()
        if "+threads-messages-list" in args:
            return type("Result", (), {"data": {"items": [{"message_id": "om_root", "content": '{"text":"https://arxiv.org/abs/1706.03762"}'}]}, "stdout": "{}"})()
        return type("Result", (), {"data": {}, "stdout": "{}"})()


class ContextFeishu(FeishuClient):
    def __init__(self):
        super().__init__(cli="lark-cli", identity="bot")
        self.calls = []

    def _json(self, args):
        self.calls.append(args)
        if "+messages-mget" in args:
            return type("Result", (), {"data": {"messages": [{"content": '{"text":"原消息 https://arxiv.org/abs/2604.12946"}'}]}, "stdout": "{}"})()
        return type("Result", (), {"data": {"items": []}, "stdout": "{}"})()


def test_doc_token_from_url():
    assert doc_token_from_url("https://x.feishu.cn/docx/DKHQd7L2NoDWlRxXEE0cCTTdnEg") == "DKHQd7L2NoDWlRxXEE0cCTTdnEg"


def test_parse_event():
    event = parse_event(
        {
            "event_id": "evt_1",
            "message_id": "om_1",
            "chat_id": "oc_1",
            "chat_type": "p2p",
            "message_type": "text",
            "sender_id": "ou_1",
            "content": "https://arxiv.org/abs/2604.12946",
        }
    )
    assert event.event_id == "evt_1"
    assert event.message_id == "om_1"
    assert event.content.endswith("2604.12946")


def test_parse_event_marks_group_bot_mention_from_text():
    event = parse_event(
        {
            "event_id": "evt_2",
            "message_id": "om_2",
            "chat_id": "oc_2",
            "chat_type": "group",
            "message_type": "text",
            "sender_id": "ou_1",
            "content": '{"text":"@MaxRead https://arxiv.org/abs/2604.12946"}',
        }
    )
    assert event.mentioned_bot is True


def test_parse_event_rejects_group_without_bot_mention():
    event = parse_event(
        {
            "event_id": "evt_3",
            "message_id": "om_3",
            "chat_id": "oc_2",
            "chat_type": "group",
            "message_type": "text",
            "sender_id": "ou_1",
            "content": '{"text":"https://arxiv.org/abs/2604.12946"}',
        }
    )
    assert event.mentioned_bot is False


def test_parse_event_rejects_group_mentioning_someone_else():
    event = parse_event(
        {
            "event_id": "evt_4",
            "message_id": "om_4",
            "chat_id": "oc_2",
            "chat_type": "group",
            "message_type": "text",
            "sender_id": "ou_1",
            "content": '{"text":"@_user_1 https://arxiv.org/abs/2604.12946"}',
            "mentions": [{"name": "张三", "id": {"open_id": "ou_other"}}],
        }
    )
    assert event.mentioned_bot is False


def test_parse_event_accepts_opaque_mention_event():
    event = parse_event(
        {
            "event_id": "evt_5",
            "message_id": "om_5",
            "chat_id": "oc_2",
            "chat_type": "group",
            "message_type": "text",
            "sender_id": "ou_1",
            "content": '{"text":"@_user_1 https://arxiv.org/abs/2604.12946"}',
            "mentions": [{"key": "@_user_1"}],
        }
    )
    assert event.mentioned_bot is True


def test_related_message_ids_find_parent_and_skip_current():
    payload = {"message_id": "om_current", "event": {"message": {"parent_id": "om_parent", "thread_id": "omt_thread"}}}
    assert _related_message_ids(payload, exclude={"om_current"}) == ["om_parent"]
    assert _related_thread_ids(payload) == ["om_parent", "omt_thread"]


def test_fetch_related_message_text_reads_parent_message():
    event = parse_event(
        {
            "event_id": "evt_parent",
            "message_id": "om_reply",
            "chat_id": "oc_2",
            "chat_type": "group",
            "message_type": "text",
            "sender_id": "ou_1",
            "content": '{"text":"@MaxRead 看看这个"}',
            "parent_id": "om_parent",
        }
    )
    text = ContextFeishu().fetch_related_message_text(event)
    assert "2604.12946" in text


def test_fetch_related_message_text_gets_thread_from_current_message():
    event = parse_event(
        {
            "event_id": "evt_topic",
            "message_id": "om_reply",
            "chat_id": "oc_2",
            "chat_type": "group",
            "message_type": "text",
            "sender_id": "ou_1",
            "content": '{"text":"@MaxRead"}',
        }
    )
    client = ThreadContextFeishu()
    text = client.fetch_related_message_text(event)
    assert "1706.03762" in text
    assert any("+threads-messages-list" in call for call in client.calls)


def test_safe_relative_path_inside_cwd():
    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.chdir(root)
        try:
            image = root / "var" / "paper" / "figure.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"png")
            assert _safe_relative_path(str(image)) == "var/paper/figure.png"
        finally:
            os.chdir(old_cwd)


def test_reply_text_defaults_to_thread_reply():
    client = CapturingFeishu()
    client.reply_text("om_1", "[了解] 收到了", idempotency_key="k")
    assert "--reply-in-thread" in client.args
    assert client.args[client.args.index("--message-id") + 1] == "om_1"


def test_add_reaction_uses_im_reactions_create():
    client = CapturingFeishu()
    client.add_reaction("om_1", "Typing")
    assert client.args[:4] == ["lark-cli", "im", "reactions", "create"]
    assert client.args[client.args.index("--params") + 1] == '{"message_id": "om_1"}'
    assert client.args[client.args.index("--data") + 1] == '{"reaction_type": {"emoji_type": "Typing"}}'


def test_progress_emoji_mapping():
    assert progress_emoji_type("start") == "Get"
    assert progress_emoji_type("downloading") == "OnIt"
    assert progress_emoji_type("reading") == "StatusReading"
    assert progress_emoji_type("reviewing") == "THINKING"
    assert progress_emoji_type("writing") == "Typing"
    assert progress_emoji_type("done") == ""
