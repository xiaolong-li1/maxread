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
    watchers = store.get_job_watchers(first["job_id"])
    assert len(watchers) == 2
    store.complete_queue_job(first["job_id"], "https://doc", "Title")
    rows = store.list_queue_jobs()
    assert rows[0]["status"] == "done"
    assert rows[0]["workflow_state"] == WorkflowState.COMPLETED.value
    assert rows[0]["doc_url"] == "https://doc"
    events = store.list_job_events(first["job_id"])
    assert any(event["event_type"] == "enqueue" for event in events)
    assert any(event["event_type"] == "claim" for event in events)
    assert any(event["event_type"] == "done" for event in events)
    stats = store.queue_stats()
    assert stats["done"] == 1
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
    assert second["state_version"] == 2

    try:
        store.transition_queue_job(queued["job_id"], WorkflowEvent.QUALITY_PASSED)
        raise AssertionError("invalid workflow transition was accepted")
    except ValueError:
        pass

    unchanged = store.list_queue_jobs()[0]
    assert unchanged["workflow_state"] == WorkflowState.FETCHING.value
    assert unchanged["state_version"] == 2
    assert any(item["event_type"] == "transition" for item in store.list_job_events(queued["job_id"]))
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
    assert any(event["event_type"] == "recover_stale" for event in store.list_job_events(queued["job_id"]))
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
    assert store.get_job_watchers(queued["job_id"])[0]["notified"] == 0
    assert any(event["event_type"] == "retry" for event in store.list_job_events(queued["job_id"]))
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
