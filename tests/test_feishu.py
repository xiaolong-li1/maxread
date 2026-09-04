import os
import tempfile
from pathlib import Path

from maxread.feishu import FeishuClient, _message_ids_from_payload, _related_message_ids, _related_thread_ids, _retry_attempts, _safe_relative_path, doc_token_from_url, normalize_doc_url, parse_event, progress_emoji_type


class CapturingFeishu(FeishuClient):
    def __init__(self):
        super().__init__(cli="lark-cli", identity="bot")
        self.args = []

    def _json(self, args):
        self.args = args
        return type("Result", (), {"data": {"ok": True}, "stdout": "{}"})()


class MarkdownCreateUrlFeishu(FeishuClient):
    def _json(self, args):
        return type(
            "Result",
            (),
            {
                "data": {
                    "data": {
                        "document": {
                            "url": "[Generated title](https://x.feishu.cn/docx/DocToken123?from=create)",
                            "document_id": "DocToken123",
                        }
                    }
                },
                "stdout": "{}",
            },
        )()


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


class ReactionFeishu(FeishuClient):
    def __init__(self):
        super().__init__(cli="lark-cli", identity="bot")
        self.calls = []

    def _json(self, args):
        self.calls.append(args)
        if "list" in args:
            return type(
                "Result",
                (),
                {
                    "data": {
                        "items": [
                            {
                                "reaction_id": "old_on_it",
                                "reaction_type": {"emoji_type": "OnIt"},
                                "operator": {"operator_type": "app", "operator_id": "cli_bot"},
                            },
                            {
                                "reaction_id": "other_user",
                                "reaction_type": {"emoji_type": "Typing"},
                                "operator": {"operator_type": "user", "operator_id": "ou_1"},
                            },
                        ]
                    },
                    "stdout": "{}",
                },
            )()
        return type("Result", (), {"data": {"ok": True}, "stdout": "{}"})()


class ContextFeishu(FeishuClient):
    def __init__(self):
        super().__init__(cli="lark-cli", identity="bot")
        self.calls = []

    def _json(self, args):
        self.calls.append(args)
        if "+messages-mget" in args:
            return type("Result", (), {"data": {"messages": [{"content": '{"text":"原消息 https://arxiv.org/abs/2604.12946"}'}]}, "stdout": "{}"})()
        return type("Result", (), {"data": {"items": []}, "stdout": "{}"})()


class MarkerFeishu(FeishuClient):
    def __init__(self, content):
        super().__init__(cli="lark-cli", identity="bot")
        self.content = content
        self.args = []

    def _json(self, args):
        self.args = args
        return type("Result", (), {"data": {"data": {"document": {"content": self.content}}}, "stdout": "{}"})()


class DelayedMarkerFeishu(FeishuClient):
    def __init__(self):
        super().__init__(cli="lark-cli", identity="bot")
        self.calls = []

    def _json(self, args):
        self.calls.append(args)
        content = "" if len(self.calls) == 1 else '<p id="target">[MaxReadFigure:3:img]</p>'
        return type("Result", (), {"data": {"data": {"document": {"content": content}}}, "stdout": "{}"})()


def test_doc_token_from_url():
    assert doc_token_from_url("https://x.feishu.cn/docx/DKHQd7L2NoDWlRxXEE0cCTTdnEg") == "DKHQd7L2NoDWlRxXEE0cCTTdnEg"


def test_normalize_doc_url_extracts_markdown_wrapped_link():
    wrapped = "[Generated title](https://x.feishu.cn/docx/DocToken123?from=create)"

    assert normalize_doc_url(wrapped) == "https://x.feishu.cn/docx/DocToken123?from=create"


def test_create_docx_returns_plain_url_when_cli_returns_markdown_link():
    created = MarkdownCreateUrlFeishu().create_docx("Generated title")

    assert created == {
        "url": "https://x.feishu.cn/docx/DocToken123?from=create",
        "token": "DocToken123",
    }


def test_fetch_docx_passes_format_scope_and_detail():
    feishu = CapturingFeishu()
    feishu.fetch_docx("https://x.feishu.cn/docx/doc123", doc_format="markdown", scope="full", detail="simple")
    assert feishu.args[:5] == ["lark-cli", "docs", "+fetch", "--api-version", "v2"]
    assert "--doc-format" in feishu.args
    assert feishu.args[feishu.args.index("--doc-format") + 1] == "markdown"
    assert "--scope" in feishu.args
    assert feishu.args[feishu.args.index("--scope") + 1] == "full"
    assert "--detail" in feishu.args
    assert feishu.args[feishu.args.index("--detail") + 1] == "simple"


def test_block_replace_uses_v2_xml_command():
    feishu = CapturingFeishu()
    feishu.block_replace("https://x.feishu.cn/docx/doc123", "block123", "<p>fixed</p>")

    assert feishu.args[:5] == ["lark-cli", "docs", "+update", "--api-version", "v2"]
    assert feishu.args[feishu.args.index("--command") + 1] == "block_replace"
    assert feishu.args[feishu.args.index("--block-id") + 1] == "block123"
    assert feishu.args[feishu.args.index("--doc-format") + 1] == "xml"
    assert feishu.args[feishu.args.index("--content") + 1] == "<p>fixed</p>"


def test_publish_docx_sets_anyone_editable_link_permission():
    feishu = CapturingFeishu()
    feishu.publish_docx("doc123")
    data = feishu.args[feishu.args.index("--data") + 1]

    assert '"link_share_entity": "anyone_editable"' in data
    assert '"security_entity": "anyone_can_edit"' in data
    assert '"comment_entity": "anyone_can_edit"' in data
    assert '"share_entity": "anyone"' in data
    assert "--yes" in feishu.args


def test_send_text_to_chat_uses_chat_id_and_idempotency_key():
    feishu = CapturingFeishu()
    feishu.send_text_to_chat("oc_duty", "today on call", "duty-key")
    assert feishu.args[:7] == [
        "lark-cli", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_duty"
    ]
    assert feishu.args[feishu.args.index("--text") + 1] == "today on call"
    assert feishu.args[feishu.args.index("--idempotency-key") + 1] == "duty-key"


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


def test_message_ids_from_thread_payload_excludes_current_and_dedupes():
    payload = {
        "data": {
            "items": [
                {"message_id": "om_root"},
                {"message_id": "om_current"},
                {"message_id": "om_root"},
            ]
        }
    }

    assert _message_ids_from_payload(payload, exclude={"om_current"}) == ["om_root"]


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


def test_safe_relative_path_uses_nested_symlink_alias_for_external_workdir():
    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        workdir = Path(tmp) / "external" / "workdir"
        alias = root / "var" / "maxread"
        image = workdir / "papers" / "2607.06838" / "rendered_figures" / "figure.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"png")
        alias.parent.mkdir(parents=True)
        try:
            alias.symlink_to(workdir, target_is_directory=True)
        except (NotImplementedError, OSError):
            return
        os.chdir(root)
        try:
            rel = _safe_relative_path(str(image))
            assert rel.startswith("var/feishu_uploads/figure-")
            cached = root / rel
            assert cached.exists()
            assert cached.resolve().is_relative_to(root.resolve())
            assert cached.read_bytes() == b"png"
        finally:
            os.chdir(old_cwd)


def test_safe_relative_path_never_falls_back_to_an_absolute_upload(tmp_path, monkeypatch):
    source = tmp_path / "external" / "figure.png"
    source.parent.mkdir()
    source.write_bytes(b"png")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    old_cwd = Path.cwd()
    os.chdir(cwd)
    monkeypatch.setattr("maxread.feishu.shutil.copyfile", lambda *_args: (_ for _ in ()).throw(OSError("disk error")))
    try:
        try:
            _safe_relative_path(str(source))
        except OSError as exc:
            assert "failed to stage upload" in str(exc)
        else:
            raise AssertionError("an unsafe absolute path must never reach lark-cli")
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


def test_set_progress_reaction_removes_previous_bot_progress_reaction():
    client = ReactionFeishu()
    client.set_progress_reaction("om_1", "writing")
    delete_calls = [call for call in client.calls if call[:4] == ["lark-cli", "im", "reactions", "delete"]]
    create_calls = [call for call in client.calls if call[:4] == ["lark-cli", "im", "reactions", "create"]]
    assert len(delete_calls) == 1
    assert delete_calls[0][delete_calls[0].index("--params") + 1] == '{"message_id": "om_1", "reaction_id": "old_on_it"}'
    assert len(create_calls) == 1
    assert create_calls[0][create_calls[0].index("--data") + 1] == '{"reaction_type": {"emoji_type": "Typing"}}'


def test_insert_image_passes_dimensions_and_caption_without_removed_selection_flag(tmp_path):
    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.chdir(root)
        try:
            image = root / "var" / "paper" / "figure.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"png")
            client = CapturingFeishu()
            client.insert_image("https://tenant.feishu.cn/docx/doc", str(image), caption="图", width=720, height=154)
        finally:
            os.chdir(old_cwd)

    assert client.args[:3] == ["lark-cli", "docs", "+media-insert"]
    assert client.args[client.args.index("--width") + 1] == "720"
    assert client.args[client.args.index("--height") + 1] == "154"
    assert client.args[client.args.index("--caption") + 1] == "图"
    assert "--selection-with-ellipsis" not in client.args


def test_find_text_block_id_uses_keyword_fetch_and_exact_text_match():
    client = MarkerFeishu('<fragment><p id="before">prefix [Marker]</p><p id="target">[Marker]</p></fragment>')

    block_id = client.find_text_block_id("https://tenant.feishu.cn/docx/doc", "[Marker]")

    assert block_id == "target"
    assert client.args[client.args.index("--scope") + 1] == "keyword"
    assert client.args[client.args.index("--keyword") + 1] == "[Marker]"
    assert client.args[client.args.index("--detail") + 1] == "with-ids"


def test_find_text_block_id_falls_back_to_full_fetch_when_keyword_scope_is_stale():
    client = DelayedMarkerFeishu()

    block_id = client.find_text_block_id("https://tenant.feishu.cn/docx/doc", "[MaxReadFigure:3:img]")

    assert block_id == "target"
    assert "--scope" in client.calls[0]
    assert "--scope" not in client.calls[1]


def test_move_and_delete_block_use_precise_update_commands():
    client = CapturingFeishu()

    client.move_block_after("https://tenant.feishu.cn/docx/doc", "anchor", "image")
    assert client.args[client.args.index("--command") + 1] == "block_move_after"
    assert client.args[client.args.index("--block-id") + 1] == "anchor"
    assert client.args[client.args.index("--src-block-ids") + 1] == "image"

    client.delete_block("https://tenant.feishu.cn/docx/doc", "image")
    assert client.args[client.args.index("--command") + 1] == "block_delete"
    assert client.args[client.args.index("--block-id") + 1] == "image"


def test_media_insert_is_not_retried_because_append_is_not_idempotent():
    assert _retry_attempts(["lark-cli", "docs", "+media-insert"]) == 1


def test_progress_emoji_mapping():
    assert progress_emoji_type("start") == "Get"
    assert progress_emoji_type("downloading") == "OnIt"
    assert progress_emoji_type("reading") == "StatusReading"
    assert progress_emoji_type("reviewing") == "THINKING"
    assert progress_emoji_type("writing") == "Typing"
    assert progress_emoji_type("done") == ""


def test_message_sender_name_uses_message_evidence_without_contact_scope():
    class SenderFeishu(FeishuClient):
        def __init__(self):
            super().__init__(cli="lark-cli", identity="bot")
            self.args = []

        def _json(self, args):
            self.args = args
            return type("Result", (), {"data": {"data": {"messages": [{
                "message_id": "om_source",
                "sender": {"id": "ou_sender", "name": "张三", "sender_type": "user"},
            }]}}, "stdout": "{}"})()

    client = SenderFeishu()

    assert client.message_sender_name("om_source", "ou_sender") == "张三"
    assert "+messages-mget" in client.args
    assert "--no-reactions" in client.args
    assert client.message_sender_name("web-message:local", "ou_sender") == ""
