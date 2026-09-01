from types import SimpleNamespace

import pytest

from maxread.cli import _handle_web_binding_event
from maxread.db import Store
from maxread.project_metadata import UNCLASSIFIED_CATEGORY, is_placeholder_project_title
from maxread.web_submit import (
    WEB_SUBMIT_HTML,
    claim_binding_code,
    create_web_project_category,
    issue_binding_code,
    new_web_identity,
    organize_web_projects,
    retry_web_job,
    submit_web_papers,
    update_web_project,
)
from maxread.web_pet import WebPetAgent, _parse_agent_action, auto_project_category, chat_with_project_pet, deterministic_status_answer, progress_payload


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


def test_arxiv_id_title_is_treated_as_placeholder():
    assert is_placeholder_project_title("arXiv 2410.02367", "2410.02367") is True
    assert is_placeholder_project_title("arXiv ID: 2410.02367", "2410.02367") is True
    assert is_placeholder_project_title("2410.02367", "2410.02367") is True
    assert is_placeholder_project_title("SageAttention: Accurate 8-bit Attention", "2410.02367") is False


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


def test_project_progress_separates_user_retries_from_service_recovery(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")
    queued = submit_web_papers(settings, store, identity, "2411.10958")["items"][0]
    job_id = queued["job_id"]
    store.add_job_event(job_id, "recover_dead_worker", "worker exited")
    store.add_job_event(job_id, "recover_dead_worker", "worker exited again")
    store.add_job_event(job_id, "web_retry", "user requested retry")

    project = progress_payload(settings, store, identity)["recent"][0]

    assert project["user_retries"] == 1
    assert project["service_recoveries"] == 2
    assert project["auto_retries"] == 0
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


def test_project_pet_parser_uses_last_valid_json_without_leaking_model_preamble():
    raw = (
        'The user said "hi". I need to respond in JSON.\n'
        '{"type":"answer","text":"自然简短的中文"}\n'
        '{"type":"answer","text":"嗨，我是 Max。想问进度还是随便聊聊？"}'
    )

    action = _parse_agent_action(raw)

    assert action == {"type": "answer", "text": "嗨，我是 Max。想问进度还是随便聊聊？"}


def test_project_pet_never_returns_unparsed_model_output(monkeypatch):
    agent = WebPetAgent.__new__(WebPetAgent)
    monkeypatch.setattr(agent, "_model_call", lambda _transcript: "The user said hi. I should explain my JSON format.")

    answer = agent._agent_loop("hi", {}, [])

    assert answer == "我查了几步还没组织好答案。你可以换个更具体的问题，比如“当前任务到哪了”。"
    assert "The user said" not in answer


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
    assert job["suppress_progress_notifications"] == 1
    task_messages = [item for item in store.list_web_messages(identity) if item["source_id"] == "2608.25927"]
    assert len(task_messages) == 1
    assert task_messages[0]["status"] == "queued"
    assert not any(item["kind"] == "retry_request" for item in store.list_web_messages(identity))
    store.close()


def test_web_retry_does_not_reopen_historical_feishu_watcher(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, guest = new_web_identity(store)
    identity = claim_binding_code(store, issue_binding_code(store, guest)["code"], "ou_bound")
    feishu_usage = store.add_usage_event(
        "evt-feishu", "om_feishu", "oc_feishu", "p2p", "ou_bound",
        "paper", "2108.12409", "url", status="queued",
    )
    queued = store.enqueue_job(
        "paper", "2108.12409", "url", "evt-feishu", "om_feishu",
        "oc_feishu", "p2p", "ou_bound", feishu_usage,
    )
    web_usage = store.add_usage_event(
        "evt-web", "web-message:1", f"web:{identity['public_id']}", "web", "ou_bound",
        "paper", "2108.12409", "url", status="queued",
    )
    conversation = store.ensure_web_conversation(identity)
    store.append_web_message(conversation["id"], "web-message:1", "user", "2108.12409")
    same = store.enqueue_job(
        "paper", "2108.12409", "url", "evt-web", "web-message:1",
        f"web:{identity['public_id']}", "web", "ou_bound", web_usage,
    )
    assert same["job_id"] == queued["job_id"]
    store.fail_queue_job(queued["job_id"], "visual-qa:high:raw-formatting")
    store.update_usage_event(feishu_usage, "failed", error="visual-qa:high:raw-formatting")
    store.update_usage_event(web_usage, "failed", error="visual-qa:high:raw-formatting")
    store.conn.execute("update job_watchers set notified=1 where job_id=?", (queued["job_id"],))
    store.conn.commit()

    result = retry_web_job(SimpleNamespace(queue_workers=3), store, identity, queued["job_id"])

    assert result["ok"] is True
    watchers = [
        dict(row) for row in store.conn.execute(
            "select chat_type,chat_id,notified from job_watchers where job_id=? order by id",
            (queued["job_id"],),
        ).fetchall()
    ]
    assert watchers == [
        {"chat_type": "p2p", "chat_id": "oc_feishu", "notified": 1},
        {"chat_type": "web", "chat_id": f"web:{identity['public_id']}", "notified": 0},
    ]
    assert store.get_queue_job(queued["job_id"])["suppress_progress_notifications"] == 1
    assert store.list_usage_events(limit=10)[1]["status"] == "failed"
    store.close()


def test_retry_button_rebuilds_published_document_for_deterministic_format_failure(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3)
    queued = submit_web_papers(settings, store, identity, "2108.12409")["items"][0]
    error = (
        "post-publish:quality:format:xml:high:raw-tex-formatting-command; "
        "visual-qa:high:raw-formatting:页面显示了格式化控制字符：\\mathrm "
        "[screenshot=/home/user/.local/share/maxread-browser/runs/example.png]"
    )
    store.conn.execute(
        "update queue_jobs set status='failed', workflow_state='quality_failed', stage='failed', "
        "doc_url='https://tenant/doc', checkpoint_json='{}', error=? where id=?",
        (error, queued["job_id"]),
    )
    store.conn.commit()

    result = retry_web_job(settings, store, identity, queued["job_id"])

    assert result["ok"] is True
    assert result["resume_published"] is False
    job = store.get_queue_job(queued["job_id"])
    assert job["rebuild_pipeline"] == 1
    assert job["checkpoint_json"] == ""
    assert "raw-tex-formatting-command" in job["retry_feedback"]
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
    assert result["resume_published"] is False
    row = store.get_queue_job(target["job_id"])
    assert row["status"] == "queued"
    assert row["checkpoint_json"] == ""
    assert row["rebuild_pipeline"] == 1
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
    active_project = progress_payload(settings, store, identity)["recent"][0]
    store.conn.execute(
        "update queue_jobs set status='done', workflow_state='completed', stage='done' where source_id=?",
        ("2608.25927",),
    )
    store.conn.commit()
    project = progress_payload(settings, store, identity)["recent"][0]

    assert active_project["category"] == "进行中"
    assert project["favorite"] is True
    assert project["category"] == "机器人"
    assert project["category_source"] == "manual"
    store.close()


def test_bound_identity_can_create_and_use_private_custom_category(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, guest = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="")
    submit_web_papers(settings, store, guest, "2608.25927")
    identity = claim_binding_code(store, issue_binding_code(store, guest)["code"], "ou_custom_category")

    created = create_web_project_category(store, identity, "位置编码")
    update_web_project(store, identity, "2608.25927", "category", "位置编码")
    store.conn.execute(
        "update queue_jobs set status='done', workflow_state='completed', stage='done' where source_id=?",
        ("2608.25927",),
    )
    store.conn.commit()
    payload = progress_payload(settings, store, identity)

    assert created == {"ok": True, "category": "位置编码", "created": True}
    assert "位置编码" in payload["categories"]
    assert payload["recent"][0]["category"] == "位置编码"

    _other_token, other = new_web_identity(store)
    assert "位置编码" not in progress_payload(settings, store, other)["categories"]
    store.close()


def test_guest_cannot_create_custom_category(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, guest = new_web_identity(store)
    with pytest.raises(ValueError, match="绑定飞书账号"):
        create_web_project_category(store, guest, "位置编码")
    store.close()


def test_one_click_organizer_requires_bound_identity(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)

    with pytest.raises(ValueError, match="绑定飞书账号"):
        organize_web_projects(SimpleNamespace(openai_api_key=""), store, identity, [])
    store.close()


def test_one_click_organizer_clusters_auto_projects_and_preserves_manual_category(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, guest = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="", workdir=tmp_path / "work")
    submit_web_papers(settings, store, guest, "2608.25927 2608.27456")
    identity = claim_binding_code(store, issue_binding_code(store, guest)["code"], "ou_feishu_user")
    store.upsert_paper("2608.25927", "done", title="Monocular 3D Object Detection with Point Clouds")
    store.upsert_paper("2608.27456", "done", title="LLM Inference with KV Cache Compression")
    store.conn.execute(
        "update queue_jobs set status='done', workflow_state='completed', stage='done' where source_id in (?, ?)",
        ("2608.25927", "2608.27456"),
    )
    store.conn.commit()
    update_web_project(store, identity, "2608.25927", "category", "机器人")

    projects = progress_payload(settings, store, identity)["recent"]
    result = organize_web_projects(settings, store, identity, projects, ["2608.27456"])
    organized = {item["source_id"]: item for item in progress_payload(settings, store, identity)["recent"]}

    assert result == {"ok": True, "updated": 1, "used_ai": False}
    assert organized["2608.25927"]["category"] == "机器人"
    assert organized["2608.25927"]["category_source"] == "manual"
    assert organized["2608.27456"]["category"] == "推理与系统"
    assert organized["2608.27456"]["category_source"] == "ai"
    store.close()


def test_new_project_stays_in_active_category_until_completion(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, guest = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="", workdir=tmp_path / "work")
    submit_web_papers(settings, store, guest, "2608.25927")
    identity = claim_binding_code(store, issue_binding_code(store, guest)["code"], "ou_feishu_user")
    store.upsert_paper("2608.25927", "queued", title="Diffusion Models for Video Generation")

    project = progress_payload(settings, store, identity)["recent"][0]

    assert project["category"] == "进行中"
    assert project["category_source"] == "status"
    assert progress_payload(settings, store, identity)["categories"][0] == "进行中"
    store.close()


def test_completed_project_waits_unclassified_until_selected_for_organizing(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, guest = new_web_identity(store)
    workdir = tmp_path / "work"
    settings = SimpleNamespace(queue_workers=3, openai_api_key="", workdir=workdir)
    queued = submit_web_papers(settings, store, guest, "2608.25927")["items"][0]
    identity = claim_binding_code(store, issue_binding_code(store, guest)["code"], "ou_feishu_user")
    store.upsert_paper("2608.25927", "done", title="A General Framework", project_summary="")
    store.conn.execute(
        "update queue_jobs set status='done', workflow_state='completed', stage='done', title=? where id=?",
        ("A General Framework", queued["job_id"]),
    )
    store.conn.commit()
    artifact = workdir / "papers" / "2608.25927" / "pipeline_artifacts" / "05-final.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "# [2608.25927] 通用框架：可控视频扩散生成\n\n"
        "**TL;DR**：本文用扩散模型生成长视频，并保持跨镜头一致性。\n\n"
        "## 1. 这篇论文要解决什么问题\n\n现有视频生成方法难以维持长时一致性。\n",
        encoding="utf-8",
    )

    project = progress_payload(settings, store, identity)["recent"][0]

    assert project["category"] == UNCLASSIFIED_CATEGORY
    assert project["category_source"] == "unclassified"
    assert store.web_project_preferences(identity).get("2608.25927", {}).get("category", "") == ""

    result = organize_web_projects(settings, store, identity, [project], ["2608.25927"])
    organized = progress_payload(settings, store, identity)["recent"][0]

    assert result["updated"] == 1
    assert organized["category"] == "生成模型"
    assert organized["category_source"] == "ai"
    store.close()


def test_selected_organizer_leaves_unselected_completed_project_unclassified(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, guest = new_web_identity(store)
    settings = SimpleNamespace(queue_workers=3, openai_api_key="", workdir=tmp_path / "work")
    submit_web_papers(settings, store, guest, "2608.25927 2608.27456")
    identity = claim_binding_code(store, issue_binding_code(store, guest)["code"], "ou_feishu_user")
    store.upsert_paper("2608.25927", "done", title="Video Diffusion Generation")
    store.upsert_paper("2608.27456", "done", title="KV Cache Compression")
    store.conn.execute(
        "update queue_jobs set status='done', workflow_state='completed', stage='done' where source_id in (?, ?)",
        ("2608.25927", "2608.27456"),
    )
    store.conn.commit()
    projects = progress_payload(settings, store, identity)["recent"]

    organize_web_projects(settings, store, identity, projects, ["2608.25927"])
    organized = {item["source_id"]: item for item in progress_payload(settings, store, identity)["recent"]}

    assert organized["2608.25927"]["category"] == "生成模型"
    assert organized["2608.27456"]["category"] == UNCLASSIFIED_CATEGORY
    assert organized["2608.27456"]["category_source"] == "unclassified"
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
    assert project["category"] == UNCLASSIFIED_CATEGORY
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
    assert "/api/web/organize" in WEB_SUBMIT_HTML
    assert "/api/web/categories" in WEB_SUBMIT_HTML
    assert "新建自定义分类" in WEB_SUBMIT_HTML
    assert "自动归类所选" in WEB_SUBMIT_HTML
    assert "toggleProjectSelection" in WEB_SUBMIT_HTML
    assert "toggleAllUnclassified" in WEB_SUBMIT_HTML
    assert "toggleCategory(" in WEB_SUBMIT_HTML
    assert "带我看页面" in WEB_SUBMIT_HTML
    assert "startPageTour" in WEB_SUBMIT_HTML
    assert "tourTransitioning" in WEB_SUBMIT_HTML
    assert "finishTourStep" in WEB_SUBMIT_HTML
    assert "shouldMove ? 420 : 0" in WEB_SUBMIT_HTML
    assert "if (state.tourTransitioning) return" in WEB_SUBMIT_HTML
    assert 'data-guide="submit"' in WEB_SUBMIT_HTML
    assert 'data-guide="project-pet"' in WEB_SUBMIT_HTML
    assert 'data-guide="organize"' in WEB_SUBMIT_HTML
    assert 'id="tour-coach"' in WEB_SUBMIT_HTML
    assert "点右侧“开始阅读”" in WEB_SUBMIT_HTML
    assert "完成后再批量归类" in WEB_SUBMIT_HTML
    assert 'id="onboarding-dialog"' not in WEB_SUBMIT_HTML
    assert "智能整理" in WEB_SUBMIT_HTML
    assert ".category-list[hidden]" in WEB_SUBMIT_HTML
    assert "完成后进入未分类" in WEB_SUBMIT_HTML
    assert "animateProjectMoves" in WEB_SUBMIT_HTML
    assert "篇 →" in WEB_SUBMIT_HTML
    assert "已到 ${category}" in WEB_SUBMIT_HTML
    assert "data-category=\"${esc(category)}\"" in WEB_SUBMIT_HTML
    assert "projectMoveBusy" in WEB_SUBMIT_HTML
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
    assert 'id="pipeline-link"' in WEB_SUBMIT_HTML
    assert 'href="./architecture"' in WEB_SUBMIT_HTML
    assert "startBindingTour" in WEB_SUBMIT_HTML
    assert "发送下面这条完整指令" in WEB_SUBMIT_HTML
    assert "回到本页等待姓名自动出现" in WEB_SUBMIT_HTML
    assert "class=\"console-link\"" in WEB_SUBMIT_HTML
    assert 'href="./projects"' in WEB_SUBMIT_HTML
    assert 'href="./admin"' in WEB_SUBMIT_HTML
    assert "maxread_web_session" not in WEB_SUBMIT_HTML
    assert "linear-gradient" not in WEB_SUBMIT_HTML
    assert "font-size: 28px" in WEB_SUBMIT_HTML
