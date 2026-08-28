from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .models import CandidateFields, StoredMessage


SCHEMA = """
CREATE TABLE IF NOT EXISTS recruiting_threads (
    thread_key TEXT PRIMARY KEY,
    candidate_address TEXT NOT NULL,
    normalized_subject TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    base_record_id TEXT,
    doc_id TEXT,
    doc_url TEXT,
    latest_time TEXT,
    last_incoming_time TEXT,
    last_outgoing_time TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    interview_assigned INTEGER NOT NULL DEFAULT 0,
    screening_status TEXT NOT NULL DEFAULT '未筛选',
    interview_result TEXT NOT NULL DEFAULT '未开始',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recruiting_messages (
    message_record_id INTEGER PRIMARY KEY,
    thread_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    folder TEXT NOT NULL,
    processed_at TEXT,
    FOREIGN KEY(message_record_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_recruiting_messages_thread ON recruiting_messages(thread_key);
CREATE TABLE IF NOT EXISTS recruiting_uploaded_attachments (
    thread_key TEXT NOT NULL,
    digest TEXT NOT NULL,
    filename TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    PRIMARY KEY(thread_key, digest)
);
CREATE TABLE IF NOT EXISTS recruiting_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    scanned_messages INTEGER NOT NULL DEFAULT 0,
    new_threads INTEGER NOT NULL DEFAULT 0,
    updated_threads INTEGER NOT NULL DEFAULT 0,
    failed_threads INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
"""


class PipelineStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(recruiting_threads)").fetchall()}
            if "screening_status" not in columns:
                conn.execute("ALTER TABLE recruiting_threads ADD COLUMN screening_status TEXT NOT NULL DEFAULT '未筛选'")
            if "interview_result" not in columns:
                conn.execute("ALTER TABLE recruiting_threads ADD COLUMN interview_result TEXT NOT NULL DEFAULT '未开始'")

    def start_run(self, run_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO recruiting_runs(run_id,started_at,status) VALUES (?,?,?)",
                (run_id, now, "running"),
            )

    def recover_stale_runs(self, stale_minutes: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE recruiting_runs SET status='abandoned', finished_at=?, error='stale run recovered' "
                "WHERE status='running' AND started_at < ?",
                (datetime.now(UTC).isoformat(), cutoff.isoformat()),
            )
            return int(cursor.rowcount)

    def finish_run(self, run_id: str, status: str, **counts: int | str) -> None:
        fields = {"finished_at": datetime.now(UTC).isoformat(), "status": status}
        fields.update(counts)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE recruiting_runs SET {assignments} WHERE run_id = ?",
                tuple(fields.values()) + (run_id,),
            )

    def messages(self) -> list[StoredMessage]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,source_uid,mailbox,subject,sender_name,sender_address,received_at,body_text,raw_path "
                "FROM messages ORDER BY COALESCE(received_at,'') ASC, id ASC"
            ).fetchall()
            result: list[StoredMessage] = []
            for row in rows:
                source_uid = str(row["source_uid"])
                message_dir = self.db_path.parent / "messages" / _safe_source_uid(source_uid)
                raw_path = Path(str(row["raw_path"]))
                if not raw_path.exists():
                    relocated = message_dir / "message.eml"
                    if relocated.exists():
                        raw_path = relocated
                attachments = tuple(
                    _relocated_attachment(Path(str(item["local_path"])), message_dir)
                    for item in conn.execute(
                        "SELECT local_path FROM attachments WHERE message_record_id=? AND local_path IS NOT NULL ORDER BY id",
                        (row["id"],),
                    ).fetchall()
                )
                result.append(
                    StoredMessage(
                        id=int(row["id"]),
                        source_uid=source_uid,
                        mailbox=str(row["mailbox"]),
                        subject=str(row["subject"]),
                        sender_name=str(row["sender_name"]),
                        sender_address=str(row["sender_address"]),
                        received_at=row["received_at"],
                        body_text=str(row["body_text"]),
                        raw_path=raw_path,
                        attachments=attachments,
                    )
                )
            return result

    def message_thread_keys(self) -> dict[int, str]:
        self.initialize()
        with self.connect() as conn:
            return {int(row["message_record_id"]): str(row["thread_key"]) for row in conn.execute("SELECT message_record_id,thread_key FROM recruiting_messages")}

    def message_processing_state(self) -> dict[int, tuple[str, bool]]:
        self.initialize()
        with self.connect() as conn:
            return {
                int(row["message_record_id"]): (str(row["thread_key"]), row["processed_at"] is not None)
                for row in conn.execute("SELECT message_record_id,thread_key,processed_at FROM recruiting_messages")
            }

    def upsert_message(self, message_id: int, thread_key: str, direction: str, folder: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO recruiting_messages(message_record_id,thread_key,direction,folder) VALUES (?,?,?,?) "
                "ON CONFLICT(message_record_id) DO UPDATE SET "
                "processed_at=CASE WHEN recruiting_messages.thread_key<>excluded.thread_key THEN NULL ELSE recruiting_messages.processed_at END,"
                "thread_key=excluded.thread_key,direction=excluded.direction,folder=excluded.folder",
                (message_id, thread_key, direction, folder),
            )

    def mark_message_processed(self, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE recruiting_messages SET processed_at=? WHERE message_record_id=?", (datetime.now(UTC).isoformat(), message_id))

    def uploaded_attachment_digests(self, thread_key: str) -> set[str]:
        self.initialize()
        with self.connect() as conn:
            return {str(row[0]) for row in conn.execute("SELECT digest FROM recruiting_uploaded_attachments WHERE thread_key=?", (thread_key,))}

    def mark_attachment_uploaded(self, thread_key: str, digest: str, filename: str, doc_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO recruiting_uploaded_attachments(thread_key,digest,filename,doc_id,uploaded_at) VALUES (?,?,?,?,?)",
                (thread_key, digest, filename, doc_id, datetime.now(UTC).isoformat()),
            )

    def get_thread(self, thread_key: str) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute("SELECT * FROM recruiting_threads WHERE thread_key=?", (thread_key,)).fetchone()

    def reset_base_links(
        self,
        since_days: int,
        *,
        only_candidates: bool = True,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> int:
        """Forget deleted Base rows while retaining mail, docs, and uploads.

        A missing Base mapping is itself a retry marker. Message processing
        state remains untouched so an interrupted backfill never appends old
        mail bodies or attachments to the material document again. Existing
        Base rows can still be reused by material-document URL.
        """
        self.initialize()
        current = (now or datetime.now(UTC)).astimezone(ZoneInfo("Asia/Shanghai"))
        cutoff = current - timedelta(days=max(1, int(since_days)))
        selected: list[str] = []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT thread_key,fields_json,latest_time FROM recruiting_threads"
            ).fetchall()
            for row in rows:
                latest = _parse_pipeline_time(str(row["latest_time"] or ""))
                if latest is None or latest < cutoff:
                    continue
                if only_candidates:
                    try:
                        fields = json.loads(str(row["fields_json"] or "{}"))
                    except json.JSONDecodeError:
                        fields = {}
                    if fields.get("mail_type") == "other":
                        continue
                selected.append(str(row["thread_key"]))
            if dry_run or not selected:
                return len(selected)
            placeholders = ",".join("?" for _ in selected)
            conn.execute(
                f"UPDATE recruiting_threads SET base_record_id=NULL,last_error='',status='base_backfill_pending' WHERE thread_key IN ({placeholders})",
                selected,
            )
        return len(selected)

    def save_thread(self, thread_key: str, candidate_address: str, subject: str, fields: CandidateFields, **updates: Any) -> None:
        now = datetime.now(UTC).isoformat()
        current = {
            "candidate_address": candidate_address,
            "normalized_subject": subject,
            "fields_json": json.dumps(fields.__dict__, ensure_ascii=False),
            "updated_at": now,
        }
        current.update(updates)
        with self.connect() as conn:
            existing = conn.execute("SELECT thread_key FROM recruiting_threads WHERE thread_key=?", (thread_key,)).fetchone()
            if existing:
                assignments = ", ".join(f"{key}=?" for key in current)
                conn.execute("UPDATE recruiting_threads SET " + assignments + " WHERE thread_key=?", tuple(current.values()) + (thread_key,))
            else:
                current.update({"thread_key": thread_key, "created_at": now})
                columns = ",".join(current)
                placeholders = ",".join("?" for _ in current)
                conn.execute(f"INSERT INTO recruiting_threads({columns}) VALUES ({placeholders})", tuple(current.values()))

    @staticmethod
    def fields_from_row(row: sqlite3.Row | None) -> CandidateFields | None:
        if not row or not row["fields_json"]:
            return None
        return CandidateFields(**json.loads(row["fields_json"])).normalized()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM recruiting_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()]


def _parse_pipeline_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _safe_source_uid(value: str) -> str:
    return value.replace("/", "_").replace(":", "_")


def _relocated_attachment(path: Path, message_dir: Path) -> Path:
    if path.exists():
        return path
    direct = message_dir / path.name
    if direct.exists():
        return direct
    matches = list(message_dir.glob(f"*{path.name}"))
    return matches[0] if matches else path
