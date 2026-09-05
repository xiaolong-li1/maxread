from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest

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
    assert "api('api/admin/login'" in html
    assert "api('api/admin/logout'" in html
    assert "本设备登录状态保持 30 天" in html
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
    monkeypatch.setattr(mail_admin, "_rejection_feature_enabled", lambda: True)

    assert mail_admin.mail_admin_status()["remote_execution"] is True
    assert mail_admin.trigger_mail_scan("all")["ok"] is True
    assert mail_admin.update_mail_admin_config(60, 168)["ok"] is True
    assert mail_admin.mail_rejection_context("a" * 32)["ok"] is True
    assert mail_admin.save_mail_rejection_draft("a" * 32, "主题", "正文")["ok"] is True
    assert mail_admin.save_mail_rejection_template("主题", "正文")["ok"] is True
    assert mail_admin.create_mail_rejection_batch(["a" * 32, "b" * 32])["ok"] is True
    assert mail_admin.mail_rejection_batch(9)["ok"] is True
    assert mail_admin.queue_mail_rejection_batch_send(9, "发送 2 封拒信")["ok"] is True
    assert mail_admin.send_mail_rejection(7, "candidate@example.com")["ok"] is True
    share_token = "A" * 43
    assert mail_admin.create_mail_candidate_share(["a" * 32], "候选人", 7)["ok"] is True
    assert mail_admin.list_mail_candidate_shares(20)["ok"] is True
    assert mail_admin.mail_candidate_share(share_token)["ok"] is True
    assert mail_admin.revoke_mail_candidate_share(3)["ok"] is True
    assert mail_admin.reissue_mail_candidate_share(3, 7)["ok"] is True
    assert mail_admin.update_mail_interest_groups("create", name="待联系")["ok"] is True
    assert [item[0] for item in requests] == [
        "http://127.0.0.1:18766/status",
        "http://127.0.0.1:18766/scan",
        "http://127.0.0.1:18766/config",
        "http://127.0.0.1:18766/rejection?thread_key=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "http://127.0.0.1:18766/rejection-draft",
        "http://127.0.0.1:18766/rejection-template",
        "http://127.0.0.1:18766/rejection-batch",
        "http://127.0.0.1:18766/rejection-batch?batch_id=9",
        "http://127.0.0.1:18766/rejection-batch-send",
        "http://127.0.0.1:18766/rejection-send",
        "http://127.0.0.1:18766/shares",
        "http://127.0.0.1:18766/shares?limit=20",
        f"http://127.0.0.1:18766/shares/{share_token}",
        "http://127.0.0.1:18766/shares/revoke",
        "http://127.0.0.1:18766/shares/reissue",
        "http://127.0.0.1:18766/interest-groups",
    ]
    assert all(item[2]["Authorization"] == "Bearer secret-token" for item in requests)
    assert requests[9][4] == 300


def test_user_systemd_prefix_is_used_on_compute_worker(monkeypatch):
    monkeypatch.setenv("MAXREAD_MAIL_SYSTEMD_USER", "1")
    assert mail_admin._systemctl("restart", "recruiting-pipeline.service") == [
        "systemctl", "--user", "restart", "recruiting-pipeline.service",
    ]


def test_mail_admin_page_removes_cloud_base_navigation():
    html = mail_admin.MAIL_ADMIN_HTML
    assert "table-links" not in html
    assert "权威主库" not in html
    assert "打开云端主库" not in html
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
              status text,screening_status text,interview_assigned integer,is_interested integer,interview_result text,
              last_error text,updated_at text
            )
            """
        )
        candidate = {
            "name": "张三", "mail_type": "candidate", "school": "浙江大学", "education_stage": "本科",
            "current_grade": "大三", "major": "计算机", "academic_display": "4.5/5.0 · Top 5%",
            "rank": "Top 5%", "rank_evidence": "专业排名前 5%", "projects": ["World Model"],
            "purpose_summary": "申请目的：世界模型研究；竞赛获得哈尔滨站铜奖", "source_accounts": ["ZIP Lab"], "is_985": "是", "is_c9": "是",
        }
        other = {"name": "系统通知", "mail_type": "other", "projects": ["unknown"], "purpose_summary": "安全通知"}
        connection.execute(
            "insert into recruiting_threads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a" * 32, "candidate@example.com", "申请", json.dumps(candidate, ensure_ascii=False), "rec1", "https://doc", "2026-09-01T10:00:00+08:00", "", "2026-09-01T11:00:00+08:00", "active", "未筛选", 0, 1, "未开始", "", "v1"),
        )
        connection.execute(
            "insert into recruiting_threads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("b" * 32, "notice@example.com", "通知", json.dumps(other, ensure_ascii=False), "rec2", "", "2026-08-31T10:00:00+08:00", "", "", "active", "未筛选", 0, 0, "未开始", "", "v2"),
        )
    return root, db


def _rejection_fixture(tmp_path: Path):
    root = tmp_path / "mail-rejection"
    accounts = root / "data/accounts"
    accounts.mkdir(parents=True)
    db = accounts / "mail.sqlite3"
    (accounts / "zip-lab.env").write_text(
        "IMAP_USERNAME=zip.lab@outlook.com\n"
        f"MAIL_DB_PATH={db}\n"
        "RECRUITING_BASE_TOKEN=base\n"
        "RECRUITING_TABLE_ID=table\n"
        "RECRUITING_REJECTION_FEATURE_ENABLED=1\n"
        "RECRUITING_OUTBOUND_ENABLED=0\n",
        encoding="utf-8",
    )
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            create table messages(id integer primary key,subject text,message_id text,received_at text);
            create table recruiting_messages(message_record_id integer,thread_key text,direction text);
            create table recruiting_threads(
              thread_key text primary key,candidate_address text,normalized_subject text,fields_json text,
              base_record_id text,doc_id text,doc_url text,latest_time text,last_incoming_time text,last_outgoing_time text,
              status text,screening_status text,interview_assigned integer,is_interested integer,interview_result text,
              last_error text,updated_at text
            );
            """
        )
        connection.execute(
            "insert into messages values(1,'申请','<incoming@example.com>','2026-09-02T16:18:00+08:00')"
        )
        connection.execute("insert into recruiting_messages values(1,?,'incoming')", ("c" * 32,))
        zip_fields = {
            "name": "李xx", "mail_type": "candidate", "school": "unknown", "major": "unknown",
            "projects": ["World Model"], "source_accounts": ["ZIP Lab"],
        }
        bohan_fields = {**zip_fields, "name": "王同学", "source_accounts": ["Bohan"]}
        connection.execute(
            "insert into recruiting_threads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("c" * 32, "15836992650@163.com", "测试申请", json.dumps(zip_fields, ensure_ascii=False), "rec-zip", "doc-zip", "https://doc", "2026-09-02 16:18", "", "", "active", "未筛选", 0, 0, "未开始", "", "v1"),
        )
        connection.execute(
            "insert into recruiting_threads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("d" * 32, "bohan-only@example.com", "测试申请", json.dumps(bohan_fields, ensure_ascii=False), "rec-bohan", "", "", "2026-09-02 16:18", "", "", "active", "未筛选", 0, 0, "未开始", "", "v1"),
        )
    return root, db


def test_rejection_feature_is_hard_disabled(tmp_path, monkeypatch):
    root, db = _rejection_fixture(tmp_path)
    env_path = root / "data/accounts/zip-lab.env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "RECRUITING_REJECTION_FEATURE_ENABLED=1",
            "RECRUITING_REJECTION_FEATURE_ENABLED=0",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))

    disabled_calls = (
        lambda: mail_admin.mail_rejection_context("c" * 32),
        lambda: mail_admin.save_mail_rejection_template("主题", "正文"),
        lambda: mail_admin.save_mail_rejection_draft("c" * 32, "主题", "正文"),
        lambda: mail_admin.generate_mail_rejection_draft("c" * 32),
        lambda: mail_admin.send_mail_rejection(1, "candidate@example.com"),
        lambda: mail_admin.create_mail_rejection_batch(["c" * 32]),
        lambda: mail_admin.mail_rejection_batch(1),
        lambda: mail_admin.queue_mail_rejection_batch_send(1, "发送 1 封拒信"),
    )
    for call in disabled_calls:
        with pytest.raises(ValueError, match="拒信功能当前已停用"):
            call()

    assert mail_admin.reconcile_mail_rejection_batches(db) == {
        "prepared": 0,
        "sent": 0,
        "failed": 0,
    }


def _add_zip_candidate(db: Path, thread_key: str, message_id: int, address: str, name: str) -> None:
    fields = {
        "name": name, "mail_type": "candidate", "school": "浙江大学", "major": "计算机",
        "projects": ["MLSys"], "purpose_summary": "申请目的：科研实习", "source_accounts": ["ZIP Lab"],
    }
    with sqlite3.connect(db) as connection:
        connection.execute(
            "insert into messages values(?,?,?,?)",
            (message_id, "实习生申请", f"<incoming-{message_id}@example.com>", "2026-09-02T17:00:00+08:00"),
        )
        connection.execute("insert into recruiting_messages values(?,?,'incoming')", (message_id, thread_key))
        connection.execute(
            "insert into recruiting_threads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (thread_key, address, "实习生申请", json.dumps(fields, ensure_ascii=False), f"rec-{message_id}", f"doc-{message_id}", "https://doc", "2026-09-02 17:00", "", "", "active", "未筛选", 0, 0, "未开始", "", "v1"),
        )


def test_rejection_context_is_zip_lab_only_and_uses_editable_default(tmp_path, monkeypatch):
    root, _db = _rejection_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))

    context = mail_admin.mail_rejection_context("c" * 32)

    assert context["candidate"]["recipient"] == "15836992650@163.com"
    assert context["sender"] == "zip.lab@outlook.com"
    assert context["application_type"] == "general"
    assert "感谢你关注浙江大学 ZIP Lab" in context["body"]
    assert context["outbound_enabled"] is False
    records = mail_admin.mail_admin_records("mail_type=candidate&days=0&limit=10")
    supported = next(item for item in records["items"] if item["thread_key"] == "c" * 32)
    assert supported["rejection_supported"] is True
    with pytest.raises(ValueError, match="只支持 ZIP Lab"):
        mail_admin.mail_rejection_context("d" * 32)
    assert mail_admin.mail_admin_records("mail_type=candidate&account=zip-lab&days=0&limit=10")["total"] == 1
    assert mail_admin.mail_admin_records("mail_type=candidate&account=bohan&days=0&limit=10")["total"] == 1
    with pytest.raises(ValueError, match="来源邮箱"):
        mail_admin.mail_admin_records("mail_type=candidate&account=unknown&days=0&limit=10")


def test_rejection_template_and_draft_are_saved_without_sending(tmp_path, monkeypatch):
    root, db = _rejection_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    sent = []
    monkeypatch.setattr(mail_admin, "_smtp_send_zip_lab", lambda _draft: sent.append(True))

    mail_admin.save_mail_rejection_template("自定义主题", "你好 {name}，这是自定义模板。", "general")
    context = mail_admin.mail_rejection_context("c" * 32)
    first = mail_admin.save_mail_rejection_draft("c" * 32, context["subject"], context["body"])
    second = mail_admin.save_mail_rejection_draft("c" * 32, "修改主题", "修改正文")

    assert context["body"] == "你好 李xx，这是自定义模板。"
    assert first["draft"]["id"] == second["draft"]["id"]
    assert second["draft"]["subject"] == "修改主题"
    assert sent == []
    records = mail_admin.mail_admin_records("mail_type=candidate&days=0&limit=10")
    candidate = next(item for item in records["items"] if item["thread_key"] == "c" * 32)
    assert candidate["rejection_status"] == "draft"
    assert candidate["has_replied"] is False
    with sqlite3.connect(db) as connection:
        assert connection.execute("select count(*) from recruiting_outbound_drafts").fetchone()[0] == 1


def test_rejection_send_is_server_gated_and_cannot_repeat(tmp_path, monkeypatch):
    root, db = _rejection_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    draft = mail_admin.save_mail_rejection_draft("c" * 32, "回复", "拒信正文")["draft"]
    sent = []
    monkeypatch.setattr(mail_admin, "_smtp_send_zip_lab", lambda row: sent.append(str(row["recipient"])))

    with pytest.raises(ValueError, match="尚未启用"):
        mail_admin.send_mail_rejection(draft["id"], "15836992650@163.com")
    assert sent == []

    env_path = root / "data/accounts/zip-lab.env"
    env_path.write_text(env_path.read_text(encoding="utf-8").replace(
        "RECRUITING_OUTBOUND_ENABLED=0",
        "RECRUITING_OUTBOUND_ENABLED=1\nSMTP_HOST=smtp.office365.com\nSMTP_PORT=587\nSMTP_SECURITY=starttls\nSMTP_AUTH=oauth2",
    ), encoding="utf-8")
    monkeypatch.setattr(mail_admin, "_sync_rejection_side_effects", lambda _db, _id: {"ok": True, "status": "sent_sync_pending"})
    monkeypatch.setattr(mail_admin, "_smtp_ready", lambda: True)
    with pytest.raises(ValueError, match="完整收件地址"):
        mail_admin.send_mail_rejection(draft["id"], "wrong@example.com")
    result = mail_admin.send_mail_rejection(draft["id"], "15836992650@163.com")

    assert result["status"] == "sent_sync_pending"
    assert sent == ["15836992650@163.com"]
    with pytest.raises(ValueError, match="不能重复发送"):
        mail_admin.send_mail_rejection(draft["id"], "15836992650@163.com")
    with sqlite3.connect(db) as connection:
        thread = connection.execute("select screening_status,last_outgoing_time from recruiting_threads where thread_key=?", ("c" * 32,)).fetchone()
        action = json.loads(connection.execute("select new_json from recruiting_admin_actions").fetchone()[0])
        assert thread[0] == "未通过" and thread[1]
    assert action["has_replied"] is True
    context = mail_admin.mail_rejection_context("c" * 32)
    assert context["draft"]["status"] == "sent_sync_pending"
    records = mail_admin.mail_admin_records("mail_type=candidate&days=0&limit=10")
    candidate = next(item for item in records["items"] if item["thread_key"] == "c" * 32)
    assert candidate["screening_label"] == "已拒绝"
    assert candidate["has_replied"] is True


def test_ai_rejection_draft_matches_graduate_template(tmp_path, monkeypatch):
    root, _db = _rejection_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.setattr(mail_admin, "_call_rejection_ai", lambda fields, subject, templates: {
        "application_type": "graduate",
        "subject": "硕博招生回复",
        "body": "同学你好，感谢你咨询硕博招生，暂时无法进入后续交流。",
    })

    result = mail_admin.generate_mail_rejection_draft("c" * 32)

    assert result["generation"]["source"] == "ai"
    assert result["draft"]["application_type"] == "graduate"
    assert result["draft"]["generation_source"] == "ai"
    assert "硕博招生" in result["draft"]["body"]


def test_rejection_type_fallback_distinguishes_internship_and_graduate():
    assert mail_admin._infer_rejection_type({"purpose_summary": "申请科研实习"}, "实习生申请") == "internship"
    assert mail_admin._infer_rejection_type({"purpose_summary": "咨询推免直博名额"}, "硕士申请") == "graduate"


def test_ai_rejection_failure_falls_back_to_matching_template(monkeypatch):
    monkeypatch.setattr(mail_admin, "_call_rejection_ai", lambda *_args: (_ for _ in ()).throw(RuntimeError("503")))
    templates = {kind: dict(value) for kind, value in mail_admin.REJECTION_DEFAULT_TEMPLATES.items()}

    result = mail_admin._generate_rejection_copy(
        {"name": "同学", "purpose_summary": "申请科研实习"},
        "实习生申请",
        templates,
    )

    assert result["application_type"] == "internship"
    assert result["source"] == "template"
    assert "实习生招生活动" in result["body"]


def test_rejection_batch_prepares_ai_drafts_without_sending(tmp_path, monkeypatch):
    root, db = _rejection_fixture(tmp_path)
    _add_zip_candidate(db, "e" * 32, 2, "second@example.com", "王同学")
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.setattr(mail_admin, "_call_rejection_ai", lambda fields, subject, templates: {
        "application_type": "internship",
        "subject": "实习申请回复",
        "body": f"{fields.get('name')}同学你好，感谢申请，暂时无法进入后续交流。",
    })
    sent = []
    monkeypatch.setattr(mail_admin, "send_mail_rejection", lambda *_args: sent.append(True))

    created = mail_admin.create_mail_rejection_batch(["c" * 32, "e" * 32])
    progress = mail_admin.reconcile_mail_rejection_batches(db, prepare_limit=3, send_limit=0)
    batch = mail_admin.mail_rejection_batch(created["batch"]["id"])

    assert created["batch"]["status"] == "preparing"
    assert progress == {"prepared": 2, "sent": 0, "failed": 0}
    assert batch["batch"]["status"] == "ready"
    assert batch["batch"]["counts"] == {"ready": 2}
    assert all(item["generation_source"] == "ai" for item in batch["items"])
    assert sent == []
    with pytest.raises(ValueError, match="真实发送当前未启用"):
        mail_admin.queue_mail_rejection_batch_send(batch["batch"]["id"], "发送 2 封拒信")


def test_rejection_batch_rejects_mixed_ineligible_selection_atomically(tmp_path, monkeypatch):
    root, db = _rejection_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))

    with pytest.raises(ValueError, match="只支持 ZIP Lab"):
        mail_admin.create_mail_rejection_batch(["c" * 32, "d" * 32])

    with sqlite3.connect(db) as connection:
        mail_admin._ensure_rejection_schema(connection)
        assert connection.execute("select count(*) from recruiting_rejection_batches").fetchone()[0] == 0


def test_rejection_batch_send_queue_reuses_single_send_pipeline(tmp_path, monkeypatch):
    root, db = _rejection_fixture(tmp_path)
    _add_zip_candidate(db, "e" * 32, 2, "second@example.com", "王同学")
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    for key in ("c" * 32, "e" * 32):
        mail_admin.save_mail_rejection_draft(key, "回复", "拒信正文", "internship", "template")
    batch = mail_admin.create_mail_rejection_batch(["c" * 32, "e" * 32])
    env_path = root / "data/accounts/zip-lab.env"
    env_path.write_text(env_path.read_text(encoding="utf-8").replace(
        "RECRUITING_OUTBOUND_ENABLED=0", "RECRUITING_OUTBOUND_ENABLED=1",
    ), encoding="utf-8")
    monkeypatch.setattr(mail_admin, "_smtp_ready", lambda: True)
    sent = []
    monkeypatch.setattr(mail_admin, "send_mail_rejection", lambda draft_id, recipient: sent.append((draft_id, recipient)) or {"status": "sent"})

    with pytest.raises(ValueError, match="发送 2 封拒信"):
        mail_admin.queue_mail_rejection_batch_send(batch["batch"]["id"], "确认")
    queued = mail_admin.queue_mail_rejection_batch_send(batch["batch"]["id"], "发送 2 封拒信")
    first = mail_admin.reconcile_mail_rejection_batches(db, prepare_limit=1, send_limit=1)
    second = mail_admin.reconcile_mail_rejection_batches(db, prepare_limit=1, send_limit=1)
    completed = mail_admin.mail_rejection_batch(batch["batch"]["id"])

    assert queued["batch"]["status"] == "sending"
    assert first["sent"] == 1 and second["sent"] == 1
    assert len(sent) == 2 and len({item[0] for item in sent}) == 2
    assert completed["batch"]["status"] == "completed"


def test_document_message_id_matches_feishu_without_angle_brackets():
    marker = "<message-id@ziplab.co>"
    assert mail_admin._document_has_message_id("- Message-ID：message-id@ziplab.co", marker)
    assert mail_admin._document_has_message_id("- Message-ID：<message-id@ziplab.co>", marker)
    assert not mail_admin._document_has_message_id("其他内容", marker)


def test_mail_record_query_filters_and_paginates(tmp_path, monkeypatch):
    root, _db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))

    result = mail_admin.mail_admin_records("mail_type=candidate&q=浙江&project=World+Model&days=0&limit=10")

    assert result["total"] == 1
    assert result["items"][0]["name"] == "张三"
    assert result["items"][0]["has_replied"] is True
    assert result["items"][0]["is_interested"] is True
    assert result["interest_total"] == 1
    assert result["filters"]["screening_statuses"] == ["未筛选", "面试资格", "面试通过", "未通过", "实习生"]

    ranked = mail_admin.mail_admin_records("mail_type=candidate&tier=c9&rank_percentile=5&days=0&limit=10")
    assert ranked["total"] == 1
    assert ranked["items"][0]["best_rank_percentile"] == 5.0
    assert mail_admin.mail_admin_records("mail_type=candidate&reply=replied&days=0&limit=10")["total"] == 1
    assert mail_admin.mail_admin_records("mail_type=candidate&reply=unreplied&days=0&limit=10")["total"] == 0
    assert mail_admin.mail_admin_records("mail_type=candidate&q=哈尔滨&days=0&limit=10")["total"] == 0
    focused = mail_admin.mail_admin_records("mail_type=candidate&interest=only&days=0&limit=10")
    assert focused["total"] == 1
    assert focused["items"][0]["name"] == "张三"
    assert focused["interest_ungrouped"] == 1
    assert focused["interest_groups"] == []


def test_interest_groups_create_assign_rename_and_delete_to_ungrouped(tmp_path, monkeypatch):
    root, _db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.delenv("MAXREAD_MAIL_REMOTE_URL", raising=False)

    created = mail_admin.update_mail_interest_groups("create", name="待联系")
    group_id = created["group"]["id"]
    assigned = mail_admin.update_mail_interest_groups(
        "assign",
        group_id=group_id,
        thread_keys=["a" * 32],
    )
    grouped = mail_admin.mail_admin_records(
        f"mail_type=candidate&interest=only&interest_group={group_id}&days=0&limit=10"
    )

    assert assigned["updated"] == 1
    assert grouped["total"] == 1
    assert grouped["items"][0]["interest_group_name"] == "待联系"
    assert len(grouped["interest_groups"]) == 1
    assert grouped["interest_groups"][0]["id"] == group_id
    assert grouped["interest_groups"][0]["name"] == "待联系"
    assert grouped["interest_groups"][0]["position"] == 1
    assert grouped["interest_groups"][0]["count"] == 1
    assert grouped["interest_ungrouped"] == 0

    renamed = mail_admin.update_mail_interest_groups("rename", group_id=group_id, name="优先联系")
    assert renamed["group"]["name"] == "优先联系"
    deleted = mail_admin.update_mail_interest_groups("delete", group_id=group_id)
    assert deleted["moved_to_ungrouped"] == 1
    ungrouped = mail_admin.mail_admin_records(
        "mail_type=candidate&interest=only&interest_group=ungrouped&days=0&limit=10"
    )
    assert ungrouped["total"] == 1
    assert ungrouped["interest_ungrouped"] == 1


def test_unstarring_candidate_removes_interest_group_membership(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.delenv("MAXREAD_MAIL_REMOTE_URL", raising=False)
    group_id = mail_admin.update_mail_interest_groups("create", name="待联系")["group"]["id"]
    mail_admin.update_mail_interest_groups("assign", group_id=group_id, thread_keys=["a" * 32])

    mail_admin.update_mail_admin_record("a" * 32, {"is_interested": False}, "v1")

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "select count(*) from recruiting_interest_group_members where thread_key=?",
            ("a" * 32,),
        ).fetchone()[0] == 0


def test_candidate_share_is_revocable_complete_snapshot(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.delenv("MAXREAD_MAIL_REMOTE_URL", raising=False)
    monkeypatch.setenv("MAXREAD_MAIL_SHARE_SECRET", "test-candidate-share-secret")

    created = mail_admin.create_mail_candidate_share(["a" * 32], "实验室候选人", 7)
    token = created["share"]["token"]
    shared = mail_admin.mail_candidate_share(token)["share"]

    assert shared["title"] == "实验室候选人"
    assert shared["item_count"] == 1
    assert shared["items"][0]["name"] == "张三"
    assert shared["items"][0]["projects"] == ["World Model"]
    assert shared["items"][0]["candidate_address"] == "candidate@example.com"
    assert shared["items"][0]["source_accounts"] == ["ZIP Lab"]
    assert shared["items"][0]["purpose_summary"].startswith("申请目的")
    assert shared["items"][0]["doc_url"] == "https://doc"
    assert shared["items"][0]["screening_label"] == "待筛选"
    assert token.startswith("s1_")
    with sqlite3.connect(db) as connection:
        stored = connection.execute(
            "select token_hash,snapshot_json from recruiting_candidate_shares"
        ).fetchone()
    assert token not in stored[0]
    assert token not in stored[1]
    listed = mail_admin.list_mail_candidate_shares()["items"][0]
    assert listed["status"] == "active"
    assert listed["candidate_names"] == ["张三"]
    assert listed["token"] == token
    assert listed["link_available"] is True

    mail_admin.revoke_mail_candidate_share(created["share"]["id"])

    with pytest.raises(ValueError, match="分享不存在或已失效"):
        mail_admin.mail_candidate_share(token)


def test_candidate_share_rejects_non_candidate_rows(tmp_path, monkeypatch):
    root, _db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.delenv("MAXREAD_MAIL_REMOTE_URL", raising=False)

    with pytest.raises(ValueError, match="只能包含候选人"):
        mail_admin.create_mail_candidate_share(["b" * 32], "通知", 7)


def test_candidate_share_default_title_uses_candidate_names(tmp_path, monkeypatch):
    root, _db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.delenv("MAXREAD_MAIL_REMOTE_URL", raising=False)
    monkeypatch.setenv("MAXREAD_MAIL_SHARE_SECRET", "test-candidate-share-secret")

    created = mail_admin.create_mail_candidate_share(["a" * 32], "", 7)

    assert created["share"]["title"] == "张三"


def test_legacy_candidate_share_can_be_reissued_without_invalidating_old_link(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.delenv("MAXREAD_MAIL_REMOTE_URL", raising=False)
    monkeypatch.setenv("MAXREAD_MAIL_SHARE_SECRET", "test-candidate-share-secret")
    legacy_token = "L" * 43
    snapshot = {"version": 1, "items": [{"name": "张三"}]}
    with sqlite3.connect(db) as connection:
        mail_admin._ensure_candidate_share_schema(connection)
        legacy_id = int(connection.execute(
            """
            insert into recruiting_candidate_shares(
                token_hash,token_prefix,token_version,title,snapshot_json,item_count,created_at,expires_at,revoked_at
            ) values(?,?,0,?,?,?,?,?,'')
            """,
            (
                mail_admin._candidate_share_token_hash(legacy_token),
                legacy_token[:8],
                "旧分享",
                json.dumps(snapshot, ensure_ascii=False),
                1,
                "2026-09-05T00:00:00+00:00",
                "",
            ),
        ).lastrowid)

    listed = mail_admin.list_mail_candidate_shares()["items"][0]
    assert listed["link_available"] is False
    assert listed["token"] == ""
    assert mail_admin.mail_candidate_share(legacy_token)["share"]["title"] == "旧分享"

    reissued = mail_admin.reissue_mail_candidate_share(legacy_id, 7)["share"]

    assert reissued["token"].startswith("s1_")
    assert mail_admin.mail_candidate_share(reissued["token"])["share"]["title"] == "旧分享"
    assert mail_admin.mail_candidate_share(legacy_token)["share"]["title"] == "旧分享"


def test_rank_filter_accepts_any_qualifying_rank_and_ignores_gpa_ratios():
    values = mail_admin._rank_percentiles({
        "rank": "大一专业排名 30/100；大二专业排名 4/100",
        "rank_evidence": "年级第 12 名，共 120 人",
        "academic_display": "GPA 4.43/5.00",
    })

    assert values == [4.0, 10.0, 30.0]
    assert any(value <= 5 for value in values)
    assert mail_admin._rank_percentiles({"academic_display": "GPA 4.43/5.00"}) == []


def test_rank_filter_accepts_multiple_and_fullwidth_formats():
    assert mail_admin._rank_percentiles({
        "rank": "硕士第1/142；本科第1/114",
        "rank_evidence": "",
    }) == [0.7042, 0.8772]
    assert mail_admin._rank_percentiles({
        "rank": "Top 5% 专业前10%｜预计推免前3%",
        "rank_evidence": "",
    }) == [3.0, 5.0, 10.0]
    assert mail_admin._rank_percentiles({
        "rank": "硕士第1／142；Top ５％；第1名（共142人）",
        "rank_evidence": "",
    }) == [0.7042, 5.0]


def test_mail_record_update_commits_local_outbox_then_base(tmp_path, monkeypatch):
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
    assert result["sync_status"] == "committed"
    assert result["sync_attempts"] == 1
    assert calls == [("rec1", result["state"])]
    with sqlite3.connect(db) as connection:
        assert connection.execute("select screening_status,interview_assigned,interview_result from recruiting_threads where thread_key=?", ("a" * 32,)).fetchone() == ("面试资格", 1, "通过")
        assert connection.execute("select status,attempts from recruiting_admin_actions").fetchone() == ("committed", 1)


def test_mail_record_base_failure_keeps_durable_pending_outbox(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    monkeypatch.setattr(mail_admin, "_update_base_workflow", lambda *_args: (_ for _ in ()).throw(RuntimeError("base unavailable")))

    result = mail_admin.update_mail_admin_record("a" * 32, {"screening_status": "未通过"}, "v1")

    assert result["sync_status"] == "pending"
    assert "base unavailable" in result["sync_error"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("select screening_status from recruiting_threads where thread_key=?", ("a" * 32,)).fetchone()[0] == "未通过"
        assert connection.execute("select status,attempts,last_error from recruiting_admin_actions").fetchone() == ("pending", 1, "base unavailable")


def test_interest_marker_uses_the_same_local_first_outbox_transaction(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    delivered = []
    monkeypatch.setattr(
        mail_admin,
        "_update_base_workflow",
        lambda record_id, state: delivered.append((record_id, dict(state))),
    )

    result = mail_admin.update_mail_admin_record(
        "a" * 32,
        {"is_interested": False},
        "v1",
    )

    assert result["sync_status"] == "local"
    assert result["state"]["is_interested"] is False
    assert delivered == []
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "select is_interested from recruiting_threads where thread_key=?",
            ("a" * 32,),
        ).fetchone()[0] == 0
        assert connection.execute("select count(*) from recruiting_admin_actions").fetchone()[0] == 0


def test_pending_admin_action_replays_idempotently_after_restart_window(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    calls = []
    failures = [True]

    def flaky_base(record_id, state):
        calls.append((record_id, dict(state)))
        should_fail = failures.pop(0) if failures else False
        if should_fail:
            raise RuntimeError("temporary timeout")

    monkeypatch.setattr(mail_admin, "_update_base_workflow", flaky_base)
    result = mail_admin.update_mail_admin_record("a" * 32, {"screening_status": "面试资格"}, "v1")
    replay = mail_admin.reconcile_mail_admin_actions(db, limit=3)

    assert result["sync_status"] == "pending"
    assert replay == {"pending": 0, "replayed": 1}
    assert calls == [
        ("rec1", {"screening_status": "面试资格", "interview_assigned": False, "interview_result": "未开始"}),
        ("rec1", {"screening_status": "面试资格", "interview_assigned": False, "interview_result": "未开始"}),
    ]
    with sqlite3.connect(db) as connection:
        assert connection.execute("select status,attempts from recruiting_admin_actions").fetchone() == ("committed", 2)


def test_staged_action_survives_crash_before_remote_delivery(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    delivered = []
    action = mail_admin._stage_mail_admin_action(
        db,
        "a" * 32,
        {"interview_assigned": True},
        "v1",
    )

    with sqlite3.connect(db) as connection:
        assert connection.execute("select interview_assigned from recruiting_threads where thread_key=?", ("a" * 32,)).fetchone()[0] == 1
        assert connection.execute("select status from recruiting_admin_actions").fetchone()[0] == "pending"

    monkeypatch.setattr(mail_admin, "_update_base_workflow", lambda record_id, state: delivered.append((record_id, dict(state))))
    assert mail_admin.reconcile_mail_admin_actions(db, limit=3) == {"pending": 0, "replayed": 1}
    assert delivered[0][0] == "rec1"
    assert delivered[0][1]["interview_assigned"] is True


def test_pending_actions_for_same_candidate_replay_in_order(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    calls = []
    unavailable = {"value": True}

    def base_write(record_id, state):
        calls.append((record_id, dict(state)))
        if unavailable["value"]:
            raise RuntimeError("base timeout")

    monkeypatch.setattr(mail_admin, "_update_base_workflow", base_write)
    first = mail_admin.update_mail_admin_record("a" * 32, {"screening_status": "面试资格"}, "v1")
    second = mail_admin.update_mail_admin_record(
        "a" * 32,
        {"interview_result": "通过"},
        first["updated_at"],
    )

    assert first["sync_status"] == "pending"
    assert second["sync_status"] == "pending"
    assert second["sync_error"] == "等待前序飞书同步"
    assert len(calls) == 1

    unavailable["value"] = False
    assert mail_admin.reconcile_mail_admin_actions(db, limit=10) == {"pending": 0, "replayed": 2}
    assert [item[1] for item in calls[1:]] == [
        {"screening_status": "面试资格", "interview_assigned": False, "interview_result": "未开始"},
        {"screening_status": "面试资格", "interview_assigned": False, "interview_result": "通过"},
    ]
    assert mail_admin.mail_admin_sync_status(db) == {
        "pending": 0,
        "replayed": 0,
        "base_pull": {"status": "never"},
    }


def test_admin_outbox_migrates_legacy_audit_table_in_place(tmp_path, monkeypatch):
    root, db = _record_fixture(tmp_path)
    monkeypatch.setenv("MAXREAD_MAIL_ROOT", str(root))
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            create table recruiting_admin_actions(
                id integer primary key autoincrement,thread_key text not null,
                old_json text not null,new_json text not null,created_at text not null
            )
            """
        )
        connection.execute(
            "insert into recruiting_admin_actions(thread_key,old_json,new_json,created_at) values(?,?,?,?)",
            ("a" * 32, "{}", "{}", "legacy"),
        )
    monkeypatch.setattr(mail_admin, "_update_base_workflow", lambda *_args: None)

    result = mail_admin.update_mail_admin_record("a" * 32, {"screening_status": "面试资格"}, "v1")

    assert result["sync_status"] == "committed"
    with sqlite3.connect(db) as connection:
        columns = {row[1] for row in connection.execute("pragma table_info(recruiting_admin_actions)")}
        assert {"operation_id", "record_id", "status", "attempts", "last_error", "updated_at"} <= columns
        assert connection.execute("select status from recruiting_admin_actions where created_at='legacy'").fetchone()[0] == "committed"


def test_mail_admin_page_contains_candidate_workbench_controls():
    html = mail_admin.MAIL_ADMIN_HTML
    assert "邮件记录" in html
    assert "api/admin/mail/records" in html
    assert "api('api/admin/mail/record'" in html
    assert "最近一周新增候选人" not in html  # links arrive only after authenticated API response


def test_mail_admin_page_uses_compact_master_detail_layout():
    html = mail_admin.MAIL_ADMIN_HTML

    assert 'data-view="candidates"' in html
    assert 'data-view="operations"' in html
    assert 'id="candidate-panel"' in html
    assert 'id="operations-panel"' in html
    assert 'class="record-table-wrap"' in html
    assert "max-height:calc(100dvh - 296px)" in html
    assert "recordState={items:[],view:'all',offset:0,limit:20" in html
    assert "候选人资料与来信状态" in html
    assert "<th>摘要</th>" not in html
    assert "<th>回复</th>" in html
    assert "<th>筛选状态</th>" not in html
    assert "<th>面试</th>" not in html
    assert 'id="record-reply"' in html
    assert 'id="record-account"' in html
    assert 'id="record-type"' not in html
    assert "mail_type:'candidate'" in html
    assert "account:values.account" in html
    assert '<option value="">回复状态</option>' in html
    assert "reply:values.reply" in html
    assert "updateRecord(" not in html
    assert "item.has_replied?'已回复':'未回复'" in html
    assert "['回复状态',item.has_replied?'已回复':'未回复']" in html
    assert 'class="ops-details wide"' in html
    assert "height:100dvh" in html
    assert "margin:0 0 0 auto" in html
    assert "data.admin_sync?.pending" in html
    assert 'id="record-tier"' in html
    assert 'id="record-rank"' in html
    assert "rank_percentile:values.rank" in html
    assert "权威主库：飞书 Base" not in html
    assert 'id="base-pull-status"' not in html
    assert 'id="admin-password"' in html
    assert 'id="admin-username"' in html
    assert "username:$('admin-username').value" in html
    assert "loginMailAdmin(event)" in html
    assert "logoutMailAdmin()" in html
    assert 'id="record-page-input"' in html
    assert 'id="record-jump"' in html
    assert "jumpRecordPage()" in html
    assert "Math.ceil(recordState.total/recordState.limit)" in html
    assert 'data-view="interest"' in html
    assert 'id="tab-interest-count"' in html
    assert "重点关注" in html
    assert "setMailView('interest')" in html
    assert "filterSets:{all:" in html
    assert "interest:{q:'',account:'',reply:'',project:'',tier:'',rank:'0',days:'0'}" in html
    assert "saveRecordFilters()" in html
    assert "applyRecordFilters(recordState.view)" in html
    assert 'id="interest-group-bar"' in html
    assert 'id="interest-group-tabs"' in html
    assert 'id="bulk-interest-group"' in html
    assert 'id="interest-group-dialog"' in html
    assert "interest_group:recordState.view==='interest'?recordState.interestGroup:'all'" in html
    assert "api('api/admin/mail/interest-groups'" in html
    assert "deleteInterestGroup()" in html
    assert "assignSelectedInterestGroup()" in html
    assert "toggleInterested(event" in html
    assert "changes:{is_interested:!item.is_interested}" in html
    assert "focus-card" not in html
    assert "rejection" not in html.casefold()
    assert "拒信" not in html
    assert 'id="share-selected"' not in html
    assert 'id="selection-bar"' in html
    assert "生成分享链接" in html
    assert 'id="select-page"' in html
    assert "recordState.selected" in html
    assert "api('api/admin/mail/shares'" in html
    assert "一次最多分享 50 位候选人" in html
    assert "candidate_names" in html
    assert "候选人：" in html
    assert "names.slice(0,3)" in html
    assert "copyStoredShare" in html
    assert "reissueCandidateShare" in html
    assert "生成新链接" in html


def test_public_candidate_share_page_renders_complete_candidate_fields():
    html = mail_admin.MAIL_SHARE_HTML

    assert "ZIP Lab · 候选人分享" in html
    assert "credentials:'omit'" in html
    assert "candidate_address" in html
    assert "purpose_summary" in html
    assert "source_accounts" in html
    assert "doc_url" in html


def test_nginx_post_allowlist_excludes_rejection_actions():
    config = Path("deploy/nginx/maxread-location.conf.example").read_text(encoding="utf-8")
    assert "mail/(scan|config|record|shares|interest-groups)" in config
    assert "rejection" not in config


def test_mail_admin_sync_status_exposes_last_base_pull(tmp_path):
    db = tmp_path / "mail.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            create table recruiting_sync_state(
                sync_key text primary key,status text,details_json text,
                started_at text,finished_at text,last_error text
            )
            """
        )
        connection.execute(
            "insert into recruiting_sync_state values(?,?,?,?,?,?)",
            (
                "feishu_base_pull",
                "completed",
                json.dumps({"remote_records": 865, "updated": 2}),
                "2026-09-02T01:00:00+00:00",
                "2026-09-02T01:00:03+00:00",
                "",
            ),
        )

    result = mail_admin.mail_admin_sync_status(db)

    assert result["base_pull"]["status"] == "completed"
    assert result["base_pull"]["remote_records"] == 865
    assert result["base_pull"]["updated"] == 2
