from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .parser import ParsedMessage


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox TEXT NOT NULL,
    source_uid TEXT NOT NULL,
    message_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    sender_address TEXT NOT NULL,
    recipients_json TEXT NOT NULL,
    received_at TEXT,
    body_text TEXT NOT NULL,
    candidate_score INTEGER NOT NULL,
    likely_candidate INTEGER NOT NULL,
    candidate_reasons_json TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    UNIQUE(mailbox, source_uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_candidate ON messages(likely_candidate, received_at);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_record_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    is_pdf INTEGER NOT NULL,
    local_path TEXT,
    skipped_reason TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    mailbox TEXT PRIMARY KEY,
    uid_validity TEXT,
    last_uid INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SyncState:
    uid_validity: str | None
    last_uid: int


class Store:
    def __init__(self, db_path: Path, data_dir: Path, max_attachment_bytes: int) -> None:
        self.db_path = db_path
        self.data_dir = data_dir
        self.max_attachment_bytes = max_attachment_bytes

    @contextmanager
    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def get_sync_state(self, mailbox: str) -> SyncState:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute("SELECT uid_validity, last_uid FROM sync_state WHERE mailbox = ?", (mailbox,)).fetchone()
        return SyncState(row["uid_validity"], row["last_uid"]) if row else SyncState(None, 0)

    def set_sync_state(self, mailbox: str, uid_validity: str | None, last_uid: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO sync_state(mailbox, uid_validity, last_uid, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(mailbox) DO UPDATE SET
                     uid_validity=excluded.uid_validity,
                     last_uid=excluded.last_uid,
                     updated_at=excluded.updated_at""",
                (mailbox, uid_validity, last_uid, now),
            )

    def persist(self, mailbox: str, source_uid: str, message: ParsedMessage) -> tuple[int, bool]:
        self.initialize()
        # UIDs are only unique within one mailbox folder. Check the durable key
        # before touching files, then namespace new material by folder so a
        # Sent UID can never overwrite an Inbox UID with the same number.
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM messages WHERE mailbox = ? AND source_uid = ?", (mailbox, source_uid)
            ).fetchone()
            if existing:
                return int(existing["id"]), False
        folder = self.data_dir / "messages" / _safe_component(mailbox) / _safe_component(source_uid)
        folder.mkdir(parents=True, exist_ok=True)
        raw_path = folder / "message.eml"
        raw_path.write_bytes(message.raw_bytes)
        (folder / "body.txt").write_text(message.body_text, encoding="utf-8")
        now = datetime.now(UTC).isoformat()

        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO messages(
                     mailbox, source_uid, message_id, subject, sender_name, sender_address,
                     recipients_json, received_at, body_text, candidate_score,
                     likely_candidate, candidate_reasons_json, raw_path, scanned_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mailbox,
                    source_uid,
                    message.message_id,
                    message.subject,
                    message.sender_name,
                    message.sender_address,
                    json.dumps(message.recipients, ensure_ascii=False),
                    message.received_at,
                    message.body_text,
                    message.candidate_score,
                    int(message.likely_candidate),
                    json.dumps(message.candidate_reasons, ensure_ascii=False),
                    str(raw_path),
                    now,
                ),
            )
            message_record_id = int(cursor.lastrowid)
            for index, attachment in enumerate(message.attachments, start=1):
                local_path: str | None = None
                skipped_reason: str | None = None
                if len(attachment.payload) > self.max_attachment_bytes:
                    skipped_reason = "attachment_too_large"
                else:
                    target = folder / f"{index:02d}-{attachment.filename}"
                    target.write_bytes(attachment.payload)
                    local_path = str(target)
                connection.execute(
                    """INSERT INTO attachments(
                         message_record_id, filename, content_type, size_bytes, sha256,
                         is_pdf, local_path, skipped_reason
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        message_record_id,
                        attachment.filename,
                        attachment.content_type,
                        len(attachment.payload),
                        attachment.sha256,
                        int(attachment.is_pdf),
                        local_path,
                        skipped_reason,
                    ),
                )
            return message_record_id, True
    def summary(self) -> dict[str, int]:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN likely_candidate = 1 THEN 1 ELSE 0 END) likely_candidates,
                          SUM(CASE WHEN likely_candidate = 0 THEN 1 ELSE 0 END) other_messages
                   FROM messages"""
            ).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "likely_candidates", "other_messages")}

    def recent(self, limit: int) -> list[dict[str, object]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, received_at, sender_address, subject, likely_candidate, candidate_score
                   FROM messages ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def _safe_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return clean.strip("._") or "unknown"
