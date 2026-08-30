from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAIL_ROOT = Path("/opt/maxread/features/mail_ingestion")
DEFAULT_PIPELINE_SERVICE = "recruiting-pipeline.service"
DEFAULT_REPORT_TIMER = "recruiting-weekly-report.timer"
MAIL_ADMIN_HTML = (Path(__file__).resolve().parent / "static" / "mail_admin.html").read_text(encoding="utf-8")


def mail_admin_status() -> dict[str, Any]:
    mail_root = _mail_root()
    primary_env = mail_root / "data/accounts/zip-lab.env"
    env = _read_env(primary_env)
    db_path = Path(env.get("MAIL_DB_PATH", str(mail_root / "data/mail_collector.sqlite3")))
    accounts = _mail_accounts(mail_root, db_path)
    runs, thread_stats, errors = _mail_database_status(db_path)
    service = _systemd_show(DEFAULT_PIPELINE_SERVICE)
    timer = _systemd_show(DEFAULT_REPORT_TIMER)
    control = _control_status(mail_root)
    latest = runs[0] if runs else {}
    business_state = "healthy"
    if str(service.get("ActiveState") or "") != "active":
        business_state = "down"
    elif latest and int(latest.get("failed_threads") or 0) > 0:
        business_state = "degraded"
    return {
        "ok": True,
        "business_state": business_state,
        "service": service,
        "report_timer": timer,
        "accounts": accounts,
        "runs": runs,
        "thread_stats": thread_stats,
        "errors": errors,
        "control": control,
        "config": {
            "scan_interval_minutes": _scan_interval_minutes(env),
            "report_interval_hours": _report_interval_hours(env),
            "scan_limit": _safe_int(env.get("MAIL_SCAN_LIMIT"), 100),
        },
    }


def update_mail_admin_config(scan_interval_minutes: int, report_interval_hours: int) -> dict[str, Any]:
    scan_minutes = int(scan_interval_minutes)
    report_hours = int(report_interval_hours)
    if not 5 <= scan_minutes <= 10_080:
        raise ValueError("自动扫描间隔必须在 5 分钟到 7 天之间")
    if not 1 <= report_hours <= 720:
        raise ValueError("周报发布间隔必须在 1 小时到 30 天之间")
    mail_root = _mail_root()
    env_path = mail_root / "data/accounts/zip-lab.env"
    _update_env(
        env_path,
        {
            "RECRUITING_SCAN_INTERVAL_DAYS": f"{scan_minutes / 1440:.8f}".rstrip("0").rstrip("."),
            "RECRUITING_REPORT_INTERVAL_HOURS": str(report_hours),
        },
    )
    dropin = Path(os.environ.get(
        "MAXREAD_MAIL_REPORT_TIMER_DROPIN",
        "/etc/systemd/system/recruiting-weekly-report.timer.d/interval.conf",
    ))
    dropin.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(
        dropin,
        "[Timer]\nOnCalendar=\nOnUnitActiveSec=" + str(report_hours) + "h\nPersistent=true\nAccuracySec=5m\n",
        mode=0o644,
    )
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "restart", DEFAULT_PIPELINE_SERVICE])
    _run(["systemctl", "restart", DEFAULT_REPORT_TIMER])
    return {
        "ok": True,
        "scan_interval_minutes": scan_minutes,
        "report_interval_hours": report_hours,
    }


def trigger_mail_scan(account_id: str) -> dict[str, Any]:
    clean = str(account_id or "").strip().lower()
    mail_root = _mail_root()
    allowed = {"all", *(item["id"] for item in _mail_accounts(mail_root, _mail_db_path(mail_root)))}
    if clean not in allowed:
        raise ValueError("未知邮箱账号")
    control = _control_status(mail_root)
    if control.get("active"):
        raise ValueError("已有邮件扫描任务正在运行")
    unit = f"maxread-mail-scan-{uuid.uuid4().hex[:10]}.service"
    script = mail_root / "bin/recruiting-control-scan"
    if not script.exists():
        raise RuntimeError("邮件扫描控制脚本尚未部署")
    result = _run(
        [
            "systemd-run",
            f"--unit={unit.removesuffix('.service')}",
            "--description=MaxRead manual recruiting mailbox scan",
            "--property=CPUQuota=60%",
            "--property=MemoryMax=700M",
            "--property=RuntimeMaxSec=1800",
            str(script),
            clean,
        ]
    )
    state = {
        "unit": unit,
        "account": clean,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "systemd": result.strip()[:500],
    }
    _atomic_text(mail_root / "data/mail-control.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    return {"ok": True, **state}


def _mail_root() -> Path:
    return Path(os.environ.get("MAXREAD_MAIL_ROOT", str(DEFAULT_MAIL_ROOT))).expanduser()


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _mail_db_path(mail_root: Path) -> Path:
    env = _read_env(mail_root / "data/accounts/zip-lab.env")
    return Path(env.get("MAIL_DB_PATH", str(mail_root / "data/mail_collector.sqlite3")))


def _mail_accounts(mail_root: Path, db_path: Path) -> list[dict[str, Any]]:
    aggregates: dict[str, dict[str, Any]] = {}
    if db_path.exists():
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                select case when instr(mailbox, '::') > 0 then substr(mailbox, 1, instr(mailbox, '::') - 1) else mailbox end as account,
                       count(*) as messages, count(distinct mailbox) as folders,
                       max(scanned_at) as last_scan, max(received_at) as latest_mail
                from messages group by account
                """
            ).fetchall()
            aggregates = {str(row["account"]).casefold(): dict(row) for row in rows}
    output = []
    for env_path in sorted((mail_root / "data/accounts").glob("*.env")):
        values = _read_env(env_path)
        address = str(values.get("IMAP_USERNAME") or "").strip()
        if not address:
            continue
        stats = aggregates.get(address.casefold(), {})
        output.append({
            "id": env_path.stem,
            "address": address,
            "messages": int(stats.get("messages") or 0),
            "folders": int(stats.get("folders") or 0),
            "last_scan": str(stats.get("last_scan") or ""),
            "latest_mail": str(stats.get("latest_mail") or ""),
            "scan_limit": _safe_int(values.get("MAIL_SCAN_LIMIT"), 100),
        })
    return output


def _mail_database_status(db_path: Path) -> tuple[list[dict], dict[str, int], list[dict]]:
    if not db_path.exists():
        return [], {}, []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        runs = [dict(row) for row in connection.execute(
            "select * from recruiting_runs order by started_at desc limit 12"
        ).fetchall()]
        thread_stats = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "select status, count(*) as count from recruiting_threads group by status"
            ).fetchall()
        }
        errors = [dict(row) for row in connection.execute(
            """
            select substr(last_error, 1, 220) as error, count(*) as count, max(updated_at) as last_seen
            from recruiting_threads where last_error <> ''
            group by substr(last_error, 1, 220)
            order by count(*) desc, last_seen desc limit 8
            """
        ).fetchall()]
    return runs, thread_stats, errors


def _systemd_show(unit: str) -> dict[str, str]:
    try:
        output = _run([
            "systemctl", "show", unit,
            "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp,NRestarts,NextElapseUSecRealtime,LastTriggerUSec,Result,ExecMainStatus",
        ])
    except Exception as exc:
        return {"ActiveState": "unknown", "error": str(exc)[:240]}
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _control_status(mail_root: Path) -> dict[str, Any]:
    path = mail_root / "data/mail-control.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active": False}
    unit = str(state.get("unit") or "")
    unit_state = _systemd_show(unit) if unit else {}
    return {**state, "active": unit_state.get("ActiveState") == "active", "unit_state": unit_state}


def _scan_interval_minutes(env: dict[str, str]) -> int:
    try:
        return max(5, round(float(env.get("RECRUITING_SCAN_INTERVAL_DAYS", "1")) * 1440))
    except ValueError:
        return 1440


def _report_interval_hours(env: dict[str, str]) -> int:
    return max(1, _safe_int(env.get("RECRUITING_REPORT_INTERVAL_HOURS"), 168))


def _safe_int(value, default: int) -> int:
    try:
        return int(str(value or default))
    except ValueError:
        return int(default)


def _update_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())
    _atomic_text(path, "\n".join(output).rstrip() + "\n", mode=path.stat().st_mode & 0o777 if path.exists() else 0o600)


def _atomic_text(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _run(argv: list[str]) -> str:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "command failed").strip()[:1000])
    return completed.stdout.strip()
