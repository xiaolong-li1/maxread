from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Store
from .feishu import FeishuClient


MEMBER_RE = re.compile(r"^(?:(?P<name>[^=:@]+)\s*[=:]\s*)?(?P<user_id>ou_[A-Za-z0-9_-]+)$")


def parse_roster_members(values: Iterable[str]) -> List[dict]:
    members = []
    seen = set()
    for raw in values:
        for item in str(raw).replace("\n", ",").split(","):
            item = item.strip()
            if not item:
                continue
            match = MEMBER_RE.match(item)
            if match:
                name = (match.group("name") or match.group("user_id")).strip()
                member_key = match.group("user_id")
            else:
                name = item
                member_key = item
            if member_key in seen:
                continue
            seen.add(member_key)
            members.append({"name": name, "user_id": member_key})
    if not members:
        raise ValueError("At least one duty member is required")
    return members


def _zone(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.duty_timezone)
    except Exception as exc:
        raise ValueError(f"Invalid duty timezone: {settings.duty_timezone}") from exc


def duty_member_for_date(store: Store, reminder_date: date) -> Optional[dict]:
    roster = store.list_duty_roster(enabled_only=True)
    if not roster:
        return None
    start_text = store.get_duty_setting("rotation_start_date", "")
    try:
        start = date.fromisoformat(start_text) if start_text else reminder_date
    except ValueError:
        start = reminder_date
    index = (reminder_date - start).days % len(roster)
    return roster[index]


def duty_members_for_dates(store: Store, *dates: date) -> List[Optional[dict]]:
    return [duty_member_for_date(store, target) for target in dates]


def ensure_rotation_start(store: Store, today: date) -> None:
    if not store.get_duty_setting("rotation_start_date", ""):
        store.set_duty_setting("rotation_start_date", today.isoformat())
        store.conn.commit()


def reminder_text(member: dict, next_member: Optional[dict], reminder_date: date) -> str:
    tomorrow = next_member["name"] if next_member else "未配置"
    return (
        "【MaxRead 值班提醒】\n"
        f"今天（{reminder_date.isoformat()}）值班：{member['name']}\n"
        f"明天（{(reminder_date + timedelta(days=1)).isoformat()}）值班：{tomorrow}\n"
        "请留意 MaxRead 的运行状态和用户反馈；出现读不动、任务失败或服务异常时及时处理。"
    )


def send_duty_reminder(settings: Settings, reminder_date: date, dry_run: bool = False) -> dict:
    store = Store(settings.db_path)
    try:
        ensure_rotation_start(store, reminder_date)
        member = duty_member_for_date(store, reminder_date)
        if not member:
            return {"status": "not_configured", "date": reminder_date.isoformat()}
        next_member = duty_member_for_date(store, reminder_date + timedelta(days=1))
        existing = store.get_duty_reminder(reminder_date.isoformat())
        if existing and existing["status"] == "sent":
            return {"status": "already_sent", "date": reminder_date.isoformat(), "member": member}
        if dry_run:
            return {
                "status": "dry_run",
                "date": reminder_date.isoformat(),
                "member": member,
                "next_member": next_member,
                "text": reminder_text(member, next_member, reminder_date),
            }
        if not settings.duty_chat_id:
            return {"status": "chat_not_configured", "date": reminder_date.isoformat(), "member": member}
        if not store.reserve_duty_reminder(reminder_date.isoformat(), member):
            return {"status": "already_sent", "date": reminder_date.isoformat(), "member": member}
        feishu = FeishuClient(settings.lark_cli, settings.feishu_as)
        key = f"maxread-duty:{reminder_date.isoformat()}:{settings.duty_chat_id}"
        try:
            result = feishu.send_text_to_chat(settings.duty_chat_id, reminder_text(member, next_member, reminder_date), key)
        except Exception as exc:
            store.fail_duty_reminder(reminder_date.isoformat(), str(exc))
            return {"status": "failed", "date": reminder_date.isoformat(), "member": member, "error": str(exc)}
        store.complete_duty_reminder(reminder_date.isoformat(), str(result.get("message_id", "")))
        return {"status": "sent", "date": reminder_date.isoformat(), "member": member, "result": result}
    finally:
        store.close()


def run_duty_daemon(settings: Settings) -> None:
    interval = max(15, int(settings.duty_poll_seconds))
    while True:
        now = datetime.now(_zone(settings))
        current_minutes = now.hour * 60 + now.minute
        target_minutes = settings.duty_hour * 60 + settings.duty_minute
        if current_minutes >= target_minutes:
            send_duty_reminder(settings, now.date())
        time.sleep(interval)


def run_duty_command(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    try:
        if args.action == "set":
            members = parse_roster_members(args.member)
            store.replace_duty_roster(members)
            print(json.dumps({"ok": True, "roster": store.list_duty_roster()}, ensure_ascii=False, indent=2))
            return 0
        if args.action == "list":
            print(json.dumps(store.list_duty_roster(), ensure_ascii=False, indent=2))
            return 0
        if args.action == "today":
            today = datetime.now(_zone(settings)).date()
            ensure_rotation_start(store, today)
            print(json.dumps({"date": today.isoformat(), "member": duty_member_for_date(store, today)}, ensure_ascii=False, indent=2))
            return 0
        if args.action == "history":
            print(json.dumps(store.list_duty_reminders(args.limit), ensure_ascii=False, indent=2))
            return 0
    finally:
        store.close()
    if args.action == "send":
        target = date.fromisoformat(args.date) if args.date else datetime.now(_zone(settings)).date()
        print(json.dumps(send_duty_reminder(settings, target, args.dry_run), ensure_ascii=False, indent=2))
        return 0
    if args.action == "daemon":
        run_duty_daemon(settings)
        return 0
    return 2
