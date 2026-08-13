from datetime import date
from pathlib import Path
from types import SimpleNamespace

from maxread.db import Store
import maxread.duty as duty
from maxread.duty import duty_member_for_date, parse_roster_members, reminder_text, send_duty_reminder


def test_parse_roster_accepts_names_and_optional_ids():
    members = parse_roster_members(["张三", "李四=ou_2", "ou_3"])
    assert members == [
        {"name": "张三", "user_id": "张三"},
        {"name": "李四", "user_id": "ou_2"},
        {"name": "ou_3", "user_id": "ou_3"},
    ]


def test_duty_rotation_is_daily_and_stable(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.replace_duty_roster([{"name": "张三", "user_id": "张三"}, {"name": "李四", "user_id": "李四"}])
    store.set_duty_setting("rotation_start_date", "2026-08-12")
    store.conn.commit()

    assert duty_member_for_date(store, date(2026, 8, 12))["name"] == "张三"
    assert duty_member_for_date(store, date(2026, 8, 13))["name"] == "李四"
    assert duty_member_for_date(store, date(2026, 8, 14))["name"] == "张三"
    store.close()


def test_send_duty_reminder_dry_run_does_not_write_send_record(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.replace_duty_roster([{"name": "张三", "user_id": "张三"}])
    store.close()
    settings = SimpleNamespace(
        db_path=Path(tmp_path) / "maxread.sqlite3",
        duty_timezone="Asia/Shanghai",
        duty_chat_id="",
        lark_cli="lark-cli",
        feishu_as="bot",
    )

    result = send_duty_reminder(settings, date(2026, 8, 12), dry_run=True)

    assert result["status"] == "dry_run"
    assert "张三" in result["text"]
    store = Store(settings.db_path)
    assert store.list_duty_reminders() == []
    store.close()


def test_send_duty_reminder_requires_chat_before_sending(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.replace_duty_roster([{"name": "张三", "user_id": "张三"}])
    store.close()
    settings = SimpleNamespace(
        db_path=Path(tmp_path) / "maxread.sqlite3",
        duty_timezone="Asia/Shanghai",
        duty_chat_id="",
        lark_cli="lark-cli",
        feishu_as="bot",
    )

    result = send_duty_reminder(settings, date(2026, 8, 12))

    assert result["status"] == "chat_not_configured"
    assert "今天" in reminder_text(result["member"], None, date(2026, 8, 12))


def test_send_duty_reminder_posts_to_configured_group_only(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.replace_duty_roster([{"name": "张三", "user_id": "张三"}])
    store.close()
    calls = []

    class FakeFeishu:
        def __init__(self, cli, identity):
            pass

        def send_text_to_chat(self, chat_id, text, idempotency_key):
            calls.append((chat_id, text, idempotency_key))
            return {"message_id": "om_duty"}

    original = duty.FeishuClient
    duty.FeishuClient = FakeFeishu
    try:
        settings = SimpleNamespace(
            db_path=Path(tmp_path) / "maxread.sqlite3",
            duty_timezone="Asia/Shanghai",
            duty_chat_id="oc_duty",
            lark_cli="lark-cli",
            feishu_as="bot",
        )
        first = send_duty_reminder(settings, date(2026, 8, 12))
        second = send_duty_reminder(settings, date(2026, 8, 12))
    finally:
        duty.FeishuClient = original

    assert first["status"] == "sent"
    assert second["status"] == "already_sent"
    assert len(calls) == 1
    assert calls[0][0] == "oc_duty"
    assert "张三" in calls[0][1]
    assert "明天" in calls[0][1]
