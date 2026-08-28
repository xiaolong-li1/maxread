from unittest.mock import patch

from types import SimpleNamespace

from maxread.cli import _extract_event_supported_inputs, _handle_retry_event, _is_feedback_text, _is_retry_command, _record_feedback, _reply_retry_missing, _retry_related_message_ids, _retry_requires_rebuild, _should_accept_event, main
from maxread.db import Store


def test_extract_command_normalizes_papers_cool_arxiv_url(capsys):
    assert main(["extract", "https://papers.cool/arxiv/2608.25479"]) == 0

    assert capsys.readouterr().out.strip() == '["2608.25479"]'


def test_admin_starts_before_store_or_workdir_initialization(tmp_path):
    settings = SimpleNamespace(workdir=tmp_path)
    with patch("maxread.cli.Settings.load", return_value=settings), patch("maxread.cli.run_admin_server") as admin, patch(
        "maxread.cli.Store", side_effect=AssertionError("admin must not construct the store")
    ):
        assert main(["admin", "--host", "127.0.0.1", "--port", "8877"]) == 0

    admin.assert_called_once_with(settings, host="127.0.0.1", port=8877)


def test_private_event_is_accepted_without_mention():
    event = SimpleNamespace(chat_type="p2p", mentioned_bot=False)
    assert _should_accept_event(event) is True


def test_group_event_requires_bot_mention():
    assert _should_accept_event(SimpleNamespace(chat_type="group", mentioned_bot=False)) is False
    assert _should_accept_event(SimpleNamespace(chat_type="group", mentioned_bot=True)) is True


def test_group_topic_retry_is_accepted_without_rementioning_bot():
    event = SimpleNamespace(
        chat_type="group",
        mentioned_bot=False,
        content="重试",
        raw={"event": {"message": {"thread_id": "omt_topic"}}},
    )
    assert _should_accept_event(event) is True
    assert _is_retry_command("@读不动了 重试 2604.12946") is True
    assert _is_retry_command("为什么还要重试") is False


class _ContextFeishu:
    def fetch_related_message_text(self, event):
        return "原消息 https://arxiv.org/abs/2604.12946"


def test_group_mention_uses_parent_context_when_reply_has_no_link():
    event = SimpleNamespace(chat_type="group", mentioned_bot=True, content="@MaxRead 看看这个")
    refs, web_refs = _extract_event_supported_inputs(_ContextFeishu(), event)
    assert [ref.paper_id for ref in refs] == ["2604.12946"]
    assert web_refs == []


def test_topic_retry_without_mention_uses_thread_context():
    event = SimpleNamespace(
        chat_type="group",
        mentioned_bot=False,
        content="重试",
        raw={"event": {"message": {"thread_id": "omt_topic"}}},
    )
    refs, web_refs = _extract_event_supported_inputs(_ContextFeishu(), event)
    assert [ref.paper_id for ref in refs] == ["2604.12946"]
    assert web_refs == []


def test_private_retry_uses_related_context_without_explicit_thread_metadata():
    event = SimpleNamespace(chat_type="p2p", mentioned_bot=False, content="重试", raw={})
    refs, web_refs = _extract_event_supported_inputs(_ContextFeishu(), event)

    assert [ref.paper_id for ref in refs] == ["2604.12946"]
    assert web_refs == []


def test_retry_event_requeues_latest_failed_attempt_without_parsing_failure_url(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    original_usage = store.add_usage_event("evt-root", "om-root", "oc", "group", "ou", "paper", "2410.06205", "url", status="queued")
    original = store.enqueue_job("paper", "2410.06205", "url", "evt-root", "om-root", "oc", "group", "ou", original_usage)
    store.fail_queue_job(original["job_id"], "visual runner redirected to https://login.feishu.cn/accounts/trap")
    retry_usage = store.add_usage_event("evt-retry-1", "om-retry-1", "oc", "group", "ou", "paper", "2410.06205", "url", status="queued")
    latest = store.enqueue_job("paper", "2410.06205", "url", "evt-retry-1", "om-retry-1", "oc", "group", "ou", retry_usage)
    store.fail_queue_job(latest["job_id"], "format failed")

    class Feishu:
        text = ""
        def reply_text(self, _message_id, text, **_kwargs):
            self.text = text

    event = SimpleNamespace(
        event_id="evt-retry-2",
        message_id="om-retry-2",
        chat_id="oc",
        chat_type="group",
        sender_id="ou",
        content="重试",
        raw={"event": {"message": {"root_id": "om-root", "thread_id": "omt-topic"}}},
    )
    settings = SimpleNamespace(queue_workers=2)
    feishu = Feishu()

    assert _handle_retry_event(settings, store, feishu, event) is True
    rows = store.list_queue_jobs()
    assert next(row for row in rows if row["id"] == latest["job_id"])["status"] == "queued"
    assert next(row for row in rows if row["id"] == original["job_id"])["status"] == "failed"
    assert "收到 1 篇" in feishu.text
    assert "login.feishu.cn" not in feishu.text
    store.close()


def test_retry_mode_resumes_visual_infrastructure_failure_but_rebuilds_formula_failure():
    checkpoint = '{"doc_url":"https://tenant.feishu.cn/docx/doc"}'
    assert _retry_requires_rebuild(
        {
            "error": "visual-qa:remote-error: visual runner failed after 3 attempts: https://login.feishu.cn/accounts/trap",
            "checkpoint_json": checkpoint,
            "doc_url": "https://tenant.feishu.cn/docx/doc",
        }
    ) is False
    assert _retry_requires_rebuild(
        {
            "error": "quality:formula:xml:high:html-tag-in-formula",
            "checkpoint_json": checkpoint,
            "doc_url": "https://tenant.feishu.cn/docx/doc",
        }
    ) is True


def test_retry_event_resolves_root_id_from_thread_api_when_event_has_only_thread_id(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage = store.add_usage_event("evt-root", "om-root", "oc", "group", "ou", "paper", "2410.06205", "url", status="queued")
    queued = store.enqueue_job("paper", "2410.06205", "url", "evt-root", "om-root", "oc", "group", "ou", usage)
    store.fail_queue_job(queued["job_id"], "visual-qa:remote-error:browser timeout")
    bad_url = "https://login.feishu.cn/accounts/trap?app_id=2"
    bad_usage = store.add_usage_event("evt-old-retry", "om-old-retry", "oc", "group", "ou", "article", bad_url, bad_url, status="queued")
    bad = store.enqueue_job("article", bad_url, bad_url, "evt-old-retry", "om-old-retry", "oc", "group", "ou", bad_usage)
    store.fail_queue_job(bad["job_id"], "old parser artifact")

    class Feishu:
        text = ""
        def fetch_related_message_ids(self, _event):
            return ["om-root", "om-old-retry"]
        def reply_text(self, _message_id, text, **_kwargs):
            self.text = text

    event = SimpleNamespace(
        event_id="evt-retry",
        message_id="om-retry",
        chat_id="oc",
        chat_type="group",
        sender_id="ou",
        content="重试",
        raw={"event": {"message": {"thread_id": "omt-topic"}}},
    )
    feishu = Feishu()

    assert _handle_retry_event(SimpleNamespace(queue_workers=2), store, feishu, event) is True
    rows = store.list_queue_jobs()
    assert next(row for row in rows if row["id"] == queued["job_id"])["status"] == "queued"
    assert next(row for row in rows if row["id"] == bad["job_id"])["status"] == "failed"
    assert "2410.06205" in feishu.text
    assert "login.feishu.cn" not in feishu.text
    store.close()


def test_retry_related_message_ids_prefers_root_over_parent():
    payload = {
        "event": {
            "message": {
                "parent_id": "om_bot_failure",
                "root_id": "om_original_request",
            }
        }
    }

    assert _retry_related_message_ids(payload) == ("om_original_request",)


def test_retry_missing_message_has_no_stale_paper_id():
    class Feishu:
        def __init__(self):
            self.text = ""

        def reply_text(self, _message_id, text, **_kwargs):
            self.text = text

    event = SimpleNamespace(event_id="evt", message_id="om")
    feishu = Feishu()
    _reply_retry_missing(feishu, event)

    assert "2608.10416" not in feishu.text
    assert "论文 ID" in feishu.text


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
