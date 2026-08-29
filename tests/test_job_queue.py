from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

from maxread.db import Store
from maxread.web_submit import claim_binding_code, issue_binding_code, new_web_identity

from maxread.job_queue import (
    _LimitedLLM,
    _is_auto_retryable_error,
    _notify_watchers,
    _notify_watchers_progress,
    _notify_watchers_started,
    _published_doc_url,
    _queue_eta_text,
    QueueManager,
)


class _DummyLLM:
    def __init__(self):
        self.calls = []
        self.image_calls = []

    def responses_text(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return "ok"

    def responses_image_text(self, system, user, image_path):
        self.image_calls.append((system, user, image_path))
        return "image ok"


def test_limited_llm_announces_reading_then_reviewing():
    events = []
    llm = _LimitedLLM(
        _DummyLLM(),
        BoundedSemaphore(1),
        on_call=lambda: events.append("reading"),
        on_review=lambda: events.append("reviewing"),
    )

    assert llm.responses_text("普通总结 prompt", "") == "ok"
    assert llm.responses_text("你是 MaxRead 的发布前质量检查员。", "") == "ok"
    assert llm.responses_text("你是 MaxRead 的发布前质量检查员。", "") == "ok"

    assert events == ["reading", "reviewing"]


def test_limited_llm_proxies_image_text_under_semaphore():
    events = []
    inner = _DummyLLM()
    llm = _LimitedLLM(inner, BoundedSemaphore(1), on_call=lambda: events.append("reading"))

    assert llm.responses_image_text("vision", "describe", "a.png") == "image ok"

    assert events == ["reading"]
    assert inner.image_calls == [("vision", "describe", "a.png")]


def test_limited_llm_announces_once_under_parallel_section_calls():
    events = []
    llm = _LimitedLLM(
        _DummyLLM(),
        BoundedSemaphore(5),
        on_call=lambda: events.append("reading"),
    )

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: llm.responses_text("generate", "section"), range(5)))

    assert results == ["ok"] * 5
    assert events == ["reading"]


def test_limited_llm_progress_failure_does_not_discard_model_output():
    def broken_progress():
        raise RuntimeError("sqlite progress connection used from another thread")

    llm = _LimitedLLM(_DummyLLM(), BoundedSemaphore(1), on_call=broken_progress)

    assert llm.responses_text("generate", "section") == "ok"


class _ReactionFeishu:
    def __init__(self):
        self.reactions = []
        self.replies = []

    def set_progress_reaction(self, message_id, stage):
        self.reactions.append((message_id, stage))

    def reply_text(self, message_id, text, idempotency_key=None):
        self.replies.append((message_id, text))



def test_queue_eta_text_accounts_for_parallel_workers():
    assert _queue_eta_text(1, 5, 720) == "队列第 1 位，并发槽位内；预计等待约 0 分钟，预计生成约 12 分钟，预计完成约 12 分钟。"
    assert _queue_eta_text(5, 5, 720) == "队列第 5 位，并发槽位内；预计等待约 0 分钟，预计生成约 12 分钟，预计完成约 12 分钟。"
    assert _queue_eta_text(6, 5, 720) == "队列第 6 位，约第 2 批开始；预计等待约 12 分钟，预计生成约 12 分钟，预计完成约 24 分钟。"


def test_notify_watchers_progress_uses_reactions_not_text(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om_1", "oc", "p2p", "ou", "paper", "2604.12946", "https://arxiv.org/abs/2604.12946", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "https://arxiv.org/abs/2604.12946", "evt", "om_1", "oc", "p2p", "ou", usage_id)
    feishu = _ReactionFeishu()

    _notify_watchers_progress(store, feishu, queued["job_id"], "[敲键盘] 在写飞书文档：2604.12946", "writing", "job-writing")

    assert feishu.reactions == [("om_1", "writing")]
    assert feishu.replies == []
    events = store.list_job_events(queued["job_id"], 10)
    assert any(event["event_type"] == "react_writing" for event in events)
    store.close()


def test_web_watcher_updates_usage_without_feishu_side_effects(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    identity = store.get_or_create_web_identity("session-hash", "web_123")
    conversation = store.ensure_web_conversation(identity)
    store.append_web_message(conversation["id"], "web-message:1", "user", "2604.12946")
    usage_id = store.add_usage_event(
        "web-event", "web-message:1", "web:web_123", "web", "guest:web_123",
        "paper", "2604.12946", "https://arxiv.org/abs/2604.12946", status="queued",
    )
    queued = store.enqueue_job(
        "paper", "2604.12946", "https://arxiv.org/abs/2604.12946",
        "web-event", "web-message:1", "web:web_123", "web", "guest:web_123", usage_id,
    )
    feishu = _ReactionFeishu()

    _notify_watchers_started(store, feishu, queued["job_id"], "2604.12946")
    _notify_watchers_progress(store, feishu, queued["job_id"], "reading", "reading", "web")
    _notify_watchers(store, feishu, queued["job_id"], "2604.12946", "https://tenant/doc", "Paper", "")

    assert feishu.reactions == []
    assert feishu.replies == []
    row = store.list_web_submissions("web_123")[0]
    assert row["status"] == "done"
    assert row["doc_url"] == "https://tenant/doc"
    assert store.get_job_watchers(queued["job_id"]) == []
    messages = store.list_web_messages(identity)
    assert messages[-1]["kind"] == "result"
    assert messages[-1]["doc_url"] == "https://tenant/doc"
    store.close()


def test_bound_feishu_watcher_is_replied_and_mirrored_to_web(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    _token, identity = new_web_identity(store)
    binding = issue_binding_code(store, identity)
    bound = claim_binding_code(store, binding["code"], "ou_bound")
    usage_id = store.add_usage_event(
        "evt", "om_1", "oc", "p2p", "ou_bound", "paper", "2604.12946", "url", status="queued",
    )
    queued = store.enqueue_job(
        "paper", "2604.12946", "url", "evt", "om_1", "oc", "p2p", "ou_bound", usage_id,
    )
    feishu = _ReactionFeishu()

    _notify_watchers(store, feishu, queued["job_id"], "2604.12946", "https://tenant/doc", "Paper", "")

    assert len(feishu.replies) == 1
    messages = store.list_web_messages(bound)
    assert messages[-1]["kind"] == "result"
    assert messages[-1]["channel"] == "system"
    assert messages[-1]["doc_url"] == "https://tenant/doc"
    store.close()


def test_recovered_job_suppresses_progress_reactions_until_terminal(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om_1", "oc", "group", "ou", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om_1", "oc", "group", "ou", usage_id)
    store.claim_next_queue_job(worker_id="host-a:12345:worker")
    assert store.recover_dead_worker_queue_jobs("host-a", lambda pid: False) == 1
    row = store.claim_next_queue_job(worker_id="worker-b")
    assert row["suppress_progress_notifications"] == 1

    feishu = _ReactionFeishu()
    _notify_watchers_started(store, feishu, queued["job_id"], "2604.12946", suppress_progress_notifications=True)
    _notify_watchers_progress(
        store,
        feishu,
        queued["job_id"],
        "[在做了] 正在读论文：2604.12946",
        "reading",
        "job-reading",
        suppress_progress_notifications=True,
    )

    assert feishu.reactions == []
    assert any(event["event_type"] == "progress_notifications_suppressed" for event in store.list_job_events(queued["job_id"]))
    assert store.complete_queue_job(queued["job_id"], "https://doc", worker_id="worker-b") is True
    assert store.list_queue_jobs()[0]["suppress_progress_notifications"] == 0
    store.close()


def test_failed_notification_explains_topic_retry(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om_1", "oc", "group", "ou", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om_1", "oc", "group", "ou", usage_id)
    feishu = _ReactionFeishu()

    _notify_watchers(store, feishu, queued["job_id"], "2604.12946", "", "Title", "quality failed")

    assert len(feishu.replies) == 1
    assert "本话题回复「重试」" in feishu.replies[0][1]
    store.close()


def test_visual_failure_notification_keeps_published_doc_for_manual_review(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om_1", "oc", "group", "ou", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om_1", "oc", "group", "ou", usage_id)
    feishu = _ReactionFeishu()
    published_url = "https://feishu/doc"
    error = "文档已生成，但发布后质检失败，暂不交付：visual-qa:high:invalid-formula"

    assert _published_doc_url(type("Result", (), {"doc_url": published_url, "error": error})()) == published_url
    _notify_watchers(
        store,
        feishu,
        queued["job_id"],
        "2604.12946",
        "",
        "Title",
        error,
        published_doc_url=published_url,
    )

    assert len(feishu.replies) == 1
    text = feishu.replies[0][1]
    assert "视觉审查重试到上限后仍未通过" in text
    assert published_url in text
    usage = store.conn.execute("select status, doc_url from usage_events where id = ?", (usage_id,)).fetchone()
    assert tuple(usage) == ("failed", published_url)
    assert any(event["event_type"] == "notify_failed_with_doc" for event in store.list_job_events(queued["job_id"]))
    store.close()


def test_recovered_failure_is_silent_but_success_reports_interruption(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om_1", "oc", "group", "ou", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om_1", "oc", "group", "ou", usage_id)
    feishu = _ReactionFeishu()

    _notify_watchers(
        store,
        feishu,
        queued["job_id"],
        "2604.12946",
        "",
        "Title",
        "browser timeout",
        notify_failure=False,
    )
    assert feishu.replies == []
    assert store.conn.execute(
        "select notified from job_watchers where job_id = ?", (queued["job_id"],)
    ).fetchone()[0] == 1

    store.conn.execute("update job_watchers set notified = 0 where job_id = ?", (queued["job_id"],))
    store.conn.commit()
    _notify_watchers(
        store,
        feishu,
        queued["job_id"],
        "2604.12946",
        "https://doc",
        "Title",
        "",
        recovery_reason="5090 的 NFS client 卡死，无法正常完成文件读写，造成服务异常",
    )
    assert len(feishu.replies) == 1
    assert "NFS client 卡死" in feishu.replies[0][1]
    store.close()


def test_silent_retry_suppresses_success_notification_but_records_done(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om_1", "oc", "group", "ou", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om_1", "oc", "group", "ou", usage_id)
    feishu = _ReactionFeishu()

    _notify_watchers(
        store,
        feishu,
        queued["job_id"],
        "2604.12946",
        "https://doc",
        "Title",
        "",
        notify_success=False,
    )

    assert feishu.replies == []
    usage = store.conn.execute("select status, doc_url from usage_events where id = ?", (usage_id,)).fetchone()
    assert tuple(usage) == ("done", "https://doc")
    assert any(event["event_type"] == "notify_done_suppressed" for event in store.list_job_events(queued["job_id"]))
    store.close()


def test_transient_failure_is_requeued_once_without_becoming_user_visible_failure(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om_1", "oc", "group", "ou", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om_1", "oc", "group", "ou", usage_id)
    store.conn.execute("update queue_jobs set suppress_progress_notifications = 1 where id = ?", (queued["job_id"],))
    store.conn.commit()
    job = store.claim_next_queue_job(worker_id="worker-a")
    manager = object.__new__(QueueManager)
    manager.settings = type("Settings", (), {"auto_retry_attempts": 1})()

    assert _is_auto_retryable_error("visual-qa:remote-error:browser timeout") is True
    assert _is_auto_retryable_error("source_missing: TeX source unavailable") is False
    assert _is_auto_retryable_error("quality:formula:xml:high:unsupported-paper-macro") is False
    assert _is_auto_retryable_error("生成格式不完整，未发布文档") is True
    assert manager._auto_retry(store, job, "feishu connection timeout while create_docx", "worker-a") is False
    assert manager._auto_retry(store, job, "visual-qa:remote-error:browser timeout", "worker-a") is True
    row = store.list_queue_jobs()[0]
    assert row["status"] == "queued"
    assert row["workflow_state"] == "queued"
    assert row["suppress_progress_notifications"] == 1
    assert row["auto_retry_count"] == 1
    assert any(event["event_type"] == "auto_retry" for event in store.list_job_events(queued["job_id"]))

    second_attempt = store.claim_next_queue_job(worker_id="worker-b")
    assert manager._auto_retry(store, second_attempt, "visual-qa:remote-error:browser timeout", "worker-b") is False
    store.close()


def test_visual_auto_retry_uses_latest_published_checkpoint_after_many_manual_attempts(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om_1", "oc", "group", "ou", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om_1", "oc", "group", "ou", usage_id)
    claimed = store.claim_next_queue_job(worker_id="worker-a")
    store.conn.execute(
        "update queue_jobs set attempts = 8, rebuild_pipeline = 1, checkpoint_json = ?, doc_url = ? where id = ?",
        ('{"doc_url":"https://published-doc"}', "https://published-doc", queued["job_id"]),
    )
    store.conn.commit()
    manager = object.__new__(QueueManager)
    manager.settings = type("Settings", (), {"auto_retry_attempts": 1})()

    assert manager._auto_retry(
        store,
        claimed,
        "visual-qa:infrastructure:export-pending:ticket=123",
        "worker-a",
    ) is True

    row = store.get_queue_job(queued["job_id"])
    assert row["status"] == "queued"
    assert row["attempts"] == 8
    assert row["auto_retry_count"] == 1
    assert row["checkpoint_json"] == '{"doc_url":"https://published-doc"}'
    assert row["rebuild_pipeline"] == 0
    assert "retry_mode=resume" in store.list_job_events(queued["job_id"])[0]["detail"]
    store.close()
