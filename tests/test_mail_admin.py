from __future__ import annotations

import sqlite3
import json
from pathlib import Path

from maxread import mail_admin


def _fixture(tmp_path: Path):
    root = tmp_path / "mail"
    accounts = root / "data/accounts"
    accounts.mkdir(parents=True)
    db = accounts / "shared.sqlite3"
    (accounts / "zip-lab.env").write_text(
        "IMAP_USERNAME=zip.lab@outlook.com\n"
        f"MAIL_DB_PATH={db}\n"
        "MAIL_SCAN_LIMIT=100\n"
        "RECRUITING_SCAN_INTERVAL_DAYS=0.5\n"
        "RECRUITING_REPORT_INTERVAL_HOURS=168\n"
        "IMAP_PASSWORD=secret-must-not-leak\n",
        encoding="utf-8",
    )
    (accounts / "bohan-zhuang.env").write_text(
        "IMAP_USERNAME=bohan.zhuang@zju.edu.cn\n"
        f"MAIL_DB_PATH={db}\n"
        "MAIL_SCAN_LIMIT=5000\n",
        encoding="utf-8",
    )
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            create table messages(mailbox text, scanned_at text, received_at text);
            create table recruiting_runs(run_id text, started_at text, finished_at text, status text,
              scanned_messages integer, new_threads integer, updated_threads integer, failed_threads integer, error text);
            create table recruiting_threads(status text, last_error text, updated_at text);
            """
        )
        connection.executemany(
            "insert into messages values(?,?,?)",
            [
                ("zip.lab@outlook.com::INBOX", "2026-08-30T01:00:00+00:00", "2026-08-30T08:00:00+08:00"),
                ("bohan.zhuang@zju.edu.cn::INBOX", "2026-08-30T02:00:00+00:00", "2026-08-30T09:00:00+08:00"),
            ],
        )
        connection.execute(
            "insert into recruiting_runs values(?,?,?,?,?,?,?,?,?)",
            ("run", "2026-08-30T02:00:00+00:00", "2026-08-30T02:02:00+00:00", "completed", 2, 0, 2, 1, ""),
        )
        connection.executemany(
            "insert into recruiting_threads values(?,?,?)",
            [("active", "", "2026-08-30"), ("extract_failed", "TLS failed", "2026-08-30")],
        )
    return root, db


def test_mail_status_reports_process_and_business_health_without_secrets(tmp_path, monkeypatch):
    root, _db = _fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.setattr(mail_admin, "_systemd_show", lambda unit: {"ActiveState": "active", "MainPID": "42"})

    status = mail_admin.mail_admin_status()

    assert status["business_state"] == "degraded"
    assert {item["address"] for item in status["accounts"]} == {
        "zip.lab@outlook.com",
        "bohan.zhuang@zju.edu.cn",
    }
    assert status["config"]["scan_interval_minutes"] == 720
    assert status["runs"][0]["failed_threads"] == 1
    assert "secret-must-not-leak" not in str(status)


def test_mail_status_treats_active_manual_scan_as_expected_takeover(tmp_path, monkeypatch):
    root, _db = _fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.setattr(mail_admin, "_systemd_show", lambda unit: {"ActiveState": "inactive", "MainPID": "0"})
    monkeypatch.setattr(mail_admin, "_control_status", lambda _root: {"active": True, "account": "all"})

    status = mail_admin.mail_admin_status()

    assert status["business_state"] == "scanning"
    assert status["control"] == {"active": True, "account": "all"}


def test_mail_config_update_is_atomic_and_restarts_units(tmp_path, monkeypatch):
    root, _db = _fixture(tmp_path)
    timer = tmp_path / "systemd/interval.conf"
    commands = []
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.setenv("MAXREAD_MAIL_REPORT_TIMER_DROPIN", str(timer))
    monkeypatch.setattr(mail_admin, "_run", lambda argv: commands.append(argv) or "")

    result = mail_admin.update_mail_admin_config(30, 24)

    env = (root / "data/accounts/zip-lab.env").read_text(encoding="utf-8")
    assert "RECRUITING_SCAN_INTERVAL_DAYS=0.02083333" in env
    assert "RECRUITING_REPORT_INTERVAL_HOURS=24" in env
    assert "OnUnitActiveSec=24h" in timer.read_text(encoding="utf-8")
    assert ["systemctl", "restart", "recruiting-pipeline.service"] in commands
    assert result["scan_interval_minutes"] == 30


def test_manual_scan_accepts_only_configured_accounts_and_records_unit(tmp_path, monkeypatch):
    root, db = _fixture(tmp_path)
    script = root / "bin/recruiting-control-scan"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    commands = []
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.setattr(mail_admin, "_mail_db_path", lambda _root: db)
    monkeypatch.setattr(mail_admin, "_systemd_show", lambda _unit: {"ActiveState": "inactive"})
    monkeypatch.setattr(mail_admin, "_run", lambda argv: commands.append(argv) or "Running as unit")

    result = mail_admin.trigger_mail_scan("bohan-zhuang")

    assert result["account"] == "bohan-zhuang"
    assert str(script) in commands[0]
    assert any(item.startswith("--setenv=HOME=") for item in commands[0])
    assert (root / "data/mail-control.json").exists()
    try:
        mail_admin.trigger_mail_scan("not-configured")
    except ValueError as exc:
        assert "未知邮箱" in str(exc)
    else:
        raise AssertionError("unknown account should fail")


def test_mail_admin_page_uses_reverse_proxy_relative_api_paths():
    html = mail_admin.MAIL_ADMIN_HTML

    assert "api('api/admin/mail/status')" in html
    assert "api('api/admin/mail/scan'" in html
    assert "api('api/admin/mail/config'" in html
    assert "api('/api/admin/mail" not in html
    assert 'href="admin?next=mail"' in html
    assert "登录成功后会自动返回本页" in html
    assert "手动扫描中" in html
    assert "手动扫描接管" in html
    assert "结束后自动恢复常驻任务" in html


def test_systemd_cst_timestamp_is_exposed_as_shanghai_iso():
    assert mail_admin._systemd_time_iso("Mon 2026-09-07 07:00:00 CST") == "2026-09-07T07:00:00+08:00"


def test_remote_mode_proxies_status_scan_and_config(monkeypatch):
    requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout=0):
        requests.append((request.full_url, request.method, request.headers, request.data, timeout))
        return Response({"ok": True, "remote_execution": True})

    monkeypatch.setenv("MAXREAD_MAIL_REMOTE_URL", "http://127.0.0.1:18766")
    monkeypatch.setenv("MAXREAD_MAIL_REMOTE_TOKEN", "secret-token")
    monkeypatch.setattr(mail_admin.urllib.request, "urlopen", fake_urlopen)

    assert mail_admin.mail_admin_status()["remote_execution"] is True
    assert mail_admin.trigger_mail_scan("all")["ok"] is True
    assert mail_admin.update_mail_admin_config(60, 168)["ok"] is True
    assert [item[0] for item in requests] == [
        "http://127.0.0.1:18766/status",
        "http://127.0.0.1:18766/scan",
        "http://127.0.0.1:18766/config",
    ]
    assert all(item[2]["Authorization"] == "Bearer secret-token" for item in requests)


def test_user_systemd_prefix_is_used_on_compute_worker(monkeypatch):
    monkeypatch.setenv("MAXREAD_MAIL_SYSTEMD_USER", "1")
    assert mail_admin._systemctl("restart", "recruiting-pipeline.service") == [
        "systemctl", "--user", "restart", "recruiting-pipeline.service",
    ]


def test_mail_admin_page_keeps_base_links_behind_authenticated_status_api():
    html = mail_admin.MAIL_ADMIN_HTML
    assert "table-links" in html
    assert "S4v4bdOCuaWvAQs90vCcek4anHh" not in html


def _record_fixture(tmp_path: Path):
    root = tmp_path / "mail-records"
    accounts = root / "data/accounts"
    accounts.mkdir(parents=True)
    db = accounts / "mail.sqlite3"
    (accounts / "zip-lab.env").write_text(f"MAIL_DB_PATH={db}\nRECRUITING_BASE_TOKEN=base\nRECRUITING_TABLE_ID=table\n", encoding="utf-8")
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            create table recruiting_threads(
              thread_key text primary key,candidate_address text,normalized_subject text,fields_json text,
              base_record_id text,doc_url text,latest_time text,last_incoming_time text,last_outgoing_time text,
              status text,screening_status text,interview_assigned integer,interview_result text,
              last_error text,updated_at text
            )
            """
        )
        candidate = {
            "name": "张三", "mail_type": "candidate", "school": "浙江大学", "education_stage": "本科",
            "current_grade": "大三", "major": "计算机", "academic_display": "4.5/5.0 · Top 5%",
            "rank": "Top 5%", "rank_evidence": "专业排名前 5%", "projects": ["World Model"],
            "purpose_summary": "申请目的：世界模型研究", "source_accounts": ["ZIP Lab"], "is_985": "是", "is_c9": "是",
        }
        other = {"name": "系统通知", "mail_type": "other", "projects": ["unknown"], "purpose_summary": "安全通知"}
        connection.execute(
            "insert into recruiting_threads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a" * 32, "candidate@example.com", "申请", json.dumps(candidate, ensure_ascii=False), "rec1", "https://doc", "2026-09-01T10:00:00+08:00", "", "2026-09-01T11:00:00+08:00", "active", "未筛选", 0, "未开始", "", "v1"),
        )
        connection.execute(
            "insert into recruiting_threads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("b" * 32, "notice@example.com", "通知", json.dumps(other, ensure_ascii=False), "rec2", "", "2026-08-31T10:00:00+08:00", "", "", "active", "未筛选", 0, "未开始", "", "v2"),
        )
    return root, db


def test_mail_record_query_filters_and_paginates(tmp_path, monkeypatch):
    root, _db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))

    result = mail_admin.mail_admin_records("mail_type=candidate&q=浙江&project=World+Model&days=0&limit=10")

    assert result["total"] == 1
    assert result["items"][0]["name"] == "张三"
    assert result["items"][0]["has_replied"] is True
    assert result["filters"]["screening_statuses"] == ["未筛选", "面试资格", "面试通过", "未通过", "实习生"]


def test_mail_record_update_writes_base_then_sqlite_and_audit(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    calls = []
    monkeypatch.setattr(mail_admin, "_update_base_workflow", lambda record_id, state: calls.append((record_id, dict(state))))

    result = mail_admin.update_mail_admin_record(
        "a" * 32,
        {"screening_status": "面试资格", "interview_assigned": True, "interview_result": "通过"},
        "v1",
    )

    assert result["state"] == {"screening_status": "面试资格", "interview_assigned": True, "interview_result": "通过"}
    assert calls == [("rec1", result["state"])]
    with sqlite3.connect(db) as connection:
        assert connection.execute("select screening_status,interview_assigned,interview_result from recruiting_threads where thread_key=?", ("a" * 32,)).fetchone() == ("面试资格", 1, "通过")
        assert connection.execute("select count(*) from recruiting_admin_actions").fetchone()[0] == 1


def test_mail_record_base_failure_leaves_sqlite_unchanged(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.setattr(mail_admin, "_update_base_workflow", lambda *_args: (_ for _ in ()).throw(RuntimeError("base unavailable")))

    try:
        mail_admin.update_mail_admin_record("a" * 32, {"screening_status": "未通过"}, "v1")
    except RuntimeError as exc:
        assert "base unavailable" in str(exc)
    else:
        raise AssertionError("Base failure must reject the update")
    with sqlite3.connect(db) as connection:
        assert connection.execute("select screening_status from recruiting_threads where thread_key=?", ("a" * 32,)).fetchone()[0] == "未筛选"


def test_mail_admin_page_contains_candidate_workbench_controls():
    html = mail_admin.MAIL_ADMIN_HTML
    assert "邮件记录" in html
    assert "api/admin/mail/records" in html
    assert "api/admin/mail/record" in html
    assert "最近一周新增候选人" not in html  # links arrive only after authenticated API response


def test_mail_admin_page_uses_compact_master_detail_layout():
    html = mail_admin.MAIL_ADMIN_HTML

    assert 'data-view="candidates"' in html
    assert 'data-view="operations"' in html
    assert 'id="candidate-panel"' in html
    assert 'id="operations-panel"' in html
    assert 'class="record-table-wrap"' in html
    assert "max-height:calc(100dvh - 344px)" in html
    assert "recordState={items:[],offset:0,limit:20" in html
    assert "点击姓名查看完整材料" in html
    assert "<th>摘要</th>" not in html
    assert 'class="ops-details wide"' in html
    assert "height:100dvh" in html
    assert "margin:0 0 0 auto" in html
