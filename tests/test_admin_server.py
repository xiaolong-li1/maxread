from types import SimpleNamespace

import maxread.admin_server as admin_server
from maxread.admin_server import _admin_summary, _attach_user_names, _limit
from maxread.db import Store


def test_admin_summary_uses_existing_records(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    feedback_id = store.add_feedback("evt", "om", "oc", "p2p", "ou", "反馈：图少")
    store.add_feedback("evt_hello", "om_hello", "oc", "p2p", "ou", "hello")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou", "paper", "2604.12946", "https://arxiv.org/abs/2604.12946", status="queued")
    store.update_usage_event(usage_id, "done", doc_url="https://doc", title="Paper")
    store.add_review_issue("paper", "2604.12946", "format", "low", "ok")

    summary = _admin_summary(store)

    assert summary["feedback"]["new"] == 1
    assert summary["usage"]["done"] == 1
    assert summary["docs_done"] == 1
    assert summary["active_users"] == 1
    assert summary["review_issues"] == 1
    assert store.update_feedback_status(feedback_id, "planned") is True
    assert store.list_feedback(status="planned")[0]["id"] == feedback_id
    store.close()


def test_admin_limit_is_bounded():
    assert _limit("limit=5000") == 300
    assert _limit("limit=0") == 1
    assert _limit("limit=bad") == 80


def test_attach_user_names_uses_contact_search():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='{"data":{"users":[{"open_id":"ou_1","localized_name":"李晓龙"}]}}',
        )

    original_run = admin_server.subprocess.run
    admin_server.subprocess.run = fake_run
    try:
        rows = _attach_user_names(SimpleNamespace(lark_cli='lark-cli'), [{'sender_id': 'ou_1', 'content': '反馈：图少'}])
    finally:
        admin_server.subprocess.run = original_run

    assert rows[0]['sender_name'] == '李晓龙'
    assert calls[0][0] == ['lark-cli', 'contact', '+search-user', '--as', 'user', '--user-ids', 'ou_1', '--format', 'json']


def test_attach_user_names_persists_contact_mapping(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout='{"data":{"users":[{"open_id":"ou_1","localized_name":"李晓龙"}]}}',
        )

    original_run = admin_server.subprocess.run
    admin_server.subprocess.run = fake_run
    store = Store(tmp_path / 'maxread.sqlite3')
    try:
        settings = SimpleNamespace(lark_cli='lark-cli')
        _attach_user_names(settings, [{'sender_id': 'ou_1'}], store)
        rows = _attach_user_names(settings, [{'sender_id': 'ou_1'}], store)
    finally:
        admin_server.subprocess.run = original_run
        store.close()

    assert rows[0]['sender_name'] == '李晓龙'
    assert calls == [['lark-cli', 'contact', '+search-user', '--as', 'user', '--user-ids', 'ou_1', '--format', 'json']]
