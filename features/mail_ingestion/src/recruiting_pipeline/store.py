from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections import defaultdict
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
CREATE TABLE IF NOT EXISTS recruiting_sync_state (
    sync_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT ''
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
            message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if message_columns and "artifacts_released_at" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN artifacts_released_at TEXT")

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
            attachment_rows = conn.execute(
                "SELECT message_record_id,local_path FROM attachments "
                "WHERE local_path IS NOT NULL ORDER BY message_record_id,id"
            ).fetchall()
            attachments_by_message: dict[int, list[Path]] = defaultdict(list)
            for item in attachment_rows:
                attachments_by_message[int(item["message_record_id"])].append(Path(str(item["local_path"])))
            result: list[StoredMessage] = []
            for row in rows:
                source_uid = str(row["source_uid"])
                stored_mailbox = str(row["mailbox"] or "")
                if "::" in stored_mailbox:
                    source_account, display_mailbox = stored_mailbox.split("::", 1)
                else:
                    source_account, display_mailbox = "", stored_mailbox
                message_dir = self.db_path.parent / "messages" / _safe_source_uid(stored_mailbox) / _safe_source_uid(source_uid)
                legacy_message_dir = self.db_path.parent / "messages" / _safe_source_uid(source_uid)
                raw_path = Path(str(row["raw_path"]))
                if not raw_path.exists():
                    relocated = message_dir / "message.eml"
                    legacy = legacy_message_dir / "message.eml"
                    raw_path = relocated if relocated.exists() else legacy if legacy.exists() else raw_path
                attachments = tuple(
                    _relocated_attachment(_relocated_attachment(path, message_dir), legacy_message_dir)
                    for path in attachments_by_message.get(int(row["id"]), [])
                )
                result.append(
                    StoredMessage(
                        id=int(row["id"]),
                        source_uid=source_uid,
                        mailbox=display_mailbox,
                        subject=str(row["subject"]),
                        sender_name=str(row["sender_name"]),
                        sender_address=str(row["sender_address"]),
                        received_at=row["received_at"],
                        body_text=str(row["body_text"]),
                        raw_path=raw_path,
                        attachments=attachments,
                        source_account=source_account,
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
        self.upsert_messages([(message_id, thread_key, direction, folder)])

    def upsert_messages(self, rows: Iterable[tuple[int, str, str, str]]) -> None:
        values = list(rows)
        if not values:
            return
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO recruiting_messages(message_record_id,thread_key,direction,folder) VALUES (?,?,?,?) "
                "ON CONFLICT(message_record_id) DO UPDATE SET "
                "processed_at=CASE WHEN recruiting_messages.thread_key<>excluded.thread_key THEN NULL ELSE recruiting_messages.processed_at END,"
                "thread_key=excluded.thread_key,direction=excluded.direction,folder=excluded.folder",
                values,
            )

    def mark_message_processed(self, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE recruiting_messages SET processed_at=? WHERE message_record_id=?", (datetime.now(UTC).isoformat(), message_id))

    def release_processed_artifacts(self) -> int:
        """Release bulky local payloads only after their durable thread commit."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                select m.id,m.raw_path from messages m
                join recruiting_messages r on r.message_record_id=m.id
                where r.processed_at is not null and m.artifacts_released_at is null
                order by m.id
                """
            ).fetchall()
        released = 0
        for row in rows:
            if self._release_message_artifacts(int(row["id"]), Path(str(row["raw_path"]))):
                released += 1
        return released

    def mark_duplicate_messages_processed(self) -> int:
        """Close physical mailbox copies once the canonical Message-ID succeeded."""
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                update recruiting_messages as pending
                set processed_at=?
                where pending.processed_at is null
                  and exists (
                    select 1
                    from messages current_message
                    join messages completed_message
                      on completed_message.message_id=current_message.message_id
                     and completed_message.id<>current_message.id
                    join recruiting_messages completed
                      on completed.message_record_id=completed_message.id
                     and completed.processed_at is not null
                    where current_message.id=pending.message_record_id
                  )
                """,
                (now,),
            )
            return int(cursor.rowcount)

    def _release_message_artifacts(self, message_id: int, raw_path: Path) -> bool:
        with self.connect() as conn:
            attachments = [
                Path(str(row["local_path"]))
                for row in conn.execute(
                    "select local_path from attachments where message_record_id=? and local_path is not null",
                    (message_id,),
                ).fetchall()
            ]
        try:
            if raw_path.exists():
                _compact_rfc822_headers(raw_path)
            for path in attachments:
                path.unlink(missing_ok=True)
            (raw_path.parent / "body.txt").unlink(missing_ok=True)
            shutil.rmtree(raw_path.parent / "external-attachments", ignore_errors=True)
        except OSError:
            return False
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                "update attachments set local_path=null,skipped_reason=coalesce(skipped_reason,'processed_cleanup') where message_record_id=?",
                (message_id,),
            )
            conn.execute("update messages set artifacts_released_at=? where id=?", (now, message_id))
        return True

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

    def list_threads(self) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return conn.execute("SELECT * FROM recruiting_threads ORDER BY latest_time DESC, thread_key").fetchall()

    def apply_base_snapshot(
        self,
        states: dict[str, dict[str, Any]],
        *,
        snapshot_started_at: str,
    ) -> dict[str, int]:
        """Refresh the local read model from a complete authoritative Base snapshot."""
        from .base_sync import merge_base_profile

        self.initialize()
        counts = {
            "remote_records": len(states),
            "matched": 0,
            "updated": 0,
            "linked": 0,
            "skipped_pending": 0,
            "skipped_newer_local": 0,
        }
        document_index: dict[str, str] = {}
        identity_index: dict[tuple[str, str], list[str]] = defaultdict(list)
        for record_id, state in states.items():
            document_url = str(state.get("document_url") or "").strip()
            if document_url:
                document_index[document_url] = record_id
            identity_index[(
                str(state.get("name") or "").strip(),
                _minute_key(str(state.get("latest_time") or "")),
            )].append(record_id)

        with self.connect() as conn:
            pending_threads: set[str] = set()
            if conn.execute(
                "select 1 from sqlite_master where type='table' and name='recruiting_admin_actions'"
            ).fetchone():
                pending_threads = {
                    str(row[0])
                    for row in conn.execute(
                        "select distinct thread_key from recruiting_admin_actions where status in ('pending','syncing')"
                    )
                }
            rows = conn.execute("select * from recruiting_threads where status<>'inactive'").fetchall()
            now = datetime.now(UTC).isoformat()
            for row in rows:
                thread_key = str(row["thread_key"])
                if thread_key in pending_threads:
                    counts["skipped_pending"] += 1
                    continue
                if _is_after(str(row["updated_at"] or ""), snapshot_started_at):
                    counts["skipped_newer_local"] += 1
                    continue
                record_id = str(row["base_record_id"] or "").strip()
                if not record_id:
                    local_url = str(row["doc_url"] or "").strip()
                    record_id = document_index.get(local_url, "") if local_url else ""
                    if not record_id:
                        try:
                            local_fields = json.loads(str(row["fields_json"] or "{}"))
                        except json.JSONDecodeError:
                            local_fields = {}
                        candidates = identity_index.get((
                            str(local_fields.get("name") or "").strip(),
                            _minute_key(str(row["latest_time"] or "")),
                        ), [])
                        record_id = candidates[0] if len(candidates) == 1 else ""
                state = states.get(record_id)
                if not state:
                    continue
                counts["matched"] += 1
                fields = self.fields_from_row(row)
                if fields is None:
                    continue
                merged = merge_base_profile(fields, state)
                screening_status = str(state.get("screening_status") or row["screening_status"] or "未筛选")
                interview_result = str(state.get("interview_result") or row["interview_result"] or "未开始")
                interview_assigned = int(bool(state.get("interview_assigned")))
                document_url = str(state.get("document_url") or row["doc_url"] or "")
                desired = (
                    json.dumps(merged.__dict__, ensure_ascii=False),
                    record_id,
                    document_url,
                    screening_status,
                    interview_assigned,
                    interview_result,
                )
                current = (
                    str(row["fields_json"] or ""),
                    str(row["base_record_id"] or ""),
                    str(row["doc_url"] or ""),
                    str(row["screening_status"] or ""),
                    int(row["interview_assigned"] or 0),
                    str(row["interview_result"] or ""),
                )
                if desired == current:
                    continue
                if not current[1] and record_id:
                    counts["linked"] += 1
                conn.execute(
                    """
                    update recruiting_threads
                    set fields_json=?,base_record_id=?,doc_url=?,screening_status=?,
                        interview_assigned=?,interview_result=?,updated_at=?
                    where thread_key=?
                    """,
                    desired + (now, thread_key),
                )
                counts["updated"] += 1
        return counts

    def record_sync_state(
        self,
        sync_key: str,
        *,
        status: str,
        started_at: str,
        details: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        self.initialize()
        finished_at = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                insert into recruiting_sync_state(
                    sync_key,status,details_json,started_at,finished_at,last_error
                ) values(?,?,?,?,?,?)
                on conflict(sync_key) do update set
                    status=excluded.status,details_json=excluded.details_json,
                    started_at=excluded.started_at,finished_at=excluded.finished_at,
                    last_error=excluded.last_error
                """,
                (
                    sync_key,
                    status,
                    json.dumps(details or {}, ensure_ascii=False),
                    started_at,
                    finished_at,
                    str(error or "")[:1000],
                ),
            )

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


def _minute_key(value: str) -> str:
    parsed = _parse_pipeline_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else value[:16].replace("T", " ")


def _is_after(value: str, boundary: str) -> bool:
    try:
        left = datetime.fromisoformat(value.replace("Z", "+00:00"))
        right = datetime.fromisoformat(boundary.replace("Z", "+00:00"))
    except ValueError:
        return False
    if left.tzinfo is None:
        left = left.replace(tzinfo=UTC)
    if right.tzinfo is None:
        right = right.replace(tzinfo=UTC)
    return left.astimezone(UTC) > right.astimezone(UTC)


def _compact_rfc822_headers(path: Path) -> None:
    payload = path.read_bytes()
    separators = [index for marker in (b"\r\n\r\n", b"\n\n") if (index := payload.find(marker)) >= 0]
    if not separators:
        raise OSError(f"message headers are incomplete: {path}")
    end = min(separators)
    compact = payload[:end].rstrip(b"\r\n") + b"\r\n\r\n"
    temporary = path.with_name(path.name + ".headers.tmp")
    temporary.write_bytes(compact)
    os.chmod(temporary, path.stat().st_mode & 0o777)
    os.replace(temporary, path)


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
