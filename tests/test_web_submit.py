from types import SimpleNamespace

import pytest

from maxread.cli import _handle_web_binding_event
from maxread.db import Store
from maxread.web_submit import (
    WEB_SUBMIT_HTML,
    claim_binding_code,
    issue_binding_code,
    new_web_identity,
    retry_web_job,
    submit_web_papers,
    update_web_project,
)
from maxread.web_pet import WebPetAgent, auto_project_category, chat_with_project_pet, deterministic_status_answer, progress_payload


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


def test_overdue_project_does_not_claim_one_minute_remaining():
    answer = deterministic_status_answer({
        "active": {
            "source_id": "2308.04079",
            "label": "生成初稿",
            "percent": 38,
            "remaining_seconds": 0,
            "elapsed_seconds": 24 * 60,
        },
        "recent": [],
    })

    assert "运行约 24 分钟" in answer
    assert "假的倒计时" in answer
    assert "还要 1 分钟" not in answer


def test_project_pet_rejects_another_identity_job(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token_a, identity_a = new_web_identity(store)
    _token_b, identity_b = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")
    submit_web_papers(settings, store, identity_a, "2608.25927")
    other = submit_web_papers(settings, store, identity_b, "2608.27456")["items"][0]

    with pytest.raises(ValueError, match="项目不在当前账号范围"):
        chat_with_project_pet(
            settings,
            store,
            identity_a,
            "进度到哪里了？",
            job_id=other["job_id"],
        )
    store.close()


def test_project_pet_chat_is_ephemeral_and_scoped(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")

    queued = submit_web_papers(settings, store, identity, "2608.25927")["items"][0]
    before = store.list_web_messages(identity)
    result = chat_with_project_pet(
        settings,
        store,
        identity,
        "任务到哪了？",
        job_id=queued["job_id"],
    )

    assert result["ok"] is True
    assert "等待调度" in result["message"]["content"]
    assert store.list_web_messages(identity) == before
    store.close()


def test_project_pet_investigates_heartbeat_instead_of_guessing(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(
        queue_workers=3,
        queue_stale_minutes=10,
        openai_api_key="",
        workdir=tmp_path / "work",
    )
    queued = submit_web_papers(settings, store, identity, "2608.25927")["items"][0]
    store.conn.execute(
        "update queue_jobs set status='running', workflow_state='generating', stage='reading', "
        "worker_id='host:1:worker', heartbeat_at=current_timestamp, stage_updated_at=datetime('now', '-4 minutes') where id=?",
        (queued["job_id"],),
    )
    store.conn.commit()

    result = chat_with_project_pet(
        settings,
        store,
        identity,
        "怎么一直卡在生成初稿？",
        job_id=queued["job_id"],
    )

    assert "心跳" in result["message"]["content"]
    assert "进程没有挂" in result["message"]["content"]
    store.close()


def test_project_pet_can_retry_owned_failed_project_on_explicit_request(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(
        queue_workers=3,
        queue_stale_minutes=10,
        openai_api_key="",
        workdir=tmp_path / "work",
    )
    queued = submit_web_papers(settings, store, identity, "2608.25927")["items"][0]
    store.conn.execute(
        "update queue_jobs set status='failed', workflow_state='quality_failed', stage='failed', error='invalid formula' where id=?",
        (queued["job_id"],),
    )
    store.conn.commit()

    result = chat_with_project_pet(
        settings,
        store,
        identity,
        "请调查并修复这个项目",
        job_id=queued["job_id"],
    )

    assert "重新加入队列" in result["message"]["content"]
    job = next(item for item in store.list_queue_jobs() if item["id"] == queued["job_id"])
    assert job["status"] == "queued"
    assert any(item["event_type"] == "project_agent_retry" for item in store.list_job_events(queued["job_id"], 20))
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
    task_messages = [item for item in store.list_web_messages(identity) if item["source_id"] == "2608.25927"]
    assert len(task_messages) == 1
    assert task_messages[0]["status"] == "queued"
    assert not any(item["kind"] == "retry_request" for item in store.list_web_messages(identity))
    store.close()


def test_retry_old_owned_project_is_not_limited_by_recent_project_page(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=2)
    target = submit_web_papers(settings, store, identity, "2212.02509")["items"][0]
    store.conn.execute(
        "update queue_jobs set status='failed', workflow_state='quality_failed', stage='failed', "
        "doc_url='https://tenant/doc', checkpoint_json=?, error=? where id=?",
        (
            '{"doc_url":"https://tenant/doc"}',
            "文档已生成，但发布后质检失败：visual-qa:high:table-overflow",
            target["job_id"],
        ),
    )
    store.conn.commit()

    for index in range(35):
        source_id = f"2608.{30000 + index}"
        usage_id = store.add_usage_event(
            f"evt-{index}", f"msg-{index}", f"web:{identity['public_id']}", "web",
            store.web_identity_sender(identity), "paper", source_id, "url", status="queued",
        )
        store.enqueue_job(
            "paper", source_id, "url", f"evt-{index}", f"msg-{index}",
            f"web:{identity['public_id']}", "web", store.web_identity_sender(identity), usage_id,
        )

    assert all(int(item["id"]) != target["job_id"] for item in store.list_web_identity_jobs(identity, 30))

    result = retry_web_job(settings, store, identity, target["job_id"])

    assert result["ok"] is True
    assert result["resume_published"] is True
    row = store.get_queue_job(target["job_id"])
    assert row["status"] == "queued"
    assert row["checkpoint_json"] == '{"doc_url":"https://tenant/doc"}'
    assert row["rebuild_pipeline"] == 0
    store.close()


def test_project_card_is_updated_in_place_across_lifecycle(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3)
    queued = submit_web_papers(settings, store, identity, "2608.25927")["items"][0]
    watcher = store.get_job_watchers(queued["job_id"])[0]

    store.update_web_job_progress(watcher, queued["job_id"], "2608.25927", "正在内容审阅", "running")
    store.append_web_job_result(
        watcher,
        queued["job_id"],
        "2608.25927",
        "完成交付",
        doc_url="https://tenant/doc",
        status="done",
    )

    cards = [item for item in store.list_web_messages(identity) if item["source_id"] == "2608.25927"]
    assert len(cards) == 1
    assert cards[0]["kind"] == "result"
    assert cards[0]["status"] == "done"
    assert cards[0]["doc_url"] == "https://tenant/doc"
    store.close()


def test_cached_document_appears_as_personal_project(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")
    store.upsert_paper(
        "2608.25927",
        "done",
        title="Code World Model",
        doc_url="https://tenant/doc",
    )

    submit_web_papers(settings, store, identity, "2608.25927")
    projects = progress_payload(settings, store, identity)["recent"]

    assert len(projects) == 1
    assert projects[0]["source_id"] == "2608.25927"
    assert projects[0]["status"] == "done"
    assert projects[0]["doc_url"] == "https://tenant/doc"
    store.close()


def test_legacy_done_stage_is_normalized_to_completed_without_twelve_percent(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="", workdir=tmp_path / "work")
    queued = submit_web_papers(settings, store, identity, "2608.25927")["items"][0]
    store.conn.execute(
        "update queue_jobs set status='done', workflow_state='', stage='done', doc_url='https://tenant/doc' where id=?",
        (queued["job_id"],),
    )
    store.conn.commit()

    project = progress_payload(settings, store, identity)["recent"][0]

    assert project["status"] == "done"
    assert project["percent"] == 100
    assert project["label"] == "完成交付"
    store.close()


def test_project_favorite_and_manual_category_are_identity_scoped(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")
    submit_web_papers(settings, store, identity, "2608.25927")

    update_web_project(store, identity, "2608.25927", "favorite", True)
    update_web_project(store, identity, "2608.25927", "category", "机器人")
    project = progress_payload(settings, store, identity)["recent"][0]

    assert project["favorite"] is True
    assert project["category"] == "机器人"
    assert project["category_source"] == "manual"
    store.close()


def test_delete_cancels_only_an_exclusive_queued_web_project(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")
    queued = submit_web_papers(settings, store, identity, "2608.25927")["items"][0]

    result = update_web_project(store, identity, "2608.25927", "delete", True)

    assert result["cancelled"] is True
    assert progress_payload(settings, store, identity)["recent"] == []
    job = next(item for item in store.list_queue_jobs() if item["id"] == queued["job_id"])
    assert job["workflow_state"] == "cancelled"
    store.close()


def test_delete_does_not_cancel_a_project_watched_by_another_user(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token_a, identity_a = new_web_identity(store)
    _token_b, identity_b = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")
    first = submit_web_papers(settings, store, identity_a, "2608.25927")["items"][0]
    second = submit_web_papers(settings, store, identity_b, "2608.25927")["items"][0]
    assert first["job_id"] == second["job_id"]

    result = update_web_project(store, identity_a, "2608.25927", "delete", True)

    assert result["cancelled"] is False
    assert progress_payload(settings, store, identity_a)["recent"] == []
    assert progress_payload(settings, store, identity_b)["recent"][0]["source_id"] == "2608.25927"
    job = next(item for item in store.list_queue_jobs() if item["id"] == first["job_id"])
    assert job["status"] == "queued"
    store.close()


def test_bound_identity_can_delete_legacy_article_project(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, guest = new_web_identity(store)
    code = issue_binding_code(store, guest)["code"]
    identity = claim_binding_code(store, code, "ou_feishu_user")
    source_url = (
        "https://login.feishu.cn/accounts/trap?app_id=2&query_scope=all&"
        "redirect_uri=https%3A%2F%2Ftenant.feishu.cn%2Fdocx%2Flegacy"
    )
    usage_id = store.add_usage_event(
        "evt-article",
        "om-article",
        "oc-p2p",
        "p2p",
        "ou_feishu_user",
        "article",
        source_url,
        source_url,
        status="failed",
    )
    queued = store.enqueue_job(
        "article",
        source_url,
        source_url,
        "evt-article",
        "om-article",
        "oc-p2p",
        "p2p",
        "ou_feishu_user",
        usage_id,
    )
    store.conn.execute(
        "update queue_jobs set status='failed', workflow_state='failed', stage='failed' where id=?",
        (queued["job_id"],),
    )
    store.conn.commit()

    result = update_web_project(store, identity, source_url, "delete", True)

    assert result["ok"] is True
    assert result["cancelled"] is False
    preference = store.web_project_preferences(identity)[source_url]
    assert preference["deleted_at"]
    store.close()


def test_auto_category_uses_title_and_generated_summary():
    assert auto_project_category("Sparse routing for language models", "") == "推理与系统"
    assert auto_project_category("A general framework", "通过视频扩散模型生成长序列") == "生成模型"


def test_existing_project_summary_is_backfilled_from_pipeline_artifact(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    workdir = tmp_path / "work"
    artifact_dir = workdir / "papers" / "2608.25927" / "pipeline_artifacts"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "05-final.md").write_text(
        "# [2608.25927] 中文标题：通过视频扩散模型生成可控长序列\n\n**TL;DR**：备用摘要。\n",
        encoding="utf-8",
    )
    settings = SimpleNamespace(queue_workers=3, openai_api_key="", workdir=workdir)
    store.upsert_paper("2608.25927", "done", title="A general framework", doc_url="https://tenant/doc")
    submit_web_papers(settings, store, identity, "2608.25927")

    project = progress_payload(settings, store, identity)["recent"][0]

    assert project["summary"] == "通过视频扩散模型生成可控长序列"
    assert project["category"] == "生成模型"
    assert store.get_paper("2608.25927").project_summary == project["summary"]
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
    assert "我的论文项目" in WEB_SUBMIT_HTML
    assert "绑定飞书账号" in WEB_SUBMIT_HTML
    assert "/api/web/submit" in WEB_SUBMIT_HTML
    assert "/api/web/retry" in WEB_SUBMIT_HTML
    assert "/api/web/pet/chat" in WEB_SUBMIT_HTML
    assert "/api/web/project-action" in WEB_SUBMIT_HTML
    assert 'id="progress-panel"' not in WEB_SUBMIT_HTML
    assert 'class="project-progress"' in WEB_SUBMIT_HTML
    assert 'class="retry-button"' in WEB_SUBMIT_HTML
    assert 'id="history"' not in WEB_SUBMIT_HTML
    assert 'id="message-input"' not in WEB_SUBMIT_HTML
    assert 'id="projects"' in WEB_SUBMIT_HTML
    assert 'id="paper-input"' in WEB_SUBMIT_HTML
    assert "openPet(" in WEB_SUBMIT_HTML
    assert "petChats" in WEB_SUBMIT_HTML
    assert "Max 正在思考" in WEB_SUBMIT_HTML
    assert "调整项目分类" in WEB_SUBMIT_HTML
    assert "删除项目" in WEB_SUBMIT_HTML
    assert "收藏" in WEB_SUBMIT_HTML
    assert "status === 'done' ? ''" in WEB_SUBMIT_HTML
    assert "project-row completed" not in WEB_SUBMIT_HTML
    assert "web-pet-sprite.png" in WEB_SUBMIT_HTML
    assert "小绿" not in WEB_SUBMIT_HTML
    assert "问 Max" in WEB_SUBMIT_HTML
    assert "maxreadProjectOnboardingSeen" in WEB_SUBMIT_HTML
    assert 'id="console-link"' in WEB_SUBMIT_HTML
    assert "class=\"console-link\"" in WEB_SUBMIT_HTML
    assert 'href="./projects"' in WEB_SUBMIT_HTML
    assert 'href="./admin"' in WEB_SUBMIT_HTML
    assert "maxread_web_session" not in WEB_SUBMIT_HTML
    assert "linear-gradient" not in WEB_SUBMIT_HTML
    assert "font-size: 28px" in WEB_SUBMIT_HTML
