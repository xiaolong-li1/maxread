from maxread.db import Store


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
    assert store.queue_position(first["job_id"]) == 0
    watchers = store.get_job_watchers(first["job_id"])
    assert len(watchers) == 2
    store.complete_queue_job(first["job_id"], "https://doc", "Title")
    rows = store.list_queue_jobs()
    assert rows[0]["status"] == "done"
    assert rows[0]["doc_url"] == "https://doc"
    events = store.list_job_events(first["job_id"])
    assert any(event["event_type"] == "enqueue" for event in events)
    assert any(event["event_type"] == "claim" for event in events)
    assert any(event["event_type"] == "done" for event in events)
    stats = store.queue_stats()
    assert stats["done"] == 1
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
