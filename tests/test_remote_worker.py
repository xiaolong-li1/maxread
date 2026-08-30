import threading
from types import SimpleNamespace

from maxread.db import Store
from maxread.remote_worker import (
    RemotePaperWorker,
    coordinator_claim,
    coordinator_event,
    coordinator_finish,
    coordinator_heartbeat,
    coordinator_transition,
)
from maxread.workflow import PublishedCheckpoint, WorkflowEvent


def settings(tmp_path):
    return SimpleNamespace(
        lark_cli="lark-cli",
        feishu_as="bot",
        auto_retry_attempts=0,
        db_path=tmp_path / "maxread.sqlite3",
    )


def worker_settings(tmp_path):
    return SimpleNamespace(
        worker_name="5090",
        worker_coordinator_url="http://coordinator.invalid",
        worker_token="token",
        workdir=tmp_path,
        batch_llm_concurrency=10,
        batch_feishu_concurrency=1,
        queue_workers=2,
    )


def enqueue(store, kind, source_id):
    usage = store.add_usage_event(
        f"evt-{kind}", f"om-{kind}", "oc", "p2p", "ou", kind, source_id, "url", status="queued"
    )
    return store.enqueue_job(kind, source_id, "url", f"evt-{kind}", f"om-{kind}", "oc", "p2p", "ou", usage)


def test_remote_claim_only_takes_paper_jobs(tmp_path, monkeypatch):
    store = Store(tmp_path / "maxread.sqlite3")
    article = enqueue(store, "article", "article-1")
    paper = enqueue(store, "paper", "2604.12946")
    monkeypatch.setattr("maxread.remote_worker._notify_watchers_started", lambda *_args, **_kwargs: None)

    result = coordinator_claim(settings(tmp_path), store, {"worker_id": "remote:5090:1"})

    assert result["job"]["id"] == paper["job_id"]
    assert result["job"]["source_kind"] == "paper"
    assert store.get_queue_job(article["job_id"])["status"] == "queued"
    assert store.get_queue_job(paper["job_id"])["worker_id"] == "remote:5090:1"
    store.close()


def test_remote_worker_reports_transitions_heartbeat_and_completion(tmp_path, monkeypatch):
    store = Store(tmp_path / "maxread.sqlite3")
    queued = enqueue(store, "paper", "2604.12946")
    monkeypatch.setattr("maxread.remote_worker._notify_watchers_started", lambda *_args, **_kwargs: None)
    notifications = []
    monkeypatch.setattr("maxread.remote_worker._notify_watchers", lambda *_args, **_kwargs: notifications.append(1))
    worker = {"worker_id": "remote:5090:2"}
    coordinator_claim(settings(tmp_path), store, worker)

    assert coordinator_heartbeat(settings(tmp_path), store, {**worker, "job_id": queued["job_id"]})["ok"] is True
    transitions = (
        (WorkflowEvent.FETCH_STARTED, "fetch"),
        (WorkflowEvent.SOURCE_READY, "source"),
        (WorkflowEvent.GENERATION_STARTED, "generation"),
        (WorkflowEvent.GENERATION_CHECK_STARTED, "check"),
        (WorkflowEvent.DRAFT_READY, "draft"),
        (WorkflowEvent.REVIEW_COMPLETED, "review"),
        (WorkflowEvent.QUALITY_PASSED, "quality"),
        (
            WorkflowEvent.PUBLISH_SUCCEEDED,
            PublishedCheckpoint("https://tenant/doc", "T", 1, 1, 1).to_json(),
        ),
        (WorkflowEvent.VISUAL_QA_STARTED, "visual"),
    )
    for event, detail in transitions:
        assert coordinator_transition(
            settings(tmp_path),
            store,
            {**worker, "job_id": queued["job_id"], "event": event.value, "detail": detail},
        )["ok"] is True
    assert coordinator_event(
        settings(tmp_path),
        store,
        {**worker, "job_id": queued["job_id"], "event_type": "llm_call_finished", "detail": "{}"},
    )["ok"] is True
    deferred = coordinator_transition(
        settings(tmp_path),
        store,
        {**worker, "job_id": queued["job_id"], "event": WorkflowEvent.COMPLETE.value, "detail": "https://tenant/doc"},
    )
    assert deferred["deferred_to_finish"] is True
    assert store.get_queue_job(queued["job_id"])["status"] == "running"

    result = coordinator_finish(
        settings(tmp_path),
        store,
        {
            **worker,
            "job_id": queued["job_id"],
            "doc_url": "https://tenant/doc",
            "error": "",
            "paper": {
                "status": "done",
                "title": "Remote paper",
                "project_summary": "summary",
                "doc_url": "https://tenant/doc",
                "doc_token": "doc",
                "error": "",
            },
            "review_issues": [],
            "cleanup": {"files": 4, "bytes": 1024},
        },
    )

    assert result["status"] == "done"
    assert store.get_queue_job(queued["job_id"])["status"] == "done"
    assert store.get_paper("2604.12946").doc_url == "https://tenant/doc"
    assert notifications == [1]
    assert any(event["event_type"] == "remote_llm_call_finished" for event in store.list_job_events(queued["job_id"]))
    assert any(event["event_type"] == "remote_pipeline_complete" for event in store.list_job_events(queued["job_id"]))
    assert any(event["event_type"] == "remote_cache_cleanup" for event in store.list_job_events(queued["job_id"]))
    store.close()


def test_remote_event_rejects_lost_worker_lease(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    queued = enqueue(store, "paper", "2604.12946")
    store.claim_next_queue_job(worker_id="remote:5090:owner", source_kinds=("paper",))

    result = coordinator_event(
        settings(tmp_path),
        store,
        {"worker_id": "remote:5090:other", "job_id": queued["job_id"], "event_type": "x", "detail": "y"},
    )

    assert result == {"ok": False, "lease": "lost"}
    store.close()


def test_remote_worker_runs_slots_concurrently_with_isolated_stores(tmp_path, monkeypatch):
    worker = RemotePaperWorker(worker_settings(tmp_path))
    barrier = threading.Barrier(2)
    observed = []

    def claim_loop(worker_id, local_store):
        barrier.wait(timeout=2)
        local_store.upsert_paper(f"paper-{worker_id[-1]}", "queued")
        observed.append((worker_id, local_store.path))

    monkeypatch.setattr(worker, "_claim_loop", claim_loop)

    worker.run_forever()

    assert {worker_id.rsplit(":", 1)[-1] for worker_id, _path in observed} == {"1", "2"}
    paths = {path for _worker_id, path in observed}
    assert paths == {
        tmp_path / "remote-worker-db" / "slot-1.sqlite3",
        tmp_path / "remote-worker-db" / "slot-2.sqlite3",
    }
    for path in paths:
        check = Store(path)
        slot = path.stem.rsplit("-", 1)[-1]
        assert check.get_paper(f"paper-{slot}") is not None
        check.close()
