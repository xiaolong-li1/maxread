#!/usr/bin/env python3
"""Independent daily duty reminder for MaxRead.

This process deliberately has no import dependency on the main MaxRead app.
It only reads its JSON configuration, keeps idempotency state in its own
SQLite database, and invokes the existing lark-cli command when enabled.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


STOP = False


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = ("chat_id", "lark_cli", "roster", "rotation_start_date", "timezone")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError("missing config keys: " + ", ".join(missing))
    if not isinstance(config["roster"], list) or not config["roster"]:
        raise ValueError("roster must contain at least one member")
    if any(not isinstance(name, str) or not name.strip() for name in config["roster"]):
        raise ValueError("roster contains an empty member")
    date.fromisoformat(config["rotation_start_date"])
    ZoneInfo(config["timezone"])
    config["hour"] = int(config.get("hour", 7))
    config["minute"] = int(config.get("minute", 0))
    config["poll_seconds"] = max(15, int(config.get("poll_seconds", 30)))
    config["send_timeout_seconds"] = max(10, int(config.get("send_timeout_seconds", 90)))
    if not 0 <= config["hour"] <= 23 or not 0 <= config["minute"] <= 59:
        raise ValueError("reminder time is outside 00:00-23:59")
    return config


def member_for(config: dict, target: date) -> str:
    start = date.fromisoformat(config["rotation_start_date"])
    roster = config["roster"]
    return roster[(target - start).days % len(roster)]


def reminder_text(config: dict, target: date) -> str:
    tomorrow = target + timedelta(days=1)
    message = (
        "【MaxRead 值班提醒】\n\n"
        "---\n\n"
        f"**今天（{target.isoformat()}）值班：{member_for(config, target)}**\n"
        f"明天（{tomorrow.isoformat()}）值班：{member_for(config, tomorrow)}"
    )
    suffix = config.get("message_suffix", "").strip()
    return f"{message}\n\n{suffix}" if suffix else message


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute(
        """
        create table if not exists duty_reminders (
            reminder_date text primary key,
            status text not null,
            attempts integer not null default 0,
            message_id text not null default '',
            error text not null default '',
            updated_at text not null
        )
        """
    )
    conn.commit()
    return conn


def reserve(conn: sqlite3.Connection, target: date) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("begin immediate")
    row = conn.execute(
        "select status from duty_reminders where reminder_date = ?", (target.isoformat(),)
    ).fetchone()
    if row and row[0] == "sent":
        conn.commit()
        return False
    if row:
        conn.execute(
            "update duty_reminders set status = 'sending', attempts = attempts + 1, error = '', updated_at = ? where reminder_date = ?",
            (now, target.isoformat()),
        )
    else:
        conn.execute(
            "insert into duty_reminders (reminder_date, status, attempts, updated_at) values (?, 'sending', 1, ?)",
            (target.isoformat(), now),
        )
    conn.commit()
    return True


def mark_result(conn: sqlite3.Connection, target: date, *, message_id: str = "", error: str = "") -> None:
    status = "failed" if error else "sent"
    conn.execute(
        "update duty_reminders set status = ?, message_id = ?, error = ?, updated_at = ? where reminder_date = ?",
        (status, message_id, error, datetime.now().isoformat(timespec="seconds"), target.isoformat()),
    )
    conn.commit()


def send(config: dict, target: date, *, dry_run: bool = False) -> dict:
    text = reminder_text(config, target)
    result = {"date": target.isoformat(), "member": member_for(config, target), "text": text}
    if dry_run:
        result["status"] = "dry_run"
        return result

    db_path = Path(config["state_db"])
    conn = init_db(db_path)
    try:
        if not reserve(conn, target):
            result["status"] = "already_sent"
            return result
        chat_fingerprint = hashlib.sha256(config["chat_id"].encode("utf-8")).hexdigest()[:12]
        key = f"maxread-duty:{target.isoformat()}:{chat_fingerprint}"
        args = [
            config["lark_cli"],
            "im",
            "+messages-send",
            "--as",
            config.get("identity", "bot"),
            "--chat-id",
            config["chat_id"],
            "--markdown",
            text,
            "--idempotency-key",
            key,
            "--format",
            "json",
        ]
        try:
            completed = subprocess.run(
                args,
                check=True,
                capture_output=True,
                text=True,
                timeout=config["send_timeout_seconds"],
            )
            payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
            message_id = str(payload.get("data", {}).get("message_id", ""))
            mark_result(conn, target, message_id=message_id)
            result.update({"status": "sent", "message_id": message_id})
            return result
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            error = f"lark-cli exited with status {exc.returncode}"
            if detail:
                error += f": {detail[-1000:]}"
            mark_result(conn, target, error=error)
            result.update({"status": "failed", "error": error})
            return result
        except subprocess.TimeoutExpired:
            error = f"lark-cli timed out after {config['send_timeout_seconds']} seconds"
            mark_result(conn, target, error=error)
            result.update({"status": "failed", "error": error})
            return result
        except Exception as exc:
            mark_result(conn, target, error=str(exc))
            result.update({"status": "failed", "error": str(exc)})
            return result
    finally:
        conn.close()


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def now(config: dict) -> datetime:
    return datetime.now(ZoneInfo(config["timezone"]))


def daemon(config: dict) -> None:
    global STOP
    lock = acquire_lock(Path(config["lock_file"]))
    try:
        while not STOP:
            current = now(config)
            target = config["hour"] * 60 + config["minute"]
            if current.hour * 60 + current.minute >= target:
                print(json.dumps(send(config, current.date()), ensure_ascii=False), flush=True)
            time.sleep(config["poll_seconds"])
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MaxRead independent duty reminder")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--date", default="", help="YYYY-MM-DD; defaults to current configured timezone")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true", help="Send one reminder; requires explicit invocation")
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.daemon:
        daemon(config)
        return 0
    target = date.fromisoformat(args.date) if args.date else now(config).date()
    result = send(config, target, dry_run=args.dry_run or not args.send)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] != "failed" else 1


def _stop(_signum, _frame):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BlockingIOError:
        print("duty reminder is already running", file=sys.stderr)
        raise SystemExit(2)
