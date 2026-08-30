from __future__ import annotations

import sqlite3
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
    assert (root / "data/mail-control.json").exists()
    try:
        mail_admin.trigger_mail_scan("not-configured")
    except ValueError as exc:
        assert "未知邮箱" in str(exc)
    else:
        raise AssertionError("unknown account should fail")
