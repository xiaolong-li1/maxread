from threading import BoundedSemaphore

from maxread.db import Store

from maxread.job_queue import _LimitedLLM, _notify_watchers_progress, _queue_eta_text


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


class _ReactionFeishu:
    def __init__(self):
        self.reactions = []
        self.replies = []

    def set_progress_reaction(self, message_id, stage):
        self.reactions.append((message_id, stage))

    def reply_text(self, message_id, text, idempotency_key=None):
        self.replies.append((message_id, text))



def test_queue_eta_text_accounts_for_parallel_workers():
    assert _queue_eta_text(1, 5) == "队列第 1 位，并发槽位内，预计马上开始。"
    assert _queue_eta_text(5, 5) == "队列第 5 位，并发槽位内，预计马上开始。"
    assert _queue_eta_text(6, 5) == "队列第 6 位，约第 2 批开始，预计等待约 3 分钟。"


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
