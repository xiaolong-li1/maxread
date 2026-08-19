from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .workflow import (
    InvalidWorkflowTransition,
    PublishedCheckpoint,
    WorkflowEvent,
    WorkflowState,
    queue_status_for_state,
    state_from_legacy,
    transition,
)


class QueueLeaseLostError(RuntimeError):
    """Raised when a worker tries to mutate a job after losing its lease."""


@dataclass
class PaperRecord:
    paper_id: str
    status: str
    title: str = ""
    doc_url: str = ""
    doc_token: str = ""
    error: str = ""


@dataclass
class DocumentRecord:
    doc_id: str
    status: str
    kind: str = ""
    title: str = ""
    source_url: str = ""
    doc_url: str = ""
    doc_token: str = ""
    error: str = ""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.init()

    def init(self) -> None:
        self.conn.executescript(
            """
            create table if not exists papers (
                paper_id text primary key,
                title text not null default '',
                authors text not null default '',
                arxiv_url text not null default '',
                pdf_path text not null default '',
                source_path text not null default '',
                status text not null,
                doc_url text not null default '',
                doc_token text not null default '',
                error text not null default '',
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            );

            create table if not exists jobs (
                event_id text not null,
                message_id text not null,
                chat_id text not null,
                paper_id text not null,
                status text not null,
                created_at datetime not null default current_timestamp,
                primary key (event_id, paper_id)
            );

            create table if not exists documents (
                doc_id text primary key,
                kind text not null default '',
                source_url text not null default '',
                title text not null default '',
                status text not null,
                doc_url text not null default '',
                doc_token text not null default '',
                error text not null default '',
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            );

            create table if not exists users (
                sender_id text primary key,
                intro_sent integer not null default 0,
                first_seen_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            );

            create table if not exists user_identity_cache (
                sender_id text primary key,
                display_name text not null default '',
                updated_at datetime not null default current_timestamp
            );

            create table if not exists feedback (
                id integer primary key autoincrement,
                event_id text not null default '',
                message_id text not null default '',
                chat_id text not null default '',
                chat_type text not null default '',
                sender_id text not null default '',
                content text not null default '',
                status text not null default 'new',
                feedback_source text not null default '',
                feedback_category text not null default '',
                feedback_confidence real not null default 0,
                created_at datetime not null default current_timestamp
            );

            create table if not exists usage_events (
                id integer primary key autoincrement,
                event_id text not null default '',
                message_id text not null default '',
                chat_id text not null default '',
                chat_type text not null default '',
                sender_id text not null default '',
                source_kind text not null default '',
                source_id text not null default '',
                source_url text not null default '',
                title text not null default '',
                status text not null default '',
                doc_url text not null default '',
                error text not null default '',
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            );

            create index if not exists usage_events_sender_created_idx on usage_events(sender_id, created_at);
            create index if not exists usage_events_source_idx on usage_events(source_kind, source_id);

            create table if not exists queue_jobs (
                id integer primary key autoincrement,
                dedupe_key text not null,
                source_kind text not null default '',
                source_id text not null default '',
                source_url text not null default '',
                status text not null default 'queued',
                priority integer not null default 0,
                attempts integer not null default 0,
                title text not null default '',
                doc_url text not null default '',
                error text not null default '',
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp,
                started_at datetime,
                finished_at datetime
            );

            create table if not exists job_watchers (
                id integer primary key autoincrement,
                job_id integer not null,
                event_id text not null default '',
                message_id text not null default '',
                chat_id text not null default '',
                chat_type text not null default '',
                sender_id text not null default '',
                usage_event_id integer not null default 0,
                notified integer not null default 0,
                created_at datetime not null default current_timestamp
            );

            create table if not exists job_events (
                id integer primary key autoincrement,
                job_id integer not null default 0,
                event_type text not null default '',
                detail text not null default '',
                created_at datetime not null default current_timestamp
            );

            create index if not exists queue_jobs_active_idx on queue_jobs(status, priority, id);
            create index if not exists queue_jobs_dedupe_idx on queue_jobs(dedupe_key, status);
            create table if not exists review_issues (
                id integer primary key autoincrement,
                source_kind text not null default '',
                source_id text not null default '',
                category text not null default '',
                severity text not null default '',
                detail text not null default '',
                created_at datetime not null default current_timestamp
            );

            create index if not exists job_watchers_job_idx on job_watchers(job_id, notified);
            create index if not exists job_events_job_idx on job_events(job_id, id);
            create index if not exists review_issues_source_idx on review_issues(source_kind, source_id, id);
            create index if not exists review_issues_category_idx on review_issues(category, severity);

            create table if not exists duty_roster (
                id integer primary key autoincrement,
                ordinal integer not null,
                name text not null default '',
                user_id text not null,
                enabled integer not null default 1,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            );

            create unique index if not exists duty_roster_user_idx on duty_roster(user_id);
            create index if not exists duty_roster_order_idx on duty_roster(enabled, ordinal, id);

            create table if not exists duty_settings (
                key text primary key,
                value text not null default '',
                updated_at datetime not null default current_timestamp
            );

            create table if not exists duty_reminders (
                reminder_date text primary key,
                roster_id integer not null,
                name text not null default '',
                user_id text not null,
                status text not null default 'pending',
                message_id text not null default '',
                error text not null default '',
                attempts integer not null default 0,
                sent_at datetime,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            );

            create index if not exists duty_reminders_status_idx on duty_reminders(status, reminder_date);
            """
        )
        self.conn.commit()
        self._ensure_column("queue_jobs", "worker_id", "text not null default ''")
        self._ensure_column("queue_jobs", "heartbeat_at", "datetime")
        self._ensure_column("queue_jobs", "stage", "text not null default ''")
        self._ensure_column("queue_jobs", "stage_updated_at", "datetime")
        self._ensure_column("queue_jobs", "workflow_state", "text not null default ''")
        self._ensure_column("queue_jobs", "state_version", "integer not null default 0")
        self._ensure_column("queue_jobs", "last_event", "text not null default ''")
        self._ensure_column("queue_jobs", "checkpoint_json", "text not null default ''")
        self._ensure_column("feedback", "feedback_source", "text not null default ''")
        self._ensure_column("feedback", "feedback_category", "text not null default ''")
        self._ensure_column("feedback", "feedback_confidence", "real not null default 0")
        self.conn.executescript(
            """
            create index if not exists queue_jobs_claim_idx on queue_jobs(status, priority desc, id asc);
            create index if not exists queue_jobs_heartbeat_idx on queue_jobs(status, heartbeat_at);
            create index if not exists queue_jobs_worker_idx on queue_jobs(status, worker_id);
            """
        )
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"pragma table_info({table})").fetchall()}
        if column in columns:
            return
        self.conn.execute(f"alter table {table} add column {column} {definition}")
        self.conn.commit()

    def get_paper(self, paper_id: str) -> Optional[PaperRecord]:
        row = self.conn.execute("select * from papers where paper_id = ?", (paper_id,)).fetchone()
        if row is None:
            return None
        return PaperRecord(
            paper_id=row["paper_id"],
            status=row["status"],
            title=row["title"],
            doc_url=row["doc_url"],
            doc_token=row["doc_token"],
            error=row["error"],
        )

    def upsert_paper(self, paper_id: str, status: str, **fields: str) -> None:
        existing = self.get_paper(paper_id)
        names = {"title", "authors", "arxiv_url", "pdf_path", "source_path", "doc_url", "doc_token", "error"}
        clean = {k: str(v) for k, v in fields.items() if k in names and v is not None}
        if existing is None:
            columns = ["paper_id", "status"] + list(clean.keys())
            values = [paper_id, status] + list(clean.values())
            placeholders = ",".join("?" for _ in values)
            self.conn.execute(
                f"insert into papers ({','.join(columns)}) values ({placeholders})",
                values,
            )
        else:
            assignments = ["status = ?", "updated_at = current_timestamp"] + [f"{k} = ?" for k in clean]
            values = [status] + list(clean.values()) + [paper_id]
            self.conn.execute(f"update papers set {','.join(assignments)} where paper_id = ?", values)
        self.conn.commit()

    def add_job(self, event_id: str, message_id: str, chat_id: str, paper_id: str, status: str) -> bool:
        try:
            self.conn.execute(
                "insert into jobs (event_id, message_id, chat_id, paper_id, status) values (?, ?, ?, ?, ?)",
                (event_id, message_id, chat_id, paper_id, status),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_document(self, doc_id: str) -> Optional[DocumentRecord]:
        row = self.conn.execute("select * from documents where doc_id = ?", (doc_id,)).fetchone()
        if row is None:
            return None
        return DocumentRecord(
            doc_id=row["doc_id"],
            kind=row["kind"],
            source_url=row["source_url"],
            title=row["title"],
            status=row["status"],
            doc_url=row["doc_url"],
            doc_token=row["doc_token"],
            error=row["error"],
        )

    def upsert_document(self, doc_id: str, status: str, **fields: str) -> None:
        existing = self.get_document(doc_id)
        names = {"kind", "source_url", "title", "doc_url", "doc_token", "error"}
        clean = {k: str(v) for k, v in fields.items() if k in names and v is not None}
        if existing is None:
            columns = ["doc_id", "status"] + list(clean.keys())
            values = [doc_id, status] + list(clean.values())
            placeholders = ",".join("?" for _ in values)
            self.conn.execute(f"insert into documents ({','.join(columns)}) values ({placeholders})", values)
        else:
            assignments = ["status = ?", "updated_at = current_timestamp"] + [f"{k} = ?" for k in clean]
            values = [status] + list(clean.values()) + [doc_id]
            self.conn.execute(f"update documents set {','.join(assignments)} where doc_id = ?", values)
        self.conn.commit()


    def should_send_intro_to_user(self, sender_id: str) -> bool:
        row = self.conn.execute("select intro_sent from users where sender_id = ?", (sender_id,)).fetchone()
        return row is None or int(row["intro_sent"]) == 0

    def mark_intro_sent(self, sender_id: str) -> None:
        self.conn.execute(
            """
            insert into users (sender_id, intro_sent) values (?, 1)
            on conflict(sender_id) do update set intro_sent = 1, updated_at = current_timestamp
            """,
            (sender_id,),
        )
        self.conn.commit()

    def get_user_names(self, sender_ids):
        ids = sorted({str(sender_id) for sender_id in sender_ids if str(sender_id)})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"select sender_id, display_name from user_identity_cache where sender_id in ({placeholders})",
            ids,
        ).fetchall()
        return {row["sender_id"]: row["display_name"] for row in rows if row["display_name"]}

    def save_user_names(self, names) -> None:
        clean = {
            str(sender_id): str(display_name).strip()
            for sender_id, display_name in names.items()
            if str(sender_id).strip() and str(display_name).strip()
        }
        if not clean:
            return
        self.conn.executemany(
            """
            insert into user_identity_cache (sender_id, display_name)
            values (?, ?)
            on conflict(sender_id) do update set display_name = excluded.display_name,
                updated_at = current_timestamp
            """,
            clean.items(),
        )
        self.conn.commit()

    def add_feedback(
        self,
        event_id: str,
        message_id: str,
        chat_id: str,
        chat_type: str,
        sender_id: str,
        content: str,
        source: str = "",
        category: str = "",
        confidence: float = 0.0,
    ) -> int:
        cur = self.conn.execute(
            """
            insert into feedback (event_id, message_id, chat_id, chat_type, sender_id, content, feedback_source, feedback_category, feedback_confidence)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, message_id, chat_id, chat_type, sender_id, content, source, category, float(confidence)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def feedback_count(self) -> int:
        row = self.conn.execute("select count(*) as n from feedback").fetchone()
        return int(row["n"])

    def list_feedback(self, limit: int = 50, status: str = ""):
        if status:
            rows = self.conn.execute(
                """
                select id, event_id, message_id, chat_id, chat_type, sender_id, content, status, feedback_source, feedback_category, feedback_confidence, created_at
                from feedback
                where status = ?
                order by id desc
                limit ?
                """,
                (status, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                select id, event_id, message_id, chat_id, chat_type, sender_id, content, status, feedback_source, feedback_category, feedback_confidence, created_at
                from feedback
                order by id desc
                limit ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_feedback_status(self, feedback_id: int, status: str) -> bool:
        allowed = {"new", "triaged", "planned", "done", "ignored"}
        clean = str(status or "").strip().lower()
        if clean not in allowed:
            raise ValueError(f"Unsupported feedback status: {status}")
        cur = self.conn.execute(
            "update feedback set status = ? where id = ?",
            (clean, int(feedback_id)),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def admin_summary(self):
        usage = self.conn.execute(
            "select status, count(*) as count from usage_events group by status"
        ).fetchall()
        feedback = self.conn.execute(
            "select status, count(*) as count from feedback group by status"
        ).fetchall()
        jobs = self.queue_stats()
        review_count = self.conn.execute("select count(*) as n from review_issues").fetchone()
        docs_done = self.conn.execute(
            """
            select count(*) as n from usage_events
            where status = 'done' and doc_url != ''
            """
        ).fetchone()
        active_users = self.conn.execute(
            "select count(distinct sender_id) as n from usage_events where sender_id != ''"
        ).fetchone()
        return {
            "usage": {row["status"]: int(row["count"]) for row in usage},
            "feedback": {row["status"]: int(row["count"]) for row in feedback},
            "jobs": jobs,
            "review_issues": int(review_count["n"]),
            "docs_done": int(docs_done["n"]),
            "active_users": int(active_users["n"]),
        }


    def add_usage_event(
        self,
        event_id: str,
        message_id: str,
        chat_id: str,
        chat_type: str,
        sender_id: str,
        source_kind: str,
        source_id: str,
        source_url: str,
        title: str = "",
        status: str = "started",
    ) -> int:
        cur = self.conn.execute(
            """
            insert into usage_events (event_id, message_id, chat_id, chat_type, sender_id, source_kind, source_id, source_url, title, status)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, message_id, chat_id, chat_type, sender_id, source_kind, source_id, source_url, title, status),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_usage_event(self, usage_id: int, status: str, doc_url: str = "", title: str = "", error: str = "") -> None:
        self.conn.execute(
            """
            update usage_events
            set status = ?, doc_url = ?, title = ?, error = ?, updated_at = current_timestamp
            where id = ?
            """,
            (status, doc_url, title, error, usage_id),
        )
        self.conn.commit()

    def list_usage_events(self, limit: int = 50):
        rows = self.conn.execute(
            """
            select id, event_id, message_id, chat_id, chat_type, sender_id, source_kind, source_id, source_url, title, status, doc_url, error, created_at, updated_at
            from usage_events
            order by id desc
            limit ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]


    def dedupe_key(self, source_kind: str, source_id: str) -> str:
        return f"{source_kind}:{source_id}"

    def find_active_job(self, dedupe_key: str):
        row = self.conn.execute(
            """
            select * from queue_jobs
            where dedupe_key = ? and status in ('queued', 'running')
            order by id asc
            limit 1
            """,
            (dedupe_key,),
        ).fetchone()
        return dict(row) if row else None

    def enqueue_job(
        self,
        source_kind: str,
        source_id: str,
        source_url: str,
        event_id: str,
        message_id: str,
        chat_id: str,
        chat_type: str,
        sender_id: str,
        usage_event_id: int,
    ):
        dedupe_key = self.dedupe_key(source_kind, source_id)
        self.conn.execute("begin immediate")
        try:
            job = self.find_active_job(dedupe_key)
            created = False
            if job is None:
                cur = self.conn.execute(
                    """
                    insert into queue_jobs (dedupe_key, source_kind, source_id, source_url, status)
                    values (?, ?, ?, ?, 'queued')
                    """,
                    (dedupe_key, source_kind, source_id, source_url),
                )
                job_id = int(cur.lastrowid)
                created = True
                self.conn.execute("insert into job_events (job_id, event_type, detail) values (?, ?, ?)", (job_id, "enqueue", source_url))
            else:
                job_id = int(job["id"])
                self.conn.execute("insert into job_events (job_id, event_type, detail) values (?, ?, ?)", (job_id, "watch", sender_id))
            cur = self.conn.execute(
                """
                insert into job_watchers (job_id, event_id, message_id, chat_id, chat_type, sender_id, usage_event_id)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, event_id, message_id, chat_id, chat_type, sender_id, int(usage_event_id)),
            )
            watcher_id = int(cur.lastrowid)
            self.conn.commit()
            return {"job_id": job_id, "watcher_id": watcher_id, "created": created}
        except Exception:
            self.conn.rollback()
            raise

    def queue_position(self, job_id: int) -> int:
        row = self.conn.execute("select status from queue_jobs where id = ?", (int(job_id),)).fetchone()
        if row is None or row["status"] == "running":
            return 0
        pos = self.conn.execute(
            """
            select count(*) as n from queue_jobs
            where status = 'queued' and id <= ?
            """,
            (int(job_id),),
        ).fetchone()
        return int(pos["n"])

    def queued_count(self) -> int:
        row = self.conn.execute("select count(*) as n from queue_jobs where status = 'queued'").fetchone()
        return int(row["n"])

    def claim_next_queue_job(self, worker_id: str = ""):
        try:
            self.conn.execute("begin immediate")
            row = self.conn.execute(
                """
                select * from queue_jobs
                where status = 'queued'
                order by priority desc, id asc
                limit 1
                """
            ).fetchone()
            if row is None:
                self.conn.execute("commit")
                return None
            job_id = int(row["id"])
            self.conn.execute(
                """
                update queue_jobs
                set status = 'running', attempts = attempts + 1, started_at = current_timestamp,
                    updated_at = current_timestamp, worker_id = ?, heartbeat_at = current_timestamp,
                    stage = 'claimed', stage_updated_at = current_timestamp,
                    workflow_state = 'claimed', state_version = coalesce(state_version, 0) + 1,
                    last_event = 'claim'
                where id = ?
                """,
                (worker_id, job_id),
            )
            self.conn.execute(
                "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
                (
                    job_id,
                    "transition",
                    json.dumps(
                        {
                            "from": WorkflowState.QUEUED.value,
                            "to": WorkflowState.CLAIMED.value,
                            "event": WorkflowEvent.CLAIM.value,
                            "detail": f"worker claimed {worker_id}".strip(),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            self.conn.execute("insert into job_events (job_id, event_type, detail) values (?, ?, ?)", (job_id, "claim", f"worker claimed {worker_id}".strip()))
            self.conn.execute("commit")
            claimed = dict(row)
            claimed["status"] = "running"
            claimed["attempts"] = int(claimed.get("attempts") or 0) + 1
            claimed["worker_id"] = worker_id
            claimed["stage"] = "claimed"
            claimed["workflow_state"] = WorkflowState.CLAIMED.value
            claimed["state_version"] = int(claimed.get("state_version") or 0) + 1
            claimed["last_event"] = WorkflowEvent.CLAIM.value
            return claimed
        except Exception:
            try:
                self.conn.execute("rollback")
            except Exception:
                pass
            raise

    def complete_queue_job(self, job_id: int, doc_url: str, title: str = "", worker_id: str = "") -> bool:
        self.conn.execute("begin immediate")
        try:
            row = self.conn.execute("select * from queue_jobs where id = ?", (int(job_id),)).fetchone()
            if row is None:
                self.conn.rollback()
                return False
            if worker_id and str(row["worker_id"] or "") != str(worker_id):
                self.conn.rollback()
                return False
            current = self._queue_workflow_state(row)
            if current is WorkflowState.COMPLETED:
                result = None
            else:
                result = transition(current, WorkflowEvent.COMPLETE)
            if result is not None:
                self._update_queue_workflow_locked(
                    job_id,
                    result.to_state,
                    WorkflowEvent.COMPLETE,
                    doc_url,
                    int(row["state_version"] or 0) + 1,
                )
                self._insert_transition_event(job_id, result, doc_url)
            self.conn.execute(
                """
                update queue_jobs
                set status = 'done', doc_url = ?, title = ?, error = '', worker_id = '', stage = 'done',
                    workflow_state = 'completed', finished_at = current_timestamp,
                    updated_at = current_timestamp, stage_updated_at = current_timestamp
                where id = ?
                """,
                (doc_url, title, int(job_id)),
            )
            self.conn.execute("insert into job_events (job_id, event_type, detail) values (?, ?, ?)", (int(job_id), "done", doc_url))
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def fail_queue_job(self, job_id: int, error: str, worker_id: str = "") -> bool:
        detail = str(error)[:1000]
        self.conn.execute("begin immediate")
        try:
            row = self.conn.execute("select * from queue_jobs where id = ?", (int(job_id),)).fetchone()
            if row is None:
                self.conn.rollback()
                return False
            if worker_id and str(row["worker_id"] or "") != str(worker_id):
                self.conn.rollback()
                return False
            current = self._queue_workflow_state(row)
            if current is WorkflowState.COMPLETED:
                self.conn.execute(
                    "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
                    (int(job_id), "ignored_failure_after_done", detail),
                )
                self.conn.commit()
                return True
            result = None
            next_state = current
            if current not in {
                WorkflowState.NEEDS_SOURCE,
                WorkflowState.GENERATION_INCOMPLETE,
                WorkflowState.QUALITY_FAILED,
                WorkflowState.FAILED,
            }:
                result = transition(current, WorkflowEvent.FAIL)
                next_state = result.to_state
                self._update_queue_workflow_locked(
                    job_id,
                    next_state,
                    WorkflowEvent.FAIL,
                    detail,
                    int(row["state_version"] or 0) + 1,
                )
                self._insert_transition_event(job_id, result, detail)
            self.conn.execute(
                """
                update queue_jobs
                set status = 'failed', error = ?, worker_id = '', stage = 'failed',
                    workflow_state = ?, finished_at = current_timestamp,
                    updated_at = current_timestamp, stage_updated_at = current_timestamp
                where id = ?
                """,
                (detail, next_state.value, int(job_id)),
            )
            self.conn.execute("insert into job_events (job_id, event_type, detail) values (?, ?, ?)", (int(job_id), "failed", detail))
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def heartbeat_queue_job(self, job_id: int, worker_id: str = "", stage: str = "") -> bool:
        if not worker_id:
            return False
        if stage:
            cur = self.conn.execute(
                """
                update queue_jobs
                set heartbeat_at = current_timestamp, updated_at = current_timestamp,
                    stage = ?, stage_updated_at = current_timestamp
                where id = ? and status = 'running' and worker_id = ?
                """,
                (stage, int(job_id), worker_id),
            )
        else:
            cur = self.conn.execute(
                """
                update queue_jobs
                set heartbeat_at = current_timestamp, updated_at = current_timestamp
                where id = ? and status = 'running' and worker_id = ?
                """,
                (int(job_id), worker_id),
            )
        self.conn.commit()
        return cur.rowcount == 1

    def update_queue_job_stage(self, job_id: int, stage: str, worker_id: str = "") -> bool:
        params = [stage, int(job_id)]
        owner_clause = ""
        if worker_id:
            owner_clause = " and worker_id = ?"
            params.append(worker_id)
        cur = self.conn.execute(
            f"""
            update queue_jobs
            set stage = ?, stage_updated_at = current_timestamp, heartbeat_at = current_timestamp, updated_at = current_timestamp
            where id = ? and status = 'running'{owner_clause}
            """,
            params,
        )
        if cur.rowcount == 1:
            self.conn.execute("insert into job_events (job_id, event_type, detail) values (?, ?, ?)", (int(job_id), "stage", stage))
        self.conn.commit()
        return cur.rowcount == 1

    def recover_stale_queue_jobs(self, stale_minutes: int) -> int:
        rows = self.conn.execute(
            """
            select id, worker_id, stage, heartbeat_at, started_at
            from queue_jobs
            where status = 'running'
              and (heartbeat_at is null or heartbeat_at < datetime('now', ?))
            """,
            (f"-{int(stale_minutes)} minutes",),
        ).fetchall()
        return self._recover_queue_job_rows(rows, "recover_stale")

    def recover_dead_worker_queue_jobs(self, host: str, is_pid_alive) -> int:
        rows = self.conn.execute(
            """
            select id, worker_id, stage, heartbeat_at, started_at
            from queue_jobs
            where status = 'running' and worker_id != ''
            """
        ).fetchall()
        dead_rows = []
        for row in rows:
            parsed_host, pid = _parse_worker_host_pid(row["worker_id"] or "")
            if parsed_host == host and pid is not None and not is_pid_alive(pid):
                dead_rows.append(row)
        return self._recover_queue_job_rows(dead_rows, "recover_dead_worker")

    def _recover_queue_job_rows(self, rows, event_type: str) -> int:
        if not rows:
            return 0
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute("begin immediate")
        try:
            current_rows = self.conn.execute(
                f"""
                select id, worker_id, stage, workflow_state, heartbeat_at, started_at
                from queue_jobs
                where id in ({placeholders}) and status = 'running'
                """,
                ids,
            ).fetchall()
            if not current_rows:
                self.conn.commit()
                return 0
            current_ids = [int(row["id"]) for row in current_rows]
            current_placeholders = ",".join("?" for _ in current_ids)
            self.conn.execute(
                f"""
                update queue_jobs
                set status = 'queued', worker_id = '', stage = 'recovered', started_at = null,
                    heartbeat_at = null, updated_at = current_timestamp, stage_updated_at = current_timestamp,
                    workflow_state = 'queued', state_version = coalesce(state_version, 0) + 1,
                    last_event = 'recover'
                where id in ({current_placeholders}) and status = 'running'
                """,
                current_ids,
            )
            for row in current_rows:
                detail = f"worker={row['worker_id'] or ''} stage={row['stage'] or ''} heartbeat={row['heartbeat_at'] or ''} started={row['started_at'] or ''}"
                from_state = str(row["workflow_state"] or "").strip()
                if not from_state:
                    try:
                        from_state = state_from_legacy("running", row["stage"] or "").value
                    except ValueError:
                        from_state = WorkflowState.CLAIMED.value
                self.conn.execute(
                    "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
                    (
                        int(row["id"]),
                        "transition",
                        json.dumps(
                            {
                                "from": from_state,
                                "to": WorkflowState.QUEUED.value,
                                "event": WorkflowEvent.RECOVER.value,
                                "detail": detail.strip(),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                self.conn.execute(
                    "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
                    (int(row["id"]), event_type, detail.strip()),
                )
            self.conn.commit()
            return len(current_rows)
        except Exception:
            self.conn.rollback()
            raise

    def get_job_watchers(self, job_id: int):
        rows = self.conn.execute(
            """
            select * from job_watchers
            where job_id = ? and notified = 0
            order by id asc
            """,
            (int(job_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_watcher_notified(self, watcher_id: int) -> None:
        self.conn.execute("update job_watchers set notified = 1 where id = ?", (int(watcher_id),))
        self.conn.commit()

    def list_queue_jobs(self, limit: int = 50, status: str = ""):
        if status:
            rows = self.conn.execute(
                """
                select * from queue_jobs
                where status = ?
                order by id desc
                limit ?
                """,
                (status, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                select * from queue_jobs
                order by id desc
                limit ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]


    def add_job_event(self, job_id: int, event_type: str, detail: str = "") -> int:
        cur = self.conn.execute(
            "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
            (int(job_id), str(event_type), str(detail)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_job_events(self, job_id: int = 0, limit: int = 100):
        if job_id:
            rows = self.conn.execute(
                """
                select id, job_id, event_type, detail, created_at
                from job_events
                where job_id = ?
                order by id desc
                limit ?
                """,
                (int(job_id), int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                select id, job_id, event_type, detail, created_at
                from job_events
                order by id desc
                limit ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def queue_stats(self):
        rows = self.conn.execute(
            "select status, count(*) as count from queue_jobs group by status order by status"
        ).fetchall()
        stats = {row["status"]: int(row["count"]) for row in rows}
        pending_watchers = self.conn.execute(
            "select count(*) as n from job_watchers where notified = 0"
        ).fetchone()
        stats["pending_watchers"] = int(pending_watchers["n"])
        return stats

    def retry_queue_job(self, job_id: int) -> bool:
        self.conn.execute("begin immediate")
        try:
            row = self.conn.execute("select * from queue_jobs where id = ?", (int(job_id),)).fetchone()
            if row is None or row["status"] != "failed":
                self.conn.commit()
                return False
            current = self._queue_workflow_state(row)
            result = transition(current, WorkflowEvent.RETRY)
            self._update_queue_workflow_locked(
                job_id,
                result.to_state,
                WorkflowEvent.RETRY,
                "manual retry",
                int(row["state_version"] or 0) + 1,
            )
            self.conn.execute(
                """
                update queue_jobs
                set status = 'queued', error = '', worker_id = '', stage = 'retry_queued',
                    started_at = null, finished_at = null, heartbeat_at = null,
                    updated_at = current_timestamp, stage_updated_at = current_timestamp
                where id = ? and workflow_state = 'queued'
                """,
                (int(job_id),),
            )
            self.conn.execute("update job_watchers set notified = 0 where job_id = ?", (int(job_id),))
            self._insert_transition_event(job_id, result, "manual retry")
            self.conn.execute(
                "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
                (int(job_id), "retry", "manual retry"),
            )
            self.conn.commit()
            return True
        except (InvalidWorkflowTransition, ValueError):
            self.conn.rollback()
            return False
        except Exception:
            self.conn.rollback()
            raise

    def transition_queue_job(
        self,
        job_id: int,
        event: WorkflowEvent | str,
        detail: str = "",
        expected_worker_id: str = "",
    ):
        """Apply one validated workflow transition and append an audit event.

        The legacy ``status`` and ``stage`` columns remain available for old
        workers and the admin UI. ``workflow_state`` is the canonical state;
        old rows are lazily upgraded from those legacy columns on first use.
        """
        trigger = WorkflowEvent(event)
        self.conn.execute("begin immediate")
        try:
            row = self.conn.execute("select * from queue_jobs where id = ?", (int(job_id),)).fetchone()
            if row is None:
                raise KeyError(f"queue job not found: {job_id}")
            if expected_worker_id and str(row["worker_id"] or "") != str(expected_worker_id):
                raise QueueLeaseLostError(f"queue job lease lost: job={job_id} worker={expected_worker_id}")
            current = self._queue_workflow_state(row)
            result = transition(current, trigger)
            version = int(row["state_version"] or 0) + 1
            next_state = result.to_state
            self._update_queue_workflow_locked(job_id, next_state, trigger, detail, version)
            self._insert_transition_event(job_id, result, detail)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def _update_queue_workflow_locked(
        self,
        job_id: int,
        state: WorkflowState,
        event: WorkflowEvent,
        detail: str,
        version: int,
    ) -> None:
        checkpoint = None
        if event is WorkflowEvent.PUBLISH_SUCCEEDED:
            raw_detail = str(detail or "").strip()
            fallback_url = raw_detail if raw_detail.startswith(("http://", "https://")) else ""
            checkpoint = PublishedCheckpoint.from_json(raw_detail, fallback_url=fallback_url)
        if checkpoint is not None:
            self.conn.execute(
                """
                update queue_jobs
                set status = ?, workflow_state = ?, state_version = ?, last_event = ?, doc_url = ?, checkpoint_json = ?,
                    updated_at = current_timestamp, stage_updated_at = current_timestamp
                where id = ?
                """,
                (
                    queue_status_for_state(state),
                    state.value,
                    int(version),
                    event.value,
                    checkpoint.doc_url,
                    checkpoint.to_json(),
                    int(job_id),
                ),
            )
            return
        self.conn.execute(
            """
            update queue_jobs
            set status = ?, workflow_state = ?, state_version = ?, last_event = ?,
                updated_at = current_timestamp, stage_updated_at = current_timestamp
            where id = ?
            """,
            (queue_status_for_state(state), state.value, int(version), event.value, int(job_id)),
        )

    def _insert_transition_event(self, job_id: int, result, detail: str = "") -> None:
        event_detail = json.dumps(
            {
                "from": result.from_state.value,
                "to": result.to_state.value,
                "event": result.event.value,
                "detail": str(detail)[:1000],
            },
            ensure_ascii=False,
        )
        self.conn.execute(
            "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
            (int(job_id), "transition", event_detail),
        )

    @staticmethod
    def _queue_workflow_state(row) -> WorkflowState:
        raw = str(row["workflow_state"] or "").strip()
        if raw:
            try:
                return WorkflowState(raw)
            except ValueError:
                pass
        return state_from_legacy(row["status"], row["stage"])



    def add_review_issue(self, source_kind: str, source_id: str, category: str, severity: str, detail: str) -> int:
        cur = self.conn.execute(
            """
            insert into review_issues (source_kind, source_id, category, severity, detail)
            values (?, ?, ?, ?, ?)
            """,
            (str(source_kind), str(source_id), str(category), str(severity), str(detail)[:1000]),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_review_issues(self, source_kind: str, source_id: str, issues) -> int:
        count = 0
        for issue in issues:
            self.add_review_issue(
                source_kind,
                source_id,
                getattr(issue, "category", "other"),
                getattr(issue, "severity", "low"),
                getattr(issue, "detail", str(issue)),
            )
            count += 1
        return count

    def list_review_issues(self, limit: int = 50, source_kind: str = "", source_id: str = ""):
        where = []
        params = []
        if source_kind:
            where.append("source_kind = ?")
            params.append(source_kind)
        if source_id:
            where.append("source_id = ?")
            params.append(source_id)
        clause = " where " + " and ".join(where) if where else ""
        rows = self.conn.execute(
            f"""
            select id, source_kind, source_id, category, severity, detail, created_at
            from review_issues
            {clause}
            order by id desc
            limit ?
            """,
            (*params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def review_issue_stats(self):
        rows = self.conn.execute(
            """
            select category, severity, count(*) as count
            from review_issues
            group by category, severity
            order by count desc, category asc, severity asc
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_duty_roster(self, members) -> None:
        clean = []
        seen = set()
        for ordinal, member in enumerate(members):
            user_id = str(member.get("user_id", "")).strip()
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            clean.append((len(clean), str(member.get("name", "")).strip() or user_id, user_id))
        if not clean:
            raise ValueError("Duty roster cannot be empty")
        self.conn.execute("delete from duty_roster")
        self.conn.executemany(
            "insert into duty_roster (ordinal, name, user_id) values (?, ?, ?)",
            clean,
        )
        self.set_duty_setting("rotation_start_date", "")
        self.conn.commit()

    def list_duty_roster(self, enabled_only: bool = False):
        query = "select id, ordinal, name, user_id, enabled, created_at, updated_at from duty_roster"
        if enabled_only:
            query += " where enabled = 1"
        query += " order by ordinal asc, id asc"
        return [dict(row) for row in self.conn.execute(query).fetchall()]

    def get_duty_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("select value from duty_settings where key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_duty_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            insert into duty_settings (key, value) values (?, ?)
            on conflict(key) do update set value = excluded.value, updated_at = current_timestamp
            """,
            (str(key), str(value)),
        )

    def get_duty_reminder(self, reminder_date: str):
        row = self.conn.execute(
            "select * from duty_reminders where reminder_date = ?", (reminder_date,)
        ).fetchone()
        return dict(row) if row else None

    def reserve_duty_reminder(self, reminder_date: str, roster_member) -> bool:
        row = self.get_duty_reminder(reminder_date)
        if row and row["status"] == "sent":
            return False
        if row:
            self.conn.execute(
                """
                update duty_reminders
                set roster_id = ?, name = ?, user_id = ?, status = 'pending',
                    error = '', attempts = attempts + 1, updated_at = current_timestamp
                where reminder_date = ?
                """,
                (int(roster_member["id"]), roster_member["name"], roster_member["user_id"], reminder_date),
            )
        else:
            self.conn.execute(
                """
                insert into duty_reminders (reminder_date, roster_id, name, user_id, status, attempts)
                values (?, ?, ?, ?, 'pending', 1)
                """,
                (reminder_date, int(roster_member["id"]), roster_member["name"], roster_member["user_id"]),
            )
        self.conn.commit()
        return True

    def complete_duty_reminder(self, reminder_date: str, message_id: str = "") -> None:
        self.conn.execute(
            """
            update duty_reminders
            set status = 'sent', message_id = ?, error = '', sent_at = current_timestamp,
                updated_at = current_timestamp
            where reminder_date = ?
            """,
            (str(message_id), reminder_date),
        )
        self.conn.commit()

    def fail_duty_reminder(self, reminder_date: str, error: str) -> None:
        self.conn.execute(
            """
            update duty_reminders
            set status = 'failed', error = ?, updated_at = current_timestamp
            where reminder_date = ?
            """,
            (str(error)[:1000], reminder_date),
        )
        self.conn.commit()

    def list_duty_reminders(self, limit: int = 30):
        rows = self.conn.execute(
            "select * from duty_reminders order by reminder_date desc limit ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.conn.close()

def _parse_worker_host_pid(worker_id: str) -> tuple[str, int | None]:
    parts = str(worker_id or "").split(":")
    if len(parts) < 2:
        return "", None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return parts[0], None
