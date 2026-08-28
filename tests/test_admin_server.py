import hashlib
import http.client
import json
import threading
from types import SimpleNamespace

import maxread.admin_server as admin_server
from maxread.admin_architecture import architecture_html, architecture_spec
from maxread.admin_server import AdminHandler, AdminServer, INDEX_HTML, _admin_summary, _attach_user_names, _limit, _record_filters
from maxread.db import Store
from maxread.workflow import transition


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


def test_admin_record_filters_default_to_three_days_and_accept_user():
    since, sender_id = _record_filters("sender_id=ou_1")
    assert since
    assert sender_id == "ou_1"
    assert _record_filters("days=0") == ("", "")


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


def test_architecture_spec_covers_states_and_scenarios_follow_real_transitions():
    spec = architecture_spec()
    state_ids = {item["id"] for item in spec["states"]}
    assert spec["metrics"]["states"] == len(state_ids)
    assert spec["metrics"]["repair_loops"] == 3
    assert spec["metrics"]["failure_modes"] == len(spec["failure_modes"])
    assert spec["metrics"]["quality_gates"] == 4
    assert [item["order"] for item in spec["quality_gates"]] == [1, 2, 3, 4]
    assert {item["handling"] for item in spec["failure_modes"]} <= {item["id"] for item in spec["handling_types"]}
    assert all(edge["label"] and edge["condition"] for edge in spec["transitions"])
    assert all(policy["label"] and policy["condition"] and policy["sources"] for policy in spec["policies"])

    policies = {item["event"]: item for item in spec["policies"]}
    assert "queued" not in policies["recover"]["sources"]
    assert set(policies["retry"]["sources"]) == {"needs_source", "generation_incomplete", "quality_failed", "failed"}

    for scenario in spec["scenarios"]:
        assert len(scenario["events"]) == len(scenario["states"]) - 1
        assert set(scenario["states"]) <= state_ids
        for source, event, target in zip(scenario["states"], scenario["events"], scenario["states"][1:]):
            assert transition(source, event).to_state.value == target


def test_architecture_html_is_self_contained_and_uses_workflow_api():
    html = architecture_html()
    assert "MaxRead Pipeline Architecture" in html
    assert "fetch('api/workflow-spec'" in html
    assert 'class="state-graph"' in html
    assert "selectedTransitionKey" in html
    assert "renderNextHopSummary" in html
    assert "shortCondition" in html
    assert "edge-condition-svg" in html
    assert "spec.compact_graph" in html
    assert "服务端状态规范仍是旧版本" in html
    assert 'id="failure-list"' not in html
    assert 'id="quality-gate-list"' not in html
    assert "renderQualityGates();" not in html
    assert "renderFailures();" not in html
    assert 'id="input-assembly"' in html
    assert "主生成不接收图片二进制" in html
    assert "build_final_user_prompt()" in html
    assert "https://" not in html


def test_admin_html_recovers_from_transient_api_failure():
    assert "Promise.all([loadUsers(), loadAdminStatus()]).finally(refreshAll)" in INDEX_HTML
    assert "数据加载失败" in INDEX_HTML
    assert "AbortController" in INDEX_HTML


def test_admin_mutations_require_authenticated_server_side_session(tmp_path):
    password = "test-admin-password"
    settings = SimpleNamespace(
        db_path=tmp_path / "maxread.sqlite3",
        admin_password_hash=hashlib.sha256(password.encode("utf-8")).hexdigest(),
        lark_cli="lark-cli",
    )
    server = AdminServer(("127.0.0.1", 0), AdminHandler, settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)

    def request(method, path, payload=None, cookie=""):
        headers = {"content-type": "application/json"}
        if cookie:
            headers["cookie"] = cookie
        connection.request(method, path, body=json.dumps(payload or {}), headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        return response, body

    try:
        response, body = request("GET", "/api/admin/status")
        assert response.status == 200
        assert body == {"authenticated": False}

        response, body = request(
            "POST",
            "/api/service-status",
            {"mode": "outage", "reason": "test"},
        )
        assert response.status == 401
        assert body["error"] == "需要管理员登录"

        response, body = request("POST", "/api/admin/login", {"password": password})
        assert response.status == 200
        assert body["authenticated"] is True
        cookie = response.getheader("set-cookie").split(";", 1)[0]
        assert cookie.startswith("maxread_admin_session=")
        assert password not in response.getheader("set-cookie")

        response, body = request(
            "POST",
            "/api/service-status",
            {"mode": "outage", "reason": "test", "updated_by": "admin"},
            cookie,
        )
        assert response.status == 200
        assert body["mode"] == "outage"

        response, body = request("GET", "/api/admin/status", cookie=cookie)
        assert response.status == 200
        assert body == {"authenticated": True}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_admin_html_defaults_to_read_only_and_has_explicit_login():
    assert "管理员登录" in INDEX_HTML
    assert "adminAuthenticated: false" in INDEX_HTML
    assert "state.adminAuthenticated ? editor : readonly" in INDEX_HTML
    assert "000000" not in INDEX_HTML
    assert "worker 心跳正常；任务失败或租约失效后方可重试" in INDEX_HTML
    assert "查看恢复记录" in INDEX_HTML
    assert "任务已在队列中，无需重复提交" in INDEX_HTML
