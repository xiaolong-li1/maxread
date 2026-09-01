from maxread.db import QueueLeaseLostError, Store
from maxread.workflow import PublishedCheckpoint, WorkflowEvent, WorkflowState


def test_store_paper_cache(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.upsert_paper("2604.12946", "done", title="T", doc_url="https://doc")
    record = store.get_paper("2604.12946")
    assert record is not None
    assert record.status == "done"
    assert record.doc_url == "https://doc"
    store.close()


def test_store_job_dedupes(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    assert store.add_job("evt", "om", "oc", "2604.12946", "started") is True
    assert store.add_job("evt", "om", "oc", "2604.12946", "started") is False
    store.close()


def test_service_status_is_persistent_and_requires_reason_when_degraded(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    assert store.get_service_status()["mode"] == "operational"
    try:
        store.set_service_status("outage")
        raise AssertionError("outage without a reason was accepted")
    except ValueError:
        pass
    status = store.set_service_status(
        "outage",
        "5090 的 NFS client 卡死，无法正常完成文件读写，造成服务异常",
        "2026-08-22 12:00 Asia/Shanghai",
        "xiaolong",
    )
    assert status["mode"] == "outage"
    assert "NFS client 卡死" in status["reason"]
    assert status["expected_recovery_at"] == "2026-08-22 12:00 Asia/Shanghai"
    assert store.set_service_status("operational")["mode"] == "operational"
    store.close()



def test_store_tracks_intro_and_feedback(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    assert store.should_send_intro_to_user("ou_1") is True
    store.mark_intro_sent("ou_1")
    assert store.should_send_intro_to_user("ou_1") is False
    feedback_id = store.add_feedback("evt", "om", "oc", "p2p", "ou_1", "反馈：图片少")
    assert feedback_id == 1
    assert store.feedback_count() == 1
    rows = store.list_feedback()
    assert rows[0]["content"] == "反馈：图片少"
    assert rows[0]["status"] == "new"
    store.close()


def test_store_persists_ai_feedback_classification(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.add_feedback(
        "evt",
        "om",
        "oc",
        "p2p",
        "ou_1",
        "方法框架图不见了",
        source="ai",
        category="quality",
        confidence=0.94,
    )

    row = store.list_feedback()[0]
    assert row["feedback_source"] == "ai"
    assert row["feedback_category"] == "quality"
    assert row["feedback_confidence"] == 0.94
    store.close()


def test_store_tracks_usage_events(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "https://arxiv.org/abs/2604.12946")
    store.update_usage_event(usage_id, "done", doc_url="https://doc", title="Title")
    rows = store.list_usage_events()
    assert rows[0]["sender_id"] == "ou_1"
    assert rows[0]["source_id"] == "2604.12946"
    assert rows[0]["doc_url"] == "https://doc"
    store.close()


def test_store_queue_jobs_and_watchers(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "https://arxiv.org/abs/2604.12946", status="queued")
    first = store.enqueue_job("paper", "2604.12946", "https://arxiv.org/abs/2604.12946", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    assert first["created"] is True
    second = store.enqueue_job("paper", "2604.12946", "https://arxiv.org/abs/2604.12946", "evt2", "om2", "oc", "p2p", "ou_2", usage_id)
    assert second["created"] is False
    assert second["job_id"] == first["job_id"]
    assert store.queue_position(first["job_id"]) == 1
    job = store.claim_next_queue_job()
    assert job["source_id"] == "2604.12946"
    assert job["workflow_state"] == WorkflowState.CLAIMED.value
    assert store.queue_position(first["job_id"]) == 0
    watchers = [
        dict(row) for row in store.conn.execute(
            "select * from job_watchers where job_id=? order by id",
            (first["job_id"],),
        ).fetchall()
    ]
    assert len(watchers) == 2
    store.complete_queue_job(first["job_id"], "https://doc", "Title")
    rows = store.list_queue_jobs()
    assert rows[0]["status"] == "done"
    assert rows[0]["workflow_state"] == WorkflowState.COMPLETED.value
    assert rows[0]["doc_url"] == "https://doc"
    assert rows[0]["resolved_title"] == "Title"
    events = store.list_job_events(first["job_id"])
    assert any(event["event_type"] == "enqueue" for event in events)
    assert any(event["event_type"] == "claim" for event in events)
    assert any(event["event_type"] == "done" for event in events)
    stats = store.queue_stats()
    assert stats["done"] == 1
    store.close()


def test_queue_claim_can_filter_worker_source_kinds(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    article_usage = store.add_usage_event(
        "evt-a", "om-a", "oc", "p2p", "ou", "article", "article-1", "url", status="queued"
    )
    article = store.enqueue_job(
        "article", "article-1", "url", "evt-a", "om-a", "oc", "p2p", "ou", article_usage
    )
    paper_usage = store.add_usage_event(
        "evt-p", "om-p", "oc", "p2p", "ou", "paper", "2604.12946", "url", status="queued"
    )
    paper = store.enqueue_job(
        "paper", "2604.12946", "url", "evt-p", "om-p", "oc", "p2p", "ou", paper_usage
    )

    claimed = store.claim_next_queue_job(worker_id="article-worker", source_kinds=("article",))

    assert claimed["id"] == article["job_id"]
    assert store.get_queue_job(paper["job_id"])["status"] == "queued"
    store.close()


def test_new_queue_job_persists_suppressed_progress_flag(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage = store.add_usage_event(
        "evt", "om", "internal", "internal", "system", "paper", "2604.12946", "url", status="queued"
    )

    queued = store.enqueue_job(
        "paper",
        "2604.12946",
        "url",
        "evt",
        "om",
        "internal",
        "internal",
        "system",
        usage,
        suppress_progress_notifications=True,
    )

    assert store.get_queue_job(queued["job_id"])["suppress_progress_notifications"] == 1
    store.close()


def test_legacy_cache_refresh_reuses_terminal_queue_job(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.upsert_paper("2604.12946", "done", doc_url="https://old-doc")
    first_usage = store.add_usage_event(
        "evt-1", "om-1", "oc", "p2p", "ou-1", "paper", "2604.12946", "url", status="queued"
    )
    first = store.enqueue_job(
        "paper", "2604.12946", "url", "evt-1", "om-1", "oc", "p2p", "ou-1", first_usage
    )
    store.claim_next_queue_job(worker_id="worker-a")
    store.complete_queue_job(first["job_id"], "https://old-doc", "Old title")
    store.upsert_paper("2604.12946", "legacy", doc_url="https://old-doc", doc_token="old-doc")

    second_usage = store.add_usage_event(
        "evt-2", "om-2", "oc", "p2p", "ou-2", "paper", "2604.12946", "url", status="queued"
    )
    refreshed = store.enqueue_job(
        "paper", "2604.12946", "url", "evt-2", "om-2", "oc", "p2p", "ou-2", second_usage
    )

    assert refreshed["created"] is True
    assert refreshed["job_id"] == first["job_id"]
    assert len(store.list_queue_jobs()) == 1
    job = store.get_queue_job(first["job_id"])
    assert job["status"] == "queued"
    assert job["workflow_state"] == "queued"
    assert job["attempts"] == 0
    assert job["doc_url"] == ""
    watchers = [
        dict(row) for row in store.conn.execute(
            "select * from job_watchers where job_id=? order by id",
            (first["job_id"],),
        ).fetchall()
    ]
    assert [watcher["notified"] for watcher in watchers] == [1, 0]
    assert any(event["event_type"] == "cache_refresh" for event in store.list_job_events(first["job_id"]))
    store.close()


def test_recent_job_duration_uses_recent_success_median(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    for index, minutes in enumerate((5, 10, 15), start=1):
        store.conn.execute(
            """
            insert into queue_jobs (
                dedupe_key, source_kind, source_id, source_url, status, started_at, finished_at
            ) values (?, 'paper', ?, 'url', 'done', datetime('now', ?), current_timestamp)
            """,
            (f"paper:{index}", str(index), f"-{minutes} minutes"),
        )
    store.conn.commit()

    assert 599 <= store.recent_job_duration_seconds("paper") <= 601
    assert store.recent_job_duration_seconds("article") == 300
    store.close()


def test_store_validates_workflow_transitions_and_records_versions(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="worker-a")

    first = store.list_queue_jobs()[0]
    assert first["state_version"] == 1
    store.transition_queue_job(queued["job_id"], WorkflowEvent.FETCH_STARTED, "download")
    second = store.list_queue_jobs()[0]
    assert second["workflow_state"] == WorkflowState.FETCHING.value
    assert second["stage"] == "downloading"
    assert second["state_version"] == 2

    store.transition_queue_job(queued["job_id"], WorkflowEvent.SOURCE_READY)
    store.transition_queue_job(queued["job_id"], WorkflowEvent.GENERATION_STARTED)
    generating = store.list_queue_jobs()[0]
    assert generating["workflow_state"] == WorkflowState.GENERATING.value
    assert generating["stage"] == "reading"

    try:
        store.transition_queue_job(queued["job_id"], WorkflowEvent.QUALITY_PASSED)
        raise AssertionError("invalid workflow transition was accepted")
    except ValueError:
        pass

    unchanged = store.list_queue_jobs()[0]
    assert unchanged["workflow_state"] == WorkflowState.GENERATING.value
    assert unchanged["state_version"] == 4
    assert any(item["event_type"] == "transition" for item in store.list_job_events(queued["job_id"]))
    store.close()


def test_admin_record_queries_filter_by_time_and_watcher_user(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    first_usage = store.add_usage_event("evt-1", "om-1", "oc", "p2p", "ou_1", "paper", "p1", "url-1", status="queued")
    second_usage = store.add_usage_event("evt-2", "om-2", "oc", "p2p", "ou_2", "paper", "p2", "url-2", status="queued")
    first = store.enqueue_job("paper", "p1", "url-1", "evt-1", "om-1", "oc", "p2p", "ou_1", first_usage)
    second = store.enqueue_job("paper", "p2", "url-2", "evt-2", "om-2", "oc", "p2p", "ou_2", second_usage)
    store.add_job_event(first["job_id"], "first-event", "one")
    store.add_job_event(second["job_id"], "second-event", "two")
    store.add_feedback("evt-1", "om-1", "oc", "p2p", "ou_1", "反馈：one")
    store.add_feedback("evt-2", "om-2", "oc", "p2p", "ou_2", "反馈：two")
    store.add_review_issue("paper", "p1", "format", "low", "one")
    store.add_review_issue("paper", "p2", "format", "low", "two")

    assert [row["sender_id"] for row in store.list_usage_events(sender_id="ou_1")] == ["ou_1"]
    assert {row["sender_id"] for row in store.list_usage_users()} == {"ou_1", "ou_2"}
    assert [row["source_id"] for row in store.list_queue_jobs(sender_id="ou_1")] == ["p1"]
    assert {row["event_type"] for row in store.list_job_events(sender_id="ou_2")} == {"enqueue", "second-event"}
    assert [row["sender_id"] for row in store.list_feedback(sender_id="ou_1")] == ["ou_1"]
    assert [row["source_id"] for row in store.list_review_issues(sender_id="ou_2")] == ["p2"]
    assert store.list_usage_events(since="2999-01-01 00:00:00") == []
    assert store.list_queue_jobs(since="2999-01-01 00:00:00") == []
    assert store.list_job_events(since="2999-01-01 00:00:00") == []
    store.close()


def test_store_persists_published_document_checkpoint(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="worker-a")
    for event in (
        WorkflowEvent.FETCH_STARTED,
        WorkflowEvent.SOURCE_READY,
        WorkflowEvent.GENERATION_STARTED,
        WorkflowEvent.DRAFT_READY,
        WorkflowEvent.REVIEW_COMPLETED,
        WorkflowEvent.QUALITY_PASSED,
    ):
        store.transition_queue_job(queued["job_id"], event)

    store.transition_queue_job(queued["job_id"], WorkflowEvent.PUBLISH_SUCCEEDED, "https://tenant.feishu.cn/docx/checkpoint")

    row = store.list_queue_jobs()[0]
    assert row["workflow_state"] == WorkflowState.POST_PUBLISH_CHECKING.value
    assert row["doc_url"] == "https://tenant.feishu.cn/docx/checkpoint"
    checkpoint = PublishedCheckpoint.from_json(row["checkpoint_json"])
    assert checkpoint is not None
    assert checkpoint.doc_url == row["doc_url"]
    assert row["last_event"] == WorkflowEvent.PUBLISH_SUCCEEDED.value
    store.close()




def test_store_queue_job_heartbeat_and_stale_recovery(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "https://arxiv.org/abs/2604.12946", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "https://arxiv.org/abs/2604.12946", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    job = store.claim_next_queue_job(worker_id="worker-a")
    assert job["worker_id"] == "worker-a"
    store.update_queue_job_stage(queued["job_id"], "reading")
    rows = store.list_queue_jobs()
    assert rows[0]["status"] == "running"
    assert rows[0]["stage"] == "reading"
    assert rows[0]["heartbeat_at"]

    store.conn.execute("update queue_jobs set heartbeat_at = datetime('now', '-20 minutes') where id = ?", (queued["job_id"],))
    store.conn.commit()
    assert store.recover_stale_queue_jobs(10) == 1
    rows = store.list_queue_jobs()
    assert rows[0]["status"] == "queued"
    assert rows[0]["stage"] == "recovered"
    assert rows[0]["suppress_progress_notifications"] == 1
    assert any(event["event_type"] == "recover_stale" for event in store.list_job_events(queued["job_id"]))
    store.close()


def test_queue_position_includes_running_workers_before_queued_job(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    first_usage = store.add_usage_event("evt-1", "om-1", "oc", "p2p", "ou", "paper", "p1", "url-1", status="queued")
    second_usage = store.add_usage_event("evt-2", "om-2", "oc", "p2p", "ou", "paper", "p2", "url-2", status="queued")
    first = store.enqueue_job("paper", "p1", "url-1", "evt-1", "om-1", "oc", "p2p", "ou", first_usage)
    second = store.enqueue_job("paper", "p2", "url-2", "evt-2", "om-2", "oc", "p2p", "ou", second_usage)
    store.claim_next_queue_job(worker_id="worker-a")

    assert store.queue_position(second["job_id"]) == 2
    assert store.queue_position(first["job_id"]) == 0
    store.close()



def test_store_recovers_dead_worker_jobs_without_waiting_for_stale_timeout(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2605.19974", "https://arxiv.org/abs/2605.19974", status="queued")
    queued = store.enqueue_job("paper", "2605.19974", "https://arxiv.org/abs/2605.19974", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="host-a:12345:abc:maxread-worker-1")

    recovered = store.recover_dead_worker_queue_jobs("host-a", lambda pid: False)

    assert recovered == 1
    rows = store.list_queue_jobs()
    assert rows[0]["id"] == queued["job_id"]
    assert rows[0]["status"] == "queued"
    assert rows[0]["stage"] == "recovered"
    assert rows[0]["suppress_progress_notifications"] == 1
    assert any(event["event_type"] == "recover_dead_worker" for event in store.list_job_events(queued["job_id"]))
    store.close()


def test_store_does_not_recover_live_worker_jobs(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2605.19974", "https://arxiv.org/abs/2605.19974", status="queued")
    store.enqueue_job("paper", "2605.19974", "https://arxiv.org/abs/2605.19974", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="host-a:12345:abc:maxread-worker-1")

    recovered = store.recover_dead_worker_queue_jobs("host-a", lambda pid: True)

    assert recovered == 0
    assert store.list_queue_jobs()[0]["status"] == "running"
    store.close()

def test_store_retry_queue_job(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "https://arxiv.org/abs/2604.12946", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "https://arxiv.org/abs/2604.12946", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.fail_queue_job(queued["job_id"], "boom")
    assert store.retry_queue_job(queued["job_id"]) is True
    rows = store.list_queue_jobs()
    assert rows[0]["status"] == "queued"
    assert rows[0]["error"] == ""
    assert rows[0]["suppress_progress_notifications"] == 0
    usage = store.list_usage_events(limit=1)[0]
    assert usage["status"] == "queued"
    assert usage["error"] == ""
    assert store.get_job_watchers(queued["job_id"])[0]["notified"] == 0
    assert any(event["event_type"] == "retry" for event in store.list_job_events(queued["job_id"]))
    store.close()


def test_find_retryable_jobs_uses_topic_source_but_selects_latest_attempt(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage1 = store.add_usage_event("evt1", "om-root", "oc", "group", "ou", "paper", "2410.06205", "url", status="queued")
    first = store.enqueue_job("paper", "2410.06205", "url", "evt1", "om-root", "oc", "group", "ou", usage1)
    store.fail_queue_job(first["job_id"], "first failure")
    usage2 = store.add_usage_event("evt2", "om-retry", "oc", "group", "ou", "paper", "2410.06205", "url", status="queued")
    second = store.enqueue_job("paper", "2410.06205", "url", "evt2", "om-retry", "oc", "group", "ou", usage2)
    store.fail_queue_job(second["job_id"], "second failure")

    jobs = store.find_retryable_queue_jobs("oc", "ou", message_ids=("om-root",))

    assert [job["id"] for job in jobs] == [second["job_id"]]
    store.close()


def test_manual_retry_rebuilds_after_published_quality_failure(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.conn.execute(
        "update queue_jobs set checkpoint_json = ?, doc_url = ?, rebuild_pipeline = 0 where id = ?",
        ('{"doc_url":"https://old-doc"}', "https://old-doc", queued["job_id"]),
    )
    store.conn.commit()
    store.fail_queue_job(queued["job_id"], "visual quality failed", doc_url="https://old-doc")

    assert store.retry_queue_job(queued["job_id"], reason="formula compiler fixed") is True
    row = store.list_queue_jobs()[0]
    assert row["status"] == "queued"
    assert row["checkpoint_json"] == ""
    assert row["rebuild_pipeline"] == 1
    assert "retry_mode=rebuild" in store.list_job_events(queued["job_id"])[0]["detail"]
    store.close()


def test_automatic_retry_can_resume_published_checkpoint(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.conn.execute(
        "update queue_jobs set checkpoint_json = ?, doc_url = ? where id = ?",
        ('{"doc_url":"https://old-doc"}', "https://old-doc", queued["job_id"]),
    )
    store.conn.commit()
    store.fail_queue_job(queued["job_id"], "temporary browser timeout", doc_url="https://old-doc")

    assert store.retry_queue_job(queued["job_id"], event_type="auto_retry", rebuild_pipeline=False) is True
    row = store.list_queue_jobs()[0]
    assert row["checkpoint_json"] == '{"doc_url":"https://old-doc"}'
    assert row["rebuild_pipeline"] == 0
    assert "retry_mode=resume" in store.list_job_events(queued["job_id"])[0]["detail"]
    store.close()


def test_retry_persists_previous_error_as_generation_feedback(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "msg", "chat", "group", "sender", "paper", "2108.12409", "url")
    queued = store.enqueue_job("paper", "2108.12409", "url", "evt", "msg", "chat", "group", "sender", usage_id)
    store.fail_queue_job(queued["job_id"], "raw caption command: \\mathrm")

    assert store.retry_queue_job(queued["job_id"], rebuild_pipeline=True) is True
    row = store.get_queue_job(queued["job_id"])
    assert row["error"] == ""
    assert row["retry_feedback"] == "raw caption command: \\mathrm"
    store.close()


def test_store_requeues_operator_recovered_cancelled_job_silently(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event(
        "evt", "om_1", "oc", "group", "ou", "paper", "2604.12946", "url", status="queued"
    )
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om_1", "oc", "group", "ou", usage_id)
    store.transition_queue_job(queued["job_id"], WorkflowEvent.CANCEL, "operator stopped old retry storm")

    assert store.requeue_interrupted_job(queued["job_id"], "服务中断恢复：旧任务未完成") is True
    row = store.list_queue_jobs()[0]
    assert row["status"] == "queued"
    assert row["workflow_state"] == WorkflowState.QUEUED.value
    assert row["suppress_progress_notifications"] == 1
    assert row["recovery_reason"] == "服务中断恢复：旧任务未完成"
    assert store.get_job_watchers(queued["job_id"])[0]["notified"] == 0
    store.close()


def test_store_does_not_overwrite_terminal_state_with_incompatible_result(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="worker-a")
    store.fail_queue_job(queued["job_id"], "quality failed")

    try:
        store.complete_queue_job(queued["job_id"], "https://wrong-doc", "Wrong")
        raise AssertionError("failed job was completed")
    except ValueError:
        pass

    row = store.list_queue_jobs()[0]
    assert row["status"] == "failed"
    assert row["workflow_state"] == WorkflowState.FAILED.value
    assert row["doc_url"] == ""
    store.close()


def test_store_ignores_failure_after_completed_job(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="worker-a")
    store.complete_queue_job(queued["job_id"], "https://doc", "Title")
    store.fail_queue_job(queued["job_id"], "late worker error")

    row = store.list_queue_jobs()[0]
    assert row["status"] == "done"
    assert row["workflow_state"] == WorkflowState.COMPLETED.value
    assert row["doc_url"] == "https://doc"
    assert any(event["event_type"] == "ignored_failure_after_done" for event in store.list_job_events(queued["job_id"]))
    store.close()


def test_store_does_not_retry_a_live_running_job(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="worker-a")

    assert store.retry_queue_job(queued["job_id"]) is False
    row = store.list_queue_jobs()[0]
    assert row["status"] == "running"
    assert row["workflow_state"] == WorkflowState.CLAIMED.value
    store.close()


def test_store_recovers_a_stale_job_only_once(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="worker-a")
    store.conn.execute("update queue_jobs set heartbeat_at = datetime('now', '-20 minutes') where id = ?", (queued["job_id"],))
    store.conn.commit()

    assert store.recover_stale_queue_jobs(10) == 1
    assert store.recover_stale_queue_jobs(10) == 0
    row = store.list_queue_jobs()[0]
    assert row["status"] == "queued"
    assert row["workflow_state"] == WorkflowState.QUEUED.value
    store.close()


def test_store_caps_infrastructure_recovery_attempts_without_notifying(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="host-a:12345:worker")

    assert store.recover_dead_worker_queue_jobs("host-a", lambda _pid: False, max_recovery_attempts=0) == 1
    row = store.list_queue_jobs()[0]
    assert row["status"] == "failed"
    assert row["stage"] == "recovery_exhausted"
    assert row["suppress_progress_notifications"] == 1
    assert store.conn.execute("select notified from job_watchers where job_id = ?", (queued["job_id"],)).fetchone()[0] == 1
    assert "自动恢复次数已达上限" in row["error"]
    store.close()


def test_store_rejects_late_worker_mutations_after_lease_recovery(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou_1", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.claim_next_queue_job(worker_id="worker-a")
    store.conn.execute("update queue_jobs set heartbeat_at = datetime('now', '-20 minutes') where id = ?", (queued["job_id"],))
    store.conn.commit()
    assert store.recover_stale_queue_jobs(10) == 1
    store.claim_next_queue_job(worker_id="worker-b")

    assert store.heartbeat_queue_job(queued["job_id"], "worker-a") is False
    assert store.complete_queue_job(queued["job_id"], "https://stale-doc", worker_id="worker-a") is False
    assert store.fail_queue_job(queued["job_id"], "stale failure", worker_id="worker-a") is False
    try:
        store.transition_queue_job(
            queued["job_id"], WorkflowEvent.FETCH_STARTED, expected_worker_id="worker-a"
        )
        raise AssertionError("late worker transition was accepted")
    except QueueLeaseLostError:
        pass

    row = store.list_queue_jobs()[0]
    assert row["status"] == "running"
    assert row["worker_id"] == "worker-b"
    store.close()


def test_store_tracks_review_issues(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    issue_id = store.add_review_issue("paper", "2210.10340", "tex_macro", "medium", "清理 \formername")
    assert issue_id == 1
    rows = store.list_review_issues(source_kind="paper", source_id="2210.10340")
    assert rows[0]["category"] == "tex_macro"
    assert rows[0]["severity"] == "medium"
    stats = store.review_issue_stats()
    assert stats[0]["category"] == "tex_macro"
    assert stats[0]["count"] == 1
    store.close()


def test_store_syncs_real_paper_title_across_placeholder_records(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    paper_id = "2608.24646"
    placeholder = f"arXiv {paper_id}"
    real_title = "On-Policy Self-Distillation in Diffusion Models"
    store.upsert_paper(paper_id, "legacy", title=placeholder)
    usage_id = store.add_usage_event(
        "evt", "om", "oc", "p2p", "ou_1", "paper", paper_id, "url", title=placeholder, status="done"
    )
    queued = store.enqueue_job("paper", paper_id, "url", "evt", "om", "oc", "p2p", "ou_1", usage_id)
    store.conn.execute("update queue_jobs set title = ?, status = 'done' where id = ?", (placeholder, queued["job_id"]))
    store.conn.commit()

    changed = store.sync_paper_title(paper_id, real_title)

    assert changed == 3
    assert store.get_paper(paper_id).title == real_title
    assert store.conn.execute("select title from queue_jobs where id = ?", (queued["job_id"],)).fetchone()[0] == real_title
    assert store.conn.execute("select title from usage_events where id = ?", (usage_id,)).fetchone()[0] == real_title
    store.close()


def test_placeholder_upsert_cannot_overwrite_a_real_paper_title(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    paper_id = "2608.24646"
    real_title = "On-Policy Self-Distillation in Diffusion Models"
    store.upsert_paper(paper_id, "done", title=real_title)

    store.upsert_paper(paper_id, "legacy", title=f"arXiv ID: {paper_id}")

    assert store.get_paper(paper_id).title == real_title
    store.close()
