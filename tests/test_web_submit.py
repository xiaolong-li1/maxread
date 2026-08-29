from types import SimpleNamespace

from maxread.cli import _handle_web_binding_event
from maxread.db import Store
from maxread.web_submit import (
    WEB_SUBMIT_HTML,
    claim_binding_code,
    issue_binding_code,
    new_web_identity,
    retry_web_job,
    submit_web_papers,
)
from maxread.web_pet import WebPetAgent, persist_pet_exchange, progress_payload


def test_web_identity_defaults_to_guest_and_is_stable(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")

    token, first = new_web_identity(store)
    same_token, second = new_web_identity(store, token)

    assert same_token == token
    assert first["public_id"] == second["public_id"]
    assert first["account_type"] == "guest"
    assert store.web_identity_sender(first).startswith("guest:web_")
    assert store.get_user_names([store.web_identity_sender(first)])[store.web_identity_sender(first)] == "游客"
    store.close()


def test_web_submit_queues_paper_without_feishu_message(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3)

    result = submit_web_papers(settings, store, identity, "https://arxiv.org/abs/2608.25927")

    assert result["ok"] is True
    assert result["items"][0]["status"] == "queued"
    rows = store.list_web_submissions(identity["public_id"])
    assert len(rows) == 1
    assert rows[0]["chat_type"] == "web"
    assert rows[0]["sender_id"].startswith("guest:")
    assert rows[0]["job_status"] == "queued"
    watcher = store.get_job_watchers(result["items"][0]["job_id"])[0]
    assert watcher["chat_type"] == "web"
    assert watcher["message_id"].startswith("web-message:")
    messages = store.list_web_messages(identity)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["kind"] == "queue_ack"
    store.close()


def test_web_submit_returns_existing_document_as_cache(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    store.upsert_paper(
        "2608.25927",
        "done",
        title="Code World Model",
        doc_url="https://tenant.feishu.cn/docx/doc",
    )

    result = submit_web_papers(SimpleNamespace(queue_workers=3), store, identity, "2608.25927")

    assert result["items"][0]["cached"] is True
    assert result["items"][0]["doc_url"].endswith("/doc")
    assert store.list_queue_jobs() == []
    store.close()


def test_admin_overlay_submission_keeps_actor_audit(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    identity["_actor_type"] = "admin"
    identity["_actor_id"] = "admin"

    submit_web_papers(SimpleNamespace(queue_workers=3), store, identity, "2608.25927")

    messages = store.list_web_messages(identity)
    assert messages[0]["role"] == "user"
    assert messages[0]["actor_type"] == "admin"
    assert messages[0]["actor_id"] == "admin"
    store.close()


def test_progress_and_pet_agent_are_scoped_to_current_identity(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token_a, identity_a = new_web_identity(store)
    _token_b, identity_b = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")
    submit_web_papers(settings, store, identity_a, "2608.25927")
    submit_web_papers(settings, store, identity_b, "2608.27456")

    progress = progress_payload(settings, store, identity_a)
    answer, _ = WebPetAgent(settings, store, identity_a).reply("任务到哪了？")

    assert [item["source_id"] for item in progress["recent"]] == ["2608.25927"]
    assert progress["active"]["percent"] == 5
    assert "2608.25927" in answer
    assert "2608.27456" not in answer
    store.close()


def test_pet_exchange_is_persisted_in_main_conversation(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")

    result = persist_pet_exchange(settings, store, identity, "任务到哪了？")

    assert result["ok"] is True
    messages = store.list_web_messages(identity)
    assert [item["kind"] for item in messages] == ["pet_user", "pet_reply"]
    assert messages[-1]["channel"] == "pet"
    store.close()


def test_retry_button_api_resumes_published_visual_failure(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3)
    queued = submit_web_papers(settings, store, identity, "2608.25927")["items"][0]
    store.conn.execute(
        "update queue_jobs set status='failed', workflow_state='quality_failed', stage='failed', "
        "doc_url='https://tenant/doc', checkpoint_json='{}', error='visual-qa:remote-error: Feishu PDF export failed' where id=?",
        (queued["job_id"],),
    )
    store.conn.commit()

    result = retry_web_job(settings, store, identity, queued["job_id"])

    assert result["ok"] is True
    assert result["resume_published"] is True
    job = next(item for item in store.list_queue_jobs() if item["id"] == queued["job_id"])
    assert job["status"] == "queued"
    assert job["rebuild_pipeline"] == 0
    assert any(item["kind"] == "retry_request" for item in store.list_web_messages(identity))
    store.close()


def test_binding_code_migrates_guest_web_usage_to_feishu_identity(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    sender = store.web_identity_sender(identity)
    store.add_usage_event(
        "evt", "web-message:1", f"web:{identity['public_id']}", "web", sender,
        "paper", "2608.25927", "https://arxiv.org/abs/2608.25927", status="queued",
    )
    binding = issue_binding_code(store, identity)

    bound = claim_binding_code(store, binding["code"], "ou_feishu_user")

    assert bound["account_type"] == "feishu"
    assert bound["feishu_open_id"] == "ou_feishu_user"
    assert store.list_web_submissions(identity["public_id"])[0]["sender_id"] == "ou_feishu_user"
    assert claim_binding_code(store, binding["code"], "ou_other") is None
    messages = store.list_web_messages(bound)
    assert messages == []
    store.close()


def test_binding_merges_guest_messages_into_existing_feishu_conversation(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _first_token, first = new_web_identity(store)
    first_code = issue_binding_code(store, first)
    bound_first = claim_binding_code(store, first_code["code"], "ou_shared")
    first_conversation = store.ensure_web_conversation(bound_first)
    store.append_web_message(first_conversation["id"], "first", "user", "来自第一台设备")

    _second_token, second = new_web_identity(store)
    second_conversation = store.ensure_web_conversation(second)
    store.append_web_message(second_conversation["id"], "second", "user", "绑定前的游客消息")
    second_code = issue_binding_code(store, second)
    bound_second = claim_binding_code(store, second_code["code"], "ou_shared")

    messages = store.list_web_messages(bound_second)
    assert [message["content"] for message in messages] == ["来自第一台设备", "绑定前的游客消息"]
    assert store.ensure_web_conversation(bound_first)["id"] == store.ensure_web_conversation(bound_second)["id"]
    store.close()


def test_bound_feishu_messages_are_mirrored_into_web_conversation(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    binding = issue_binding_code(store, identity)
    bound = claim_binding_code(store, binding["code"], "ou_feishu_user")

    assert store.mirror_feishu_message("ou_feishu_user", "feishu:1", "user", "2608.25927") is True
    assert store.mirror_feishu_message("ou_unknown", "feishu:2", "user", "nothing") is False
    messages = store.list_web_messages(bound)
    assert len(messages) == 1
    assert messages[0]["channel"] == "feishu"
    assert messages[0]["content"] == "2608.25927"
    store.close()


def test_feishu_binding_command_claims_code_and_replies(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    binding = issue_binding_code(store, identity)

    class Feishu:
        replies = []

        def reply_text(self, message_id, text, **_kwargs):
            self.replies.append((message_id, text))

    feishu = Feishu()
    event = SimpleNamespace(
        event_id="evt-bind",
        message_id="om-bind",
        chat_type="p2p",
        sender_id="ou_feishu_user",
        content=binding["command"],
    )

    assert _handle_web_binding_event(SimpleNamespace(lark_cli="missing-lark-cli"), store, feishu, event) is True
    assert "已绑定" in feishu.replies[0][1]
    assert store.get_web_identity(identity["session_hash"])["feishu_open_id"] == "ou_feishu_user"
    store.close()


def test_web_submit_page_is_compact_and_supports_binding():
    assert "论文会话" in WEB_SUBMIT_HTML
    assert "绑定飞书账号" in WEB_SUBMIT_HTML
    assert "/api/web/submit" in WEB_SUBMIT_HTML
    assert "/api/web/retry" in WEB_SUBMIT_HTML
    assert "/api/web/pet/chat" in WEB_SUBMIT_HTML
    assert 'id="progress-track"' not in WEB_SUBMIT_HTML
    assert 'class="progress-track"' in WEB_SUBMIT_HTML
    assert 'class="retry-button"' in WEB_SUBMIT_HTML
    assert WEB_SUBMIT_HTML.index('id="history"') < WEB_SUBMIT_HTML.index('id="message-input"')
    assert "web-pet-sprite.png" in WEB_SUBMIT_HTML
    assert "小绿" not in WEB_SUBMIT_HTML
    assert "和 Max 聊聊" in WEB_SUBMIT_HTML
    assert "maxreadOnboardingSeen" in WEB_SUBMIT_HTML
    assert 'id="console-link"' in WEB_SUBMIT_HTML
    assert "class=\"console-link hidden\"" in WEB_SUBMIT_HTML
    assert "loadAdminContext()" in WEB_SUBMIT_HTML
    assert "maxread_web_session" not in WEB_SUBMIT_HTML
    assert "linear-gradient" not in WEB_SUBMIT_HTML
    assert "font-size:28px" in WEB_SUBMIT_HTML
