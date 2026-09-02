from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode


DEFAULT_MAIL_ROOT = Path("/opt/maxread/features/mail_ingestion")
DEFAULT_PIPELINE_SERVICE = "recruiting-pipeline.service"
DEFAULT_REPORT_TIMER = "recruiting-weekly-report.timer"
MAIL_ADMIN_HTML = (Path(__file__).resolve().parent / "static" / "mail_admin.html").read_text(encoding="utf-8")
MAIL_BASE_ROOT = "https://ccnsbbr30xgq.feishu.cn/base/S4v4bdOCuaWvAQs90vCcek4anHh?table=tblJtH3AVWn0Gar8"
MAIL_PUBLIC_LINKS = (
    ("候选人池", "全部候选人及当前筛选状态", "vewhN2XnwI", "primary"),
    ("未筛选", "等待初筛的候选人", "vew37TarSs", "primary"),
    ("最近一周新增候选人", "按最新邮件时间倒序", "vewVVbQsCs", "primary"),
    ("其他邮件", "非候选邮件的独立归档", "vewmpcpnxQ", "secondary"),
    ("MLSys", "训练与系统方向", "vewaFIevDP", "topic"),
    ("Agentic Infrastructure", "Agent 与基础设施方向", "vewclBCsP4", "topic"),
    ("Kernel Efficiency", "算子与内核方向", "vewJL5BjVw", "topic"),
    ("World Model", "世界模型方向", "vewVuhfX3m", "topic"),
)
SCREENING_STATUSES = ("未筛选", "面试资格", "面试通过", "未通过", "实习生")
INTERVIEW_RESULTS = ("未开始", "通过", "不通过")
_ADMIN_ACTION_LOCK = threading.RLock()


def mail_admin_status() -> dict[str, Any]:
    if _remote_url():
        return _remote_request("/status")
    mail_root = _mail_root()
    primary_env = mail_root / "data/accounts/zip-lab.env"
    env = _read_env(primary_env)
    db_path = Path(env.get("MAIL_DB_PATH", str(mail_root / "data/mail_collector.sqlite3")))
    admin_sync = mail_admin_sync_status(db_path)
    accounts = _mail_accounts(mail_root, db_path)
    runs, thread_stats, errors = _mail_database_status(db_path)
    service = _systemd_show(DEFAULT_PIPELINE_SERVICE)
    timer = _systemd_show(DEFAULT_REPORT_TIMER)
    control = _control_status(mail_root)
    latest = runs[0] if runs else {}
    business_state = "healthy"
    if control.get("active"):
        business_state = "scanning"
    elif str(service.get("ActiveState") or "") != "active":
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
        "overview": mail_public_summary(),
        "admin_sync": admin_sync,
    }


def mail_public_summary() -> dict[str, Any]:
    db_path = _mail_db_path(_mail_root())
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    cutoff = now - timedelta(days=7)
    candidate_total = 0
    other_total = 0
    recent_candidates = 0
    statuses: dict[str, int] = {}
    latest_time = ""
    if db_path.exists():
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "select fields_json,latest_time,screening_status,status from recruiting_threads"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                try:
                    fields = json.loads(str(row["fields_json"] or "{}"))
                except json.JSONDecodeError:
                    fields = {}
                if str(row["status"] or "") == "inactive":
                    continue
                current_time = str(row["latest_time"] or "")
                latest_time = max(latest_time, current_time)
                if str(fields.get("mail_type") or "other") == "other":
                    other_total += 1
                    continue
                candidate_total += 1
                status = str(row["screening_status"] or "未筛选")
                statuses[status] = statuses.get(status, 0) + 1
                parsed = _parse_datetime(current_time)
                if parsed is not None and parsed >= cutoff:
                    recent_candidates += 1
    return {
        "ok": True,
        "updated_at": latest_time,
        "metrics": {
            "candidate_total": candidate_total,
            "unscreened": statuses.get("未筛选", 0),
            "recent_candidates": recent_candidates,
            "other_total": other_total,
        },
        "status_counts": statuses,
        "links": [
            {"title": title, "description": description, "url": f"{MAIL_BASE_ROOT}&view={view}", "group": group}
            for title, description, view, group in MAIL_PUBLIC_LINKS
        ],
    }


def mail_admin_records(query_string: str = "") -> dict[str, Any]:
    if _remote_url():
        suffix = f"?{query_string}" if query_string else ""
        return _remote_request(f"/records{suffix}")
    query = parse_qs(str(query_string or ""))
    search = str(query.get("q", [""])[0] or "").strip().casefold()[:120]
    mail_type = str(query.get("mail_type", ["candidate"])[0] or "candidate").strip()
    screening = str(query.get("screening", [""])[0] or "").strip()
    project = str(query.get("project", [""])[0] or "").strip()
    days = _bounded_int(query.get("days", ["30"])[0], 30, 0, 3650)
    limit = _bounded_int(query.get("limit", ["30"])[0], 30, 10, 100)
    offset = _bounded_int(query.get("offset", ["0"])[0], 0, 0, 100_000)
    if mail_type not in {"all", "candidate", "other"}:
        raise ValueError("不支持的邮件类型")
    if screening and screening not in SCREENING_STATUSES:
        raise ValueError("不支持的筛选状态")
    cutoff = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=days) if days else None
    db_path = _mail_db_path(_mail_root())
    records: list[dict[str, Any]] = []
    if db_path.exists():
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                select thread_key,candidate_address,normalized_subject,fields_json,
                       base_record_id,doc_url,latest_time,last_incoming_time,last_outgoing_time,
                       status,screening_status,interview_assigned,interview_result,last_error,updated_at
                from recruiting_threads
                where status<>'inactive'
                order by coalesce(latest_time,'') desc,updated_at desc
                """
            ).fetchall()
            for row in rows:
                item = _mail_record(row)
                if mail_type != "all" and item["mail_type"] != mail_type:
                    continue
                if screening and item["screening_status"] != screening:
                    continue
                if project and project not in item["projects"]:
                    continue
                parsed = _parse_datetime(item["latest_time"])
                if cutoff is not None and (parsed is None or parsed < cutoff):
                    continue
                if search and search not in item["search_text"]:
                    continue
                item.pop("search_text", None)
                records.append(item)
    total = len(records)
    return {
        "ok": True,
        "items": records[offset:offset + limit],
        "total": total,
        "offset": offset,
        "limit": limit,
        "filters": {
            "screening_statuses": list(SCREENING_STATUSES),
            "interview_results": list(INTERVIEW_RESULTS),
            "projects": ["MLSys", "Agentic Infrastructure", "Kernel Efficiency", "World Model"],
        },
    }


def update_mail_admin_record(thread_key: str, changes: dict[str, Any], expected_updated_at: str = "") -> dict[str, Any]:
    if _remote_url():
        return _remote_request(
            "/record",
            {"thread_key": thread_key, "changes": changes, "expected_updated_at": expected_updated_at},
        )
    clean_key = str(thread_key or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", clean_key):
        raise ValueError("无效邮件线程")
    allowed = {"screening_status", "interview_assigned", "interview_result"}
    requested = {str(key): value for key, value in dict(changes or {}).items() if key in allowed}
    if not requested:
        raise ValueError("没有可更新字段")
    db_path = _mail_db_path(_mail_root())
    with _ADMIN_ACTION_LOCK:
        action = _stage_mail_admin_action(
            db_path,
            clean_key,
            requested,
            expected_updated_at,
        )
        if int(action.get("action_id") or 0):
            delivery = _deliver_mail_admin_action(db_path, int(action["action_id"]))
        else:
            delivery = {"status": "committed", "attempts": 0, "last_error": ""}
    return {
        "ok": True,
        "thread_key": clean_key,
        "state": action["state"],
        "updated_at": action["updated_at"],
        "sync_status": delivery["status"],
        "sync_attempts": delivery["attempts"],
        "sync_error": delivery["last_error"],
    }


def _stage_mail_admin_action(
    db_path: Path,
    thread_key: str,
    requested: dict[str, Any],
    expected_updated_at: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    operation_id = uuid.uuid4().hex
    with sqlite3.connect(db_path, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_admin_actions_schema(connection)
        connection.execute("begin immediate")
        row = connection.execute(
            "select * from recruiting_threads where thread_key=?",
            (thread_key,),
        ).fetchone()
        if row is None:
            raise ValueError("邮件记录不存在")
        if expected_updated_at and str(row["updated_at"] or "") != str(expected_updated_at):
            raise ValueError("记录已被其他操作更新，请刷新后重试")
        fields = json.loads(str(row["fields_json"] or "{}"))
        if str(fields.get("mail_type") or "other") == "other":
            raise ValueError("其他邮件不使用候选筛选状态")
        record_id = str(row["base_record_id"] or "").strip()
        if not record_id:
            raise ValueError("该记录尚未绑定飞书 Base，不能修改")
        old_state = _workflow_state(row)
        new_state = _validated_workflow_state(old_state, requested)
        if new_state == old_state:
            connection.rollback()
            return {"action_id": 0, "state": new_state, "updated_at": str(row["updated_at"] or "")}
        cursor = connection.execute(
            """
            update recruiting_threads
            set screening_status=?,interview_assigned=?,interview_result=?,updated_at=?
            where thread_key=? and updated_at=?
            """,
            (
                new_state["screening_status"],
                int(new_state["interview_assigned"]),
                new_state["interview_result"],
                now,
                thread_key,
                str(row["updated_at"] or ""),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("记录在写入期间发生变化")
        action_id = connection.execute(
            """
            insert into recruiting_admin_actions(
                operation_id,thread_key,record_id,old_json,new_json,status,
                attempts,last_error,created_at,updated_at
            ) values(?,?,?,?,?,'pending',0,'',?,?)
            """,
            (
                operation_id,
                thread_key,
                record_id,
                json.dumps(old_state, ensure_ascii=False),
                json.dumps(new_state, ensure_ascii=False),
                now,
                now,
            ),
        ).lastrowid
        connection.commit()
    return {"action_id": int(action_id), "state": new_state, "updated_at": now}


def _workflow_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "screening_status": str(row["screening_status"] or "未筛选"),
        "interview_assigned": bool(row["interview_assigned"]),
        "interview_result": str(row["interview_result"] or "未开始"),
    }


def _validated_workflow_state(old_state: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    new_state = dict(old_state)
    if "screening_status" in requested:
        value = str(requested["screening_status"] or "").strip()
        if value not in SCREENING_STATUSES:
            raise ValueError("不支持的筛选状态")
        new_state["screening_status"] = value
    if "interview_result" in requested:
        value = str(requested["interview_result"] or "").strip()
        if value not in INTERVIEW_RESULTS:
            raise ValueError("不支持的面试结果")
        new_state["interview_result"] = value
    if "interview_assigned" in requested:
        new_state["interview_assigned"] = bool(requested["interview_assigned"])
    return new_state


def _deliver_mail_admin_action(db_path: Path, action_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_admin_actions_schema(connection)
        action = connection.execute(
            "select * from recruiting_admin_actions where id=?",
            (int(action_id),),
        ).fetchone()
        if action is None:
            raise RuntimeError("待同步操作不存在")
        if str(action["status"] or "") == "committed":
            return {"status": "committed", "attempts": int(action["attempts"] or 0), "last_error": ""}
        older = connection.execute(
            """
            select id from recruiting_admin_actions
            where thread_key=? and id<? and status in ('pending','syncing')
            order by id asc limit 1
            """,
            (str(action["thread_key"] or ""), int(action_id)),
        ).fetchone()
        if older is not None:
            return {
                "status": "pending",
                "attempts": int(action["attempts"] or 0),
                "last_error": "等待前序飞书同步",
            }
        attempts = int(action["attempts"] or 0) + 1
        connection.execute(
            "update recruiting_admin_actions set status='syncing',attempts=?,updated_at=? where id=?",
            (attempts, now, int(action_id)),
        )
        connection.commit()
        record_id = str(action["record_id"] or "")
        new_state = json.loads(str(action["new_json"] or "{}"))
    try:
        _update_base_workflow(record_id, new_state)
    except Exception as exc:
        error = str(exc)[:500]
        with sqlite3.connect(db_path, timeout=10) as connection:
            _ensure_admin_actions_schema(connection)
            connection.execute(
                "update recruiting_admin_actions set status='pending',last_error=?,updated_at=? where id=?",
                (error, datetime.now(timezone.utc).isoformat(), int(action_id)),
            )
            connection.commit()
        return {"status": "pending", "attempts": attempts, "last_error": error}
    with sqlite3.connect(db_path, timeout=10) as connection:
        _ensure_admin_actions_schema(connection)
        connection.execute(
            "update recruiting_admin_actions set status='committed',last_error='',updated_at=? where id=?",
            (datetime.now(timezone.utc).isoformat(), int(action_id)),
        )
        connection.commit()
    return {"status": "committed", "attempts": attempts, "last_error": ""}


def reconcile_mail_admin_actions(db_path: Path, limit: int = 3) -> dict[str, Any]:
    if not Path(db_path).exists():
        return {"pending": 0, "replayed": 0}
    replayed = 0
    with _ADMIN_ACTION_LOCK:
        with sqlite3.connect(db_path, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            _ensure_admin_actions_schema(connection)
            rows = connection.execute(
                """
                select id from recruiting_admin_actions
                where status in ('pending','syncing')
                order by id asc limit ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        for row in rows:
            result = _deliver_mail_admin_action(db_path, int(row["id"]))
            if result["status"] == "committed":
                replayed += 1
        with sqlite3.connect(db_path, timeout=10) as connection:
            _ensure_admin_actions_schema(connection)
            pending = int(connection.execute(
                "select count(*) from recruiting_admin_actions where status in ('pending','syncing')"
            ).fetchone()[0])
    return {"pending": pending, "replayed": replayed}


def mail_admin_sync_status(db_path: Path) -> dict[str, Any]:
    if not Path(db_path).exists():
        return {"pending": 0, "replayed": 0}
    with sqlite3.connect(db_path, timeout=5) as connection:
        _ensure_admin_actions_schema(connection)
        pending = int(connection.execute(
            "select count(*) from recruiting_admin_actions where status in ('pending','syncing')"
        ).fetchone()[0])
    return {"pending": pending, "replayed": 0}


def _ensure_admin_actions_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        create table if not exists recruiting_admin_actions(
            id integer primary key autoincrement,
            operation_id text not null default '',
            thread_key text not null,
            record_id text not null default '',
            old_json text not null,
            new_json text not null,
            status text not null default 'committed',
            attempts integer not null default 0,
            last_error text not null default '',
            created_at text not null,
            updated_at text not null default ''
        )
        """
    )
    columns = {str(row[1]) for row in connection.execute("pragma table_info(recruiting_admin_actions)")}
    additions = {
        "operation_id": "text not null default ''",
        "record_id": "text not null default ''",
        "status": "text not null default 'committed'",
        "attempts": "integer not null default 0",
        "last_error": "text not null default ''",
        "updated_at": "text not null default ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"alter table recruiting_admin_actions add column {name} {definition}")
    connection.execute(
        "create index if not exists recruiting_admin_actions_status_idx on recruiting_admin_actions(status,id)"
    )
    connection.commit()


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _mail_record(row: sqlite3.Row) -> dict[str, Any]:
    try:
        fields = json.loads(str(row["fields_json"] or "{}"))
    except json.JSONDecodeError:
        fields = {}
    mail_type = "other" if str(fields.get("mail_type") or "other") == "other" else "candidate"
    study_parts = [str(fields.get("school") or "unknown")]
    for key in ("education_stage", "current_grade"):
        value = str(fields.get(key) or "").strip()
        if value and value != "unknown":
            study_parts.append(value)
    if str(fields.get("entry_year") or "unknown") != "unknown":
        study_parts.append(f"入学 {fields['entry_year']}")
    if str(fields.get("expected_grad_year") or "unknown") != "unknown":
        study_parts.append(f"预计毕业 {fields['expected_grad_year']}")
    item = {
        "thread_key": str(row["thread_key"]),
        "candidate_address": str(row["candidate_address"] or ""),
        "subject": str(row["normalized_subject"] or ""),
        "name": str(fields.get("name") or row["candidate_address"] or "unknown"),
        "mail_type": mail_type,
        "school": str(fields.get("school") or "unknown"),
        "study": "｜".join(study_parts),
        "major": str(fields.get("major") or "unknown"),
        "academic_display": str(fields.get("academic_display") or "unknown"),
        "rank": str(fields.get("rank") or "未提供"),
        "rank_evidence": str(fields.get("rank_evidence") or "未提供"),
        "projects": [str(value) for value in fields.get("projects") or [] if str(value)],
        "purpose_summary": str(fields.get("purpose_summary") or ""),
        "source_accounts": [str(value) for value in fields.get("source_accounts") or [] if str(value)],
        "is_985": str(fields.get("is_985") or "未知"),
        "is_c9": str(fields.get("is_c9") or "未知"),
        "latest_time": str(row["latest_time"] or ""),
        "has_replied": bool(str(row["last_outgoing_time"] or "")),
        "screening_status": str(row["screening_status"] or "未筛选"),
        "interview_assigned": bool(row["interview_assigned"]),
        "interview_result": str(row["interview_result"] or "未开始"),
        "doc_url": str(row["doc_url"] or ""),
        "base_record_id": str(row["base_record_id"] or ""),
        "last_error": str(row["last_error"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }
    item["search_text"] = " ".join(
        str(value) for value in (
            item["name"], item["candidate_address"], item["subject"], item["school"],
            item["study"], item["major"], item["academic_display"], item["rank"],
            item["purpose_summary"], " ".join(item["projects"]),
        )
    ).casefold()
    return item


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(str(value))))
    except (TypeError, ValueError):
        return default


def _update_base_workflow(record_id: str, state: dict[str, Any]) -> None:
    mail_root = _mail_root()
    primary = _read_env(mail_root / "data/accounts/zip-lab.env")
    project_root = mail_root.parent.parent
    project = _read_env(project_root / ".env")
    values = {**project, **primary}
    lark_cli = values.get("RECRUITING_LARK_CLI") or values.get("MAXREAD_LARK_CLI") or "lark-cli"
    base_token = str(values.get("RECRUITING_BASE_TOKEN") or "").strip()
    table_id = str(values.get("RECRUITING_TABLE_ID") or "").strip()
    if not base_token or not table_id:
        raise RuntimeError("招聘 Base 配置不完整")
    payload = {
        "update_records": {
            str(record_id): {
                "筛选状态": [state["screening_status"]],
                "是否已分配面试": bool(state["interview_assigned"]),
                "面试结果": [state["interview_result"]],
            }
        }
    }
    env = dict(os.environ)
    node_value = str(values.get("MAXREAD_NODE") or "").strip()
    if node_value:
        node = Path(node_value).expanduser()
        env["PATH"] = f"{node.parent}:{Path(lark_cli).expanduser().parent}:{env.get('PATH', '')}"
    completed = subprocess.run(
        [
            lark_cli, "base", "+record-batch-update",
            "--base-token", base_token,
            "--table-id", table_id,
            "--json", json.dumps(payload, ensure_ascii=False),
            "--as", values.get("RECRUITING_FEISHU_AS") or values.get("MAXREAD_FEISHU_AS") or "bot",
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "飞书 Base 更新失败").strip()[:500])
    try:
        result = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("飞书 Base 返回无效 JSON") from exc
    if result.get("ok") is False:
        raise RuntimeError(str(result.get("error") or "飞书 Base 更新失败")[:500])


def update_mail_admin_config(scan_interval_minutes: int, report_interval_hours: int) -> dict[str, Any]:
    scan_minutes = int(scan_interval_minutes)
    report_hours = int(report_interval_hours)
    if not 5 <= scan_minutes <= 10_080:
        raise ValueError("自动扫描间隔必须在 5 分钟到 7 天之间")
    if not 1 <= report_hours <= 720:
        raise ValueError("周报发布间隔必须在 1 小时到 30 天之间")
    if _remote_url():
        return _remote_request(
            "/config",
            {
                "scan_interval_minutes": scan_minutes,
                "report_interval_hours": report_hours,
            },
        )
    mail_root = _mail_root()
    env_path = mail_root / "data/accounts/zip-lab.env"
    _update_env(
        env_path,
        {
            "RECRUITING_SCAN_INTERVAL_DAYS": f"{scan_minutes / 1440:.8f}".rstrip("0").rstrip("."),
            "RECRUITING_REPORT_INTERVAL_HOURS": str(report_hours),
        },
    )
    default_dropin = (
        Path.home() / ".config/systemd/user/recruiting-weekly-report.timer.d/interval.conf"
        if _user_systemd()
        else Path("/etc/systemd/system/recruiting-weekly-report.timer.d/interval.conf")
    )
    dropin = Path(os.environ.get("MAXREAD_MAIL_REPORT_TIMER_DROPIN", str(default_dropin)))
    dropin.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(
        dropin,
        "[Timer]\nOnCalendar=\nOnUnitActiveSec=" + str(report_hours) + "h\nPersistent=true\nAccuracySec=5m\n",
        mode=0o644,
    )
    _run(_systemctl("daemon-reload"))
    _run(_systemctl("restart", DEFAULT_PIPELINE_SERVICE))
    _run(_systemctl("restart", DEFAULT_REPORT_TIMER))
    return {
        "ok": True,
        "scan_interval_minutes": scan_minutes,
        "report_interval_hours": report_hours,
    }


def trigger_mail_scan(account_id: str) -> dict[str, Any]:
    clean = str(account_id or "").strip().lower()
    if _remote_url():
        return _remote_request("/scan", {"account": clean})
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
    command = ["systemd-run"]
    if _user_systemd():
        command.append("--user")
    command.extend(
        [
            f"--unit={unit.removesuffix('.service')}",
            "--description=MaxRead manual recruiting mailbox scan",
            "--property=CPUQuota=60%",
            "--property=MemoryMax=700M",
            "--property=RuntimeMaxSec=1800",
            f"--setenv=HOME={Path.home()}",
            f"--setenv=MAXREAD_ROOT={Path(os.environ.get('MAXREAD_ROOT', '/opt/maxread'))}",
            f"--setenv=MAXREAD_SERVICE_HOME={Path.home()}",
            f"--setenv=MAXREAD_MAIL_SYSTEMD_USER={'1' if _user_systemd() else '0'}",
            str(script),
            clean,
        ]
    )
    result = _run(command)
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
        output = _run(_systemctl(
            "show", unit,
            "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp,NRestarts,NextElapseUSecRealtime,LastTriggerUSec,Result,ExecMainStatus",
        ))
    except Exception as exc:
        return {"ActiveState": "unknown", "error": str(exc)[:240]}
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    for key in ("ActiveEnterTimestamp", "NextElapseUSecRealtime", "LastTriggerUSec"):
        values[key + "ISO"] = _systemd_time_iso(values.get(key, ""))
    return values


def _systemd_time_iso(value: str) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", str(value or ""))
    return f"{match.group(1)}T{match.group(2)}+08:00" if match else ""


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


def _user_systemd() -> bool:
    return str(os.environ.get("MAXREAD_MAIL_SYSTEMD_USER", "")).strip().lower() in {"1", "true", "yes", "on"}


def _systemctl(*args: str) -> list[str]:
    return ["systemctl", *(["--user"] if _user_systemd() else []), *args]


def _remote_url() -> str:
    return str(os.environ.get("MAXREAD_MAIL_REMOTE_URL", "")).strip().rstrip("/")


def _remote_request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = _remote_url() + path
    token = str(os.environ.get("MAXREAD_MAIL_REMOTE_TOKEN", "")).strip()
    if not token:
        raise RuntimeError("MAXREAD_MAIL_REMOTE_TOKEN is required")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="GET" if payload is None else "POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"remote mail worker HTTP {exc.code}: {detail}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"remote mail worker unavailable: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("remote mail worker returned invalid JSON")
    return result
