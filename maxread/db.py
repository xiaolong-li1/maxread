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
    legacy_stage_for_state,
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
    project_summary: str = ""
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
    def __init__(self, path: Path, initialize: bool = True):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        if initialize:
            self.init()

    def init(self) -> None:
        self.conn.executescript(
            """
            create table if not exists papers (
                paper_id text primary key,
                title text not null default '',
                project_summary text not null default '',
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

            create table if not exists web_identities (
                id integer primary key autoincrement,
                public_id text not null unique,
                session_hash text not null unique,
                account_type text not null default 'guest',
                feishu_open_id text not null default '',
                display_name text not null default '游客',
                created_at datetime not null default current_timestamp,
                last_seen_at datetime not null default current_timestamp,
                bound_at datetime
            );

            create table if not exists web_binding_codes (
                code_hash text primary key,
                web_identity_id integer not null,
                expires_at datetime not null,
                used_at datetime,
                created_at datetime not null default current_timestamp,
                foreign key (web_identity_id) references web_identities(id) on delete cascade
            );

            create index if not exists web_identity_feishu_idx on web_identities(feishu_open_id);
            create index if not exists web_binding_identity_idx on web_binding_codes(web_identity_id, expires_at);

            create table if not exists web_conversations (
                id integer primary key autoincrement,
                owner_key text not null unique,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            );

            create table if not exists web_messages (
                id integer primary key autoincrement,
                conversation_id integer not null,
                external_id text not null unique,
                role text not null,
                kind text not null default 'message',
                content text not null default '',
                source_id text not null default '',
                job_id integer not null default 0,
                doc_url text not null default '',
                status text not null default '',
                channel text not null default 'web',
                actor_type text not null default 'user',
                actor_id text not null default '',
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp,
                foreign key (conversation_id) references web_conversations(id) on delete cascade
            );

            create index if not exists web_messages_conversation_idx on web_messages(conversation_id, id);
            create index if not exists web_messages_job_idx on web_messages(job_id, conversation_id);

            create table if not exists web_project_preferences (
                owner_key text not null,
                source_id text not null,
                favorite integer not null default 0,
                category text not null default '',
                category_source text not null default '',
                deleted_at datetime,
                updated_at datetime not null default current_timestamp,
                primary key (owner_key, source_id)
            );

            create index if not exists web_project_preferences_owner_idx
                on web_project_preferences(owner_key, deleted_at, favorite, updated_at);

            create table if not exists service_status (
                id integer primary key check (id = 1),
                mode text not null default 'operational',
                reason text not null default '',
                expected_recovery_at text not null default '',
                updated_by text not null default '',
                updated_at datetime not null default current_timestamp
            );

            insert or ignore into service_status (id) values (1);

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
                finished_at datetime,
                suppress_progress_notifications integer not null default 0,
                recovery_reason text not null default '',
                recovery_attempts integer not null default 0,
                auto_retry_count integer not null default 0,
                rebuild_pipeline integer not null default 0
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
        self._ensure_column("queue_jobs", "suppress_progress_notifications", "integer not null default 0")
        self._ensure_column("queue_jobs", "recovery_reason", "text not null default ''")
        self._ensure_column("queue_jobs", "recovery_attempts", "integer not null default 0")
        self._ensure_column("queue_jobs", "auto_retry_count", "integer not null default 0")
        self._ensure_column("queue_jobs", "rebuild_pipeline", "integer not null default 0")
        self._ensure_column("papers", "project_summary", "text not null default ''")
        self._ensure_column("web_project_preferences", "category_source", "text not null default ''")
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
            project_summary=row["project_summary"],
            doc_url=row["doc_url"],
            doc_token=row["doc_token"],
            error=row["error"],
        )

    def upsert_paper(self, paper_id: str, status: str, **fields: str) -> None:
        existing = self.get_paper(paper_id)
        names = {"title", "project_summary", "authors", "arxiv_url", "pdf_path", "source_path", "doc_url", "doc_token", "error"}
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

    def set_paper_project_summary(self, paper_id: str, summary: str) -> None:
        self.conn.execute(
            "update papers set project_summary = ? where paper_id = ? and project_summary = ''",
            (str(summary or "")[:500], str(paper_id)),
        )
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

    def mark_cache_legacy_before(self, cutoff: str) -> dict[str, int]:
        paper = self.conn.execute(
            "update papers set status='legacy' where status='cache_expired' or (status='done' and updated_at < ?)",
            (str(cutoff),),
        )
        document = self.conn.execute(
            "update documents set status='legacy' where status='cache_expired' or (status='done' and updated_at < ?)",
            (str(cutoff),),
        )
        self.conn.commit()
        return {"papers": int(paper.rowcount), "documents": int(document.rowcount)}

    def list_cache_cleanup_candidates(self, cutoff: str):
        rows = self.conn.execute(
            """
            select 'paper' source_kind, paper_id source_id, updated_at
            from papers
            where status in ('done', 'legacy') and updated_at <= ?
            union all
            select 'article' source_kind, doc_id source_id, updated_at
            from documents
            where status in ('done', 'legacy') and updated_at <= ?
            order by updated_at asc
            """,
            (str(cutoff), str(cutoff)),
        ).fetchall()
        return [dict(row) for row in rows]


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

    def get_or_create_web_identity(self, session_hash: str, public_id: str = ""):
        clean_hash = str(session_hash or "").strip()
        if not clean_hash:
            raise ValueError("session_hash is required")
        row = self.conn.execute(
            "select * from web_identities where session_hash = ?",
            (clean_hash,),
        ).fetchone()
        if row is None:
            clean_public_id = str(public_id or "").strip()
            if not clean_public_id:
                raise ValueError("public_id is required for a new web identity")
            self.conn.execute(
                "insert into web_identities (public_id, session_hash) values (?, ?)",
                (clean_public_id, clean_hash),
            )
            self.conn.execute(
                "insert or ignore into user_identity_cache (sender_id, display_name) values (?, '游客')",
                (f"guest:{clean_public_id}",),
            )
        else:
            self.conn.execute(
                "update web_identities set last_seen_at = current_timestamp where id = ?",
                (int(row["id"]),),
            )
        self.conn.commit()
        return self.get_web_identity(clean_hash)

    def get_web_identity(self, session_hash: str):
        row = self.conn.execute(
            "select * from web_identities where session_hash = ?",
            (str(session_hash or "").strip(),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        open_id = str(result.get("feishu_open_id") or "")
        if open_id:
            names = self.get_user_names([open_id])
            if names.get(open_id):
                result["display_name"] = names[open_id]
        return result

    def get_web_identity_by_public_id(self, public_id: str):
        row = self.conn.execute(
            "select * from web_identities where public_id = ?",
            (str(public_id or "").strip(),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        open_id = str(result.get("feishu_open_id") or "")
        if open_id:
            result["display_name"] = self.get_user_names([open_id]).get(open_id, result["display_name"])
        return result

    def update_web_identity_display_name(self, feishu_open_id: str, display_name: str) -> None:
        clean_name = str(display_name or "").strip()
        clean_open_id = str(feishu_open_id or "").strip()
        if not clean_name or not clean_open_id:
            return
        self.conn.execute(
            """
            update web_identities set display_name = ?, last_seen_at = current_timestamp
            where feishu_open_id = ?
            """,
            (clean_name, clean_open_id),
        )
        self.conn.commit()

    @staticmethod
    def web_identity_sender(identity) -> str:
        open_id = str(identity.get("feishu_open_id") or "").strip()
        return open_id or f"guest:{identity['public_id']}"

    @staticmethod
    def web_conversation_owner(identity) -> str:
        open_id = str(identity.get("feishu_open_id") or "").strip()
        return f"feishu:{open_id}" if open_id else f"guest:{identity['public_id']}"

    def ensure_web_conversation(self, identity):
        owner_key = self.web_conversation_owner(identity)
        self.conn.execute(
            "insert or ignore into web_conversations (owner_key) values (?)",
            (owner_key,),
        )
        self.conn.commit()
        row = self.conn.execute(
            "select * from web_conversations where owner_key = ?",
            (owner_key,),
        ).fetchone()
        return dict(row)

    def append_web_message(
        self,
        conversation_id: int,
        external_id: str,
        role: str,
        content: str,
        *,
        kind: str = "message",
        source_id: str = "",
        job_id: int = 0,
        doc_url: str = "",
        status: str = "",
        channel: str = "web",
        actor_type: str = "user",
        actor_id: str = "",
    ):
        self.conn.execute(
            """
            insert into web_messages (
                conversation_id, external_id, role, kind, content, source_id,
                job_id, doc_url, status, channel, actor_type, actor_id
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(external_id) do update set
                role=excluded.role, kind=excluded.kind, content=excluded.content,
                source_id=excluded.source_id, job_id=excluded.job_id,
                doc_url=excluded.doc_url, status=excluded.status,
                channel=excluded.channel, updated_at=current_timestamp
            """,
            (
                int(conversation_id), str(external_id), str(role), str(kind),
                str(content)[:8000], str(source_id), int(job_id or 0), str(doc_url),
                str(status), str(channel), str(actor_type), str(actor_id),
            ),
        )
        self.conn.execute(
            "update web_conversations set updated_at = current_timestamp where id = ?",
            (int(conversation_id),),
        )
        self.conn.commit()
        row = self.conn.execute(
            "select * from web_messages where external_id = ?",
            (str(external_id),),
        ).fetchone()
        return dict(row)

    def list_web_messages(self, identity, after_id: int = 0, limit: int = 100):
        conversation = self.ensure_web_conversation(identity)
        rows = self.conn.execute(
            """
            select m.*,
                   coalesce(nullif(q.title, ''), nullif(p.title, ''), '') as job_title,
                   coalesce(q.source_id, m.source_id) as job_source_id,
                   coalesce(q.status, m.status) as job_status,
                   coalesce(q.workflow_state, '') as job_workflow_state,
                   coalesce(q.stage, '') as job_stage,
                   coalesce(q.error, '') as job_error,
                   coalesce(nullif(q.doc_url, ''), m.doc_url) as job_doc_url
            from web_messages m
            left join queue_jobs q on q.id = m.job_id and m.job_id != 0
            left join papers p on q.source_kind = 'paper' and p.paper_id = q.source_id
            where m.conversation_id = ? and m.id > ?
            order by m.id asc
            limit ?
            """,
            (int(conversation["id"]), max(0, int(after_id)), max(1, min(200, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_web_accounts(self, limit: int = 200):
        rows = self.conn.execute(
            """
            select w.public_id, w.account_type, w.feishu_open_id, w.display_name,
                   w.created_at, w.last_seen_at,
                   (select count(*) from usage_events ue
                    where ue.chat_type = 'web' and ue.chat_id = 'web:' || w.public_id) as submission_count
            from web_identities w
            order by w.last_seen_at desc, w.id desc
            limit ?
            """,
            (max(1, min(500, int(limit))),),
        ).fetchall()
        results = [dict(row) for row in rows]
        names = self.get_user_names([row["feishu_open_id"] for row in results if row["feishu_open_id"]])
        for row in results:
            if row["feishu_open_id"] in names:
                row["display_name"] = names[row["feishu_open_id"]]
        return results

    def list_web_identity_jobs(self, identity, limit: int = 8):
        public_id = str(identity.get("public_id") or "")
        open_id = str(identity.get("feishu_open_id") or "").strip()
        clauses = ["(w.chat_type = 'web' and w.chat_id = ?)"]
        params: list[object] = [f"web:{public_id}"]
        if open_id:
            clauses.append("w.sender_id = ?")
            params.append(open_id)
        rows = self.conn.execute(
            f"""
            select q.*, coalesce(nullif(q.title, ''), nullif(p.title, ''), '') as resolved_title,
                   coalesce(p.project_summary, '') as project_summary
            from queue_jobs q
            left join papers p on q.source_kind = 'paper' and p.paper_id = q.source_id
            where exists (
                select 1 from job_watchers w
                where w.job_id = q.id and ({' or '.join(clauses)})
            )
            order by case q.status when 'running' then 0 when 'queued' then 1 else 2 end,
                     q.id desc
            limit ?
            """,
            (*params, max(1, min(200, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_web_identity_job(self, identity, job_id: int):
        """Return one owned job without applying project-list pagination."""
        public_id = str(identity.get("public_id") or "")
        open_id = str(identity.get("feishu_open_id") or "").strip()
        clauses = ["(w.chat_type = 'web' and w.chat_id = ?)"]
        params: list[object] = [int(job_id), f"web:{public_id}"]
        if open_id:
            clauses.append("w.sender_id = ?")
            params.append(open_id)
        row = self.conn.execute(
            f"""
            select q.*, coalesce(nullif(q.title, ''), nullif(p.title, ''), '') as resolved_title,
                   coalesce(p.project_summary, '') as project_summary
            from queue_jobs q
            left join papers p on q.source_kind = 'paper' and p.paper_id = q.source_id
            where q.id = ? and exists (
                select 1 from job_watchers w
                where w.job_id = q.id and ({' or '.join(clauses)})
            )
            limit 1
            """,
            params,
        ).fetchone()
        return dict(row) if row is not None else None

    def list_web_identity_usage(self, identity, limit: int = 50):
        public_id = str(identity.get("public_id") or "")
        open_id = str(identity.get("feishu_open_id") or "").strip()
        clauses = ["(chat_type = 'web' and chat_id = ?)"]
        params: list[object] = [f"web:{public_id}"]
        if open_id:
            clauses.append("sender_id = ?")
            params.append(open_id)
        rows = self.conn.execute(
            f"""
            select ue.*, coalesce(p.project_summary, '') as project_summary
            from usage_events ue
            left join papers p on p.paper_id = ue.source_id
            where ue.source_kind = 'paper' and ({' or '.join(clauses)})
            order by ue.id desc
            limit ?
            """,
            (*params, max(1, min(200, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def web_project_preferences(self, identity) -> dict[str, dict]:
        owner_key = self.web_conversation_owner(identity)
        rows = self.conn.execute(
            "select * from web_project_preferences where owner_key = ?",
            (owner_key,),
        ).fetchall()
        return {str(row["source_id"]): dict(row) for row in rows}

    def _web_identity_owns_source(self, identity, source_id: str) -> bool:
        public_id = str(identity.get("public_id") or "")
        open_id = str(identity.get("feishu_open_id") or "").strip()
        clauses = ["(w.chat_type = 'web' and w.chat_id = ?)"]
        params: list[object] = [str(source_id), f"web:{public_id}"]
        if open_id:
            clauses.append("w.sender_id = ?")
            params.append(open_id)
        row = self.conn.execute(
            f"""
            select 1
            from queue_jobs q
            join job_watchers w on w.job_id = q.id
            where q.source_kind in ('paper', 'article') and q.source_id = ?
              and ({' or '.join(clauses)})
            limit 1
            """,
            params,
        ).fetchone()
        if row is not None:
            return True
        usage_clauses = ["(chat_type = 'web' and chat_id = ?)"]
        usage_params: list[object] = [str(source_id), f"web:{public_id}"]
        if open_id:
            usage_clauses.append("sender_id = ?")
            usage_params.append(open_id)
        row = self.conn.execute(
            f"""
            select 1 from usage_events
            where source_kind in ('paper', 'article') and source_id = ?
              and ({' or '.join(usage_clauses)})
            limit 1
            """,
            usage_params,
        ).fetchone()
        return row is not None

    def restore_web_project(self, identity, source_id: str) -> None:
        owner_key = self.web_conversation_owner(identity)
        self.conn.execute(
            """
            insert into web_project_preferences (owner_key, source_id, deleted_at)
            values (?, ?, null)
            on conflict(owner_key, source_id) do update set
                deleted_at=null, updated_at=current_timestamp
            """,
            (owner_key, str(source_id)),
        )
        self.conn.commit()

    def set_web_project_favorite(self, identity, source_id: str, favorite: bool) -> dict:
        clean_source = str(source_id or "").strip()
        if not clean_source or not self._web_identity_owns_source(identity, clean_source):
            raise ValueError("项目不在当前账号范围")
        owner_key = self.web_conversation_owner(identity)
        self.conn.execute(
            """
            insert into web_project_preferences (owner_key, source_id, favorite)
            values (?, ?, ?)
            on conflict(owner_key, source_id) do update set
                favorite=excluded.favorite, updated_at=current_timestamp
            """,
            (owner_key, clean_source, 1 if favorite else 0),
        )
        self.conn.commit()
        return {"source_id": clean_source, "favorite": bool(favorite)}

    def set_web_project_category(self, identity, source_id: str, category: str) -> dict:
        clean_source = str(source_id or "").strip()
        clean_category = str(category or "").strip()[:60]
        if not clean_source or not self._web_identity_owns_source(identity, clean_source):
            raise ValueError("项目不在当前账号范围")
        owner_key = self.web_conversation_owner(identity)
        self.conn.execute(
            """
            insert into web_project_preferences (owner_key, source_id, category, category_source)
            values (?, ?, ?, 'manual')
            on conflict(owner_key, source_id) do update set
                category=excluded.category, category_source='manual', updated_at=current_timestamp
            """,
            (owner_key, clean_source, clean_category),
        )
        self.conn.commit()
        return {"source_id": clean_source, "category": clean_category}

    def set_web_project_auto_categories(self, identity, assignments: dict[str, str]) -> int:
        owner_key = self.web_conversation_owner(identity)
        rows = []
        for source_id, category in assignments.items():
            clean_source = str(source_id or "").strip()
            clean_category = str(category or "").strip()[:60]
            if not clean_source or not clean_category:
                continue
            if not self._web_identity_owns_source(identity, clean_source):
                continue
            preference = self.conn.execute(
                "select category, category_source from web_project_preferences where owner_key=? and source_id=?",
                (owner_key, clean_source),
            ).fetchone()
            if preference is not None and str(preference["category"] or "").strip() and str(preference["category_source"] or "manual") != "ai":
                continue
            rows.append((owner_key, clean_source, clean_category))
        if not rows:
            return 0
        self.conn.executemany(
            """
            insert into web_project_preferences (owner_key, source_id, category, category_source)
            values (?, ?, ?, 'ai')
            on conflict(owner_key, source_id) do update set
                category=excluded.category, category_source='ai', updated_at=current_timestamp
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def delete_web_project(self, identity, source_id: str) -> dict:
        clean_source = str(source_id or "").strip()
        if not clean_source or not self._web_identity_owns_source(identity, clean_source):
            raise ValueError("项目不在当前账号范围")
        owner_key = self.web_conversation_owner(identity)
        public_chat = f"web:{str(identity.get('public_id') or '')}"
        self.conn.execute(
            """
            insert into web_project_preferences (owner_key, source_id, deleted_at)
            values (?, ?, current_timestamp)
            on conflict(owner_key, source_id) do update set
                deleted_at=current_timestamp, updated_at=current_timestamp
            """,
            (owner_key, clean_source),
        )
        job = self.conn.execute(
            """
            select q.* from queue_jobs q
            where q.source_kind in ('paper', 'article') and q.source_id = ?
              and exists (
                select 1 from job_watchers w
                where w.job_id = q.id and w.chat_type = 'web' and w.chat_id = ?
              )
            order by q.id desc limit 1
            """,
            (clean_source, public_chat),
        ).fetchone()
        cancelled = False
        if job is not None:
            watchers = self.conn.execute(
                "select * from job_watchers where job_id = ? and notified = 0",
                (int(job["id"]),),
            ).fetchall()
            exclusively_owned = bool(watchers) and all(
                str(row["chat_type"] or "") == "web" and str(row["chat_id"] or "") == public_chat
                for row in watchers
            )
            if str(job["status"] or "") == "queued" and exclusively_owned:
                current = self._queue_workflow_state(job)
                result = transition(current, WorkflowEvent.CANCEL)
                self._update_queue_workflow_locked(
                    int(job["id"]), result.to_state, WorkflowEvent.CANCEL,
                    "cancelled after owner deleted accidental web submission",
                    int(job["state_version"] or 0) + 1,
                )
                self._insert_transition_event(
                    int(job["id"]), result,
                    "cancelled after owner deleted accidental web submission",
                )
                cancelled = True
            self.conn.execute(
                "update job_watchers set notified = 1 where job_id = ? and chat_type = 'web' and chat_id = ?",
                (int(job["id"]), public_chat),
            )
            self.conn.execute(
                """
                update usage_events set status = ?, updated_at = current_timestamp
                where id in (
                    select usage_event_id from job_watchers
                    where job_id = ? and chat_type = 'web' and chat_id = ? and usage_event_id != 0
                )
                """,
                ("cancelled" if cancelled else "hidden", int(job["id"]), public_chat),
            )
            self.conn.execute(
                "insert into job_events (job_id, event_type, detail) values (?, 'web_project_deleted', ?)",
                (int(job["id"]), owner_key),
            )
        self.conn.commit()
        return {"source_id": clean_source, "deleted": True, "cancelled": cancelled}

    def recent_pet_message_count(self, identity, minutes: int = 10) -> int:
        conversation = self.ensure_web_conversation(identity)
        row = self.conn.execute(
            """
            select count(*) as n from web_messages
            where conversation_id = ? and kind = 'pet_user'
              and created_at >= datetime('now', ?)
            """,
            (int(conversation["id"]), f"-{max(1, int(minutes))} minutes"),
        ).fetchone()
        return int(row["n"] if row else 0)

    def _conversation_for_watcher(self, watcher):
        if str(watcher.get("chat_type") or "").lower() == "web":
            row = self.conn.execute(
                "select conversation_id from web_messages where external_id = ?",
                (str(watcher.get("message_id") or ""),),
            ).fetchone()
            return int(row["conversation_id"]) if row else 0
        sender_id = str(watcher.get("sender_id") or "").strip()
        if not sender_id:
            return 0
        identity = self.conn.execute(
            "select * from web_identities where feishu_open_id = ? order by bound_at asc, id asc limit 1",
            (sender_id,),
        ).fetchone()
        if identity is None:
            return 0
        return int(self.ensure_web_conversation(dict(identity))["id"])

    def update_web_job_progress(self, watcher, job_id: int, source_id: str, content: str, status: str) -> None:
        conversation_id = self._conversation_for_watcher(watcher)
        if not conversation_id:
            return
        if not str(source_id or "").strip():
            row = self.conn.execute(
                "select source_id from queue_jobs where id = ?",
                (int(job_id),),
            ).fetchone()
            source_id = str(row["source_id"] if row else "")
        owner = self.conn.execute(
            "select owner_key from web_conversations where id = ?",
            (conversation_id,),
        ).fetchone()
        owner_key = str(owner["owner_key"] if owner else conversation_id)
        self.append_web_message(
            conversation_id,
            f"web-task:{owner_key}:{source_id or int(job_id)}",
            "assistant",
            content,
            kind="queue_ack",
            source_id=source_id,
            job_id=job_id,
            status=status,
            channel="system",
            actor_type="system",
        )

    def upsert_web_task(
        self,
        identity,
        job_id: int,
        source_id: str,
        content: str,
        *,
        status: str,
        doc_url: str = "",
        kind: str = "queue_ack",
    ) -> None:
        """Update the identity's one durable project record for a paper."""
        conversation = self.ensure_web_conversation(identity)
        owner_key = str(conversation.get("owner_key") or conversation["id"])
        self.append_web_message(
            int(conversation["id"]),
            f"web-task:{owner_key}:{source_id or int(job_id)}",
            "assistant",
            content,
            kind=kind,
            source_id=source_id,
            job_id=job_id,
            doc_url=doc_url,
            status=status,
            channel="system",
            actor_type="system",
        )

    def append_web_job_result(
        self,
        watcher,
        job_id: int,
        source_id: str,
        content: str,
        *,
        doc_url: str = "",
        status: str,
    ) -> None:
        conversation_id = self._conversation_for_watcher(watcher)
        if not conversation_id:
            return
        owner = self.conn.execute(
            "select owner_key from web_conversations where id = ?",
            (conversation_id,),
        ).fetchone()
        owner_key = str(owner["owner_key"] if owner else conversation_id)
        self.append_web_message(
            conversation_id,
            f"web-task:{owner_key}:{source_id or int(job_id)}",
            "assistant",
            content,
            kind="result",
            source_id=source_id,
            job_id=job_id,
            doc_url=doc_url,
            status=status,
            channel="system",
            actor_type="system",
        )

    def mirror_feishu_message(
        self,
        feishu_open_id: str,
        external_id: str,
        role: str,
        content: str,
        *,
        kind: str = "message",
    ) -> bool:
        identity = self.conn.execute(
            "select * from web_identities where feishu_open_id = ? order by bound_at asc, id asc limit 1",
            (str(feishu_open_id or "").strip(),),
        ).fetchone()
        if identity is None:
            return False
        conversation = self.ensure_web_conversation(dict(identity))
        self.append_web_message(
            int(conversation["id"]), external_id, role, content,
            kind=kind, channel="feishu", actor_type="user" if role == "user" else "system",
            actor_id=str(feishu_open_id or ""),
        )
        return True

    def issue_web_binding_code(self, web_identity_id: int, code_hash: str, ttl_minutes: int = 10) -> None:
        self.conn.execute("begin immediate")
        try:
            self.conn.execute(
                "delete from web_binding_codes where web_identity_id = ? or expires_at <= current_timestamp or used_at is not null",
                (int(web_identity_id),),
            )
            self.conn.execute(
                """
                insert into web_binding_codes (code_hash, web_identity_id, expires_at)
                values (?, ?, datetime('now', ?))
                """,
                (str(code_hash), int(web_identity_id), f"+{max(1, int(ttl_minutes))} minutes"),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def claim_web_binding_code(self, code_hash: str, feishu_open_id: str):
        clean_open_id = str(feishu_open_id or "").strip()
        if not clean_open_id:
            return None
        self.conn.execute("begin immediate")
        try:
            row = self.conn.execute(
                """
                select b.web_identity_id, w.public_id
                from web_binding_codes b
                join web_identities w on w.id = b.web_identity_id
                where b.code_hash = ? and b.used_at is null and b.expires_at > current_timestamp
                """,
                (str(code_hash),),
            ).fetchone()
            if row is None:
                self.conn.commit()
                return None
            identity_id = int(row["web_identity_id"])
            guest_sender = f"guest:{row['public_id']}"
            guest_owner = f"guest:{row['public_id']}"
            feishu_owner = f"feishu:{clean_open_id}"
            cached_name = self.conn.execute(
                "select display_name from user_identity_cache where sender_id = ?",
                (clean_open_id,),
            ).fetchone()
            display_name = str(cached_name["display_name"] if cached_name else "").strip() or "飞书用户"
            self.conn.execute(
                """
                update web_identities
                set account_type = 'feishu', feishu_open_id = ?, display_name = ?,
                    bound_at = current_timestamp, last_seen_at = current_timestamp
                where id = ?
                """,
                (clean_open_id, display_name, identity_id),
            )
            self.conn.execute(
                "update usage_events set sender_id = ? where sender_id = ? and chat_type = 'web'",
                (clean_open_id, guest_sender),
            )
            self.conn.execute(
                "update job_watchers set sender_id = ? where sender_id = ? and chat_type = 'web'",
                (clean_open_id, guest_sender),
            )
            self.conn.execute(
                "update web_binding_codes set used_at = current_timestamp where code_hash = ?",
                (str(code_hash),),
            )
            guest_conversation = self.conn.execute(
                "select id from web_conversations where owner_key = ?",
                (guest_owner,),
            ).fetchone()
            feishu_conversation = self.conn.execute(
                "select id from web_conversations where owner_key = ?",
                (feishu_owner,),
            ).fetchone()
            if guest_conversation and feishu_conversation:
                self.conn.execute(
                    "update web_messages set conversation_id = ? where conversation_id = ?",
                    (int(feishu_conversation["id"]), int(guest_conversation["id"])),
                )
                self.conn.execute(
                    "delete from web_conversations where id = ?",
                    (int(guest_conversation["id"]),),
                )
            elif guest_conversation:
                self.conn.execute(
                    "update web_conversations set owner_key = ?, updated_at = current_timestamp where id = ?",
                    (feishu_owner, int(guest_conversation["id"])),
                )
            elif not feishu_conversation:
                self.conn.execute(
                    "insert into web_conversations (owner_key) values (?)",
                    (feishu_owner,),
                )
            self.conn.commit()
            result = self.conn.execute("select * from web_identities where id = ?", (identity_id,)).fetchone()
            return dict(result) if result else None
        except Exception:
            self.conn.rollback()
            raise

    def list_web_submissions(self, public_id: str, limit: int = 30):
        rows = self.conn.execute(
            """
            select ue.*, q.id as job_id, q.status as job_status, q.stage, q.workflow_state
            from usage_events ue
            left join job_watchers w on w.usage_event_id = ue.id
            left join queue_jobs q on q.id = w.job_id
            where ue.chat_type = 'web' and ue.chat_id = ?
            order by ue.id desc
            limit ?
            """,
            (f"web:{str(public_id)}", max(1, min(100, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_web_submission_count(self, public_id: str, minutes: int = 10) -> int:
        row = self.conn.execute(
            """
            select count(*) as n from usage_events
            where chat_type = 'web' and chat_id = ? and created_at >= datetime('now', ?)
            """,
            (f"web:{str(public_id)}", f"-{max(1, int(minutes))} minutes"),
        ).fetchone()
        return int(row["n"] if row else 0)

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

    def list_feedback(self, limit: int = 50, status: str = "", since: str = "", sender_id: str = ""):
        where = []
        params = []
        if status:
            where.append("status = ?")
            params.append(status)
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if sender_id:
            where.append("sender_id = ?")
            params.append(sender_id)
        clause = " where " + " and ".join(where) if where else ""
        rows = self.conn.execute(
            f"""
            select id, event_id, message_id, chat_id, chat_type, sender_id, content, status, feedback_source, feedback_category, feedback_confidence, created_at
            from feedback
            {clause}
            order by id desc
            limit ?
            """,
            (*params, int(limit)),
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

    def get_service_status(self):
        row = self.conn.execute(
            "select mode, reason, expected_recovery_at, updated_by, updated_at from service_status where id = 1"
        ).fetchone()
        if row is None:
            return {
                "mode": "operational",
                "reason": "",
                "expected_recovery_at": "",
                "updated_by": "",
                "updated_at": "",
            }
        return dict(row)

    def set_service_status(
        self,
        mode: str,
        reason: str = "",
        expected_recovery_at: str = "",
        updated_by: str = "",
    ):
        clean_mode = str(mode or "").strip().lower()
        if clean_mode not in {"operational", "degraded", "maintenance", "outage"}:
            raise ValueError(f"Unsupported service mode: {mode}")
        clean_reason = str(reason or "").strip()[:1000]
        clean_eta = str(expected_recovery_at or "").strip()[:120]
        clean_by = str(updated_by or "").strip()[:120]
        if clean_mode != "operational" and not clean_reason:
            raise ValueError("reason is required when service is not operational")
        self.conn.execute(
            """
            insert into service_status (id, mode, reason, expected_recovery_at, updated_by, updated_at)
            values (1, ?, ?, ?, ?, current_timestamp)
            on conflict(id) do update set mode=excluded.mode, reason=excluded.reason,
                expected_recovery_at=excluded.expected_recovery_at, updated_by=excluded.updated_by,
                updated_at=current_timestamp
            """,
            (clean_mode, clean_reason, clean_eta, clean_by),
        )
        self.conn.commit()
        return self.get_service_status()


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

    def list_usage_events(self, limit: int = 50, since: str = "", sender_id: str = ""):
        where = []
        params = []
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if sender_id:
            where.append("sender_id = ?")
            params.append(sender_id)
        clause = " where " + " and ".join(where) if where else ""
        rows = self.conn.execute(
            f"""
            select id, event_id, message_id, chat_id, chat_type, sender_id, source_kind, source_id, source_url, title, status, doc_url, error, created_at, updated_at
            from usage_events
            {clause}
            order by id desc
            limit ?
            """,
            (*params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_usage_users(self, since: str = ""):
        where = ["sender_id <> ''"]
        params = []
        if since:
            where.append("created_at >= ?")
            params.append(since)
        rows = self.conn.execute(
            f"""
            select sender_id, count(*) as event_count, max(created_at) as last_seen_at
            from usage_events
            where {' and '.join(where)}
            group by sender_id
            order by last_seen_at desc, sender_id asc
            """,
            params,
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

    def find_retryable_queue_jobs(
        self,
        chat_id: str,
        sender_id: str = "",
        message_ids: tuple[str, ...] = (),
        dedupe_keys: tuple[str, ...] = (),
        limit: int = 20,
    ):
        """Resolve a retry command to durable jobs, not quoted message text.

        Related message IDs identify the original topic request.  Once its
        source keys are known, select the user's newest failed attempt for
        each source so a previous retry does not leave us replaying an older
        row.  Explicit source keys use the same ownership boundary.
        """
        keys = [str(item) for item in dedupe_keys if str(item)]
        if not keys:
            params: list[object] = [str(chat_id)]
            sender_clause = ""
            if sender_id:
                sender_clause = "and w.sender_id = ?"
                params.append(str(sender_id))
            message_clause = ""
            ids = [str(item) for item in message_ids if str(item)]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                message_clause = f"and w.message_id in ({placeholders})"
                params.extend(ids)
            elif not sender_id:
                return []
            rows = self.conn.execute(
                f"""
                select distinct q.dedupe_key
                from queue_jobs q
                join job_watchers w on w.job_id = q.id
                where w.chat_id = ? {sender_clause} {message_clause}
                order by q.id desc
                limit ?
                """,
                (*params, max(1, int(limit))),
            ).fetchall()
            keys = [str(row["dedupe_key"]) for row in rows]
            # A plain private-chat retry without thread metadata means the
            # most recent failed source, not every failure in that DM.
            if not ids and keys:
                keys = keys[:1]
        if not keys:
            return []

        output = []
        for key in keys[: max(1, int(limit))]:
            params = [key, str(chat_id)]
            sender_clause = ""
            if sender_id:
                sender_clause = "and w.sender_id = ?"
                params.append(str(sender_id))
            row = self.conn.execute(
                f"""
                select q.*
                from queue_jobs q
                where q.dedupe_key = ? and q.status = 'failed'
                  and exists (
                      select 1 from job_watchers w
                      where w.job_id = q.id and w.chat_id = ? {sender_clause}
                  )
                order by q.id desc
                limit 1
                """,
                params,
            ).fetchone()
            if row is not None:
                output.append(dict(row))
        return output

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
        suppress_progress_notifications: bool = False,
    ):
        dedupe_key = self.dedupe_key(source_kind, source_id)
        self.conn.execute("begin immediate")
        try:
            job = self.find_active_job(dedupe_key)
            created = False
            if job is None:
                cache_row = None
                if source_kind == "paper":
                    cache_row = self.conn.execute(
                        "select status from papers where paper_id = ?",
                        (source_id,),
                    ).fetchone()
                reusable = None
                if cache_row is not None and str(cache_row["status"] or "") in {"legacy", "cache_expired"}:
                    reusable = self.conn.execute(
                        "select id from queue_jobs where dedupe_key = ? order by id desc limit 1",
                        (dedupe_key,),
                    ).fetchone()
                if reusable is not None:
                    job_id = int(reusable["id"])
                    self.conn.execute(
                        """
                        update queue_jobs set
                            source_url=?, status='queued', priority=0, attempts=0,
                            title='', doc_url='', error='', started_at=null, finished_at=null,
                            worker_id='', heartbeat_at=null, stage='queued',
                            stage_updated_at=current_timestamp, workflow_state='queued',
                            state_version=state_version+1, last_event='cache_refresh',
                            checkpoint_json='', suppress_progress_notifications=?,
                            recovery_reason='', recovery_attempts=0, rebuild_pipeline=1,
                            auto_retry_count=0, updated_at=current_timestamp
                        where id=?
                        """,
                        (source_url, 1 if suppress_progress_notifications else 0, job_id),
                    )
                    self.conn.execute(
                        "insert into job_events (job_id, event_type, detail) values (?, 'cache_refresh', ?)",
                        (job_id, source_url),
                    )
                    created = True
                else:
                    cur = self.conn.execute(
                        """
                        insert into queue_jobs (
                            dedupe_key, source_kind, source_id, source_url, status,
                            suppress_progress_notifications
                        )
                        values (?, ?, ?, ?, 'queued', ?)
                        """,
                        (
                            dedupe_key,
                            source_kind,
                            source_id,
                            source_url,
                            1 if suppress_progress_notifications else 0,
                        ),
                    )
                    job_id = int(cur.lastrowid)
                    created = True
                    self.conn.execute("insert into job_events (job_id, event_type, detail) values (?, ?, ?)", (job_id, "enqueue", source_url))
            else:
                job_id = int(job["id"])
                self.conn.execute("insert into job_events (job_id, event_type, detail) values (?, ?, ?)", (job_id, "watch", sender_id))
                if suppress_progress_notifications:
                    self.conn.execute(
                        "update queue_jobs set suppress_progress_notifications = 1 where id = ? and status in ('queued', 'running')",
                        (job_id,),
                    )
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
            where status = 'running' or (status = 'queued' and id <= ?)
            """,
            (int(job_id),),
        ).fetchone()
        return int(pos["n"])

    def recent_job_duration_seconds(self, source_kind: str = "", limit: int = 12) -> int:
        """Return a robust recent completion estimate for queue ETA messages."""
        params = []
        kind_clause = ""
        if source_kind:
            kind_clause = "and source_kind = ?"
            params.append(str(source_kind))
        params.append(max(3, int(limit or 12)))
        rows = self.conn.execute(
            f"""
            select cast((julianday(finished_at) - julianday(started_at)) * 86400 as integer) as duration
            from queue_jobs
            where status = 'done'
              and started_at is not null
              and finished_at is not null
              and finished_at >= datetime('now', '-14 days')
              {kind_clause}
            order by finished_at desc
            limit ?
            """,
            params,
        ).fetchall()
        durations = sorted(
            int(row["duration"])
            for row in rows
            if row["duration"] is not None and 30 <= int(row["duration"]) <= 10800
        )
        if not durations:
            return 300
        middle = len(durations) // 2
        if len(durations) % 2:
            return durations[middle]
        return round((durations[middle - 1] + durations[middle]) / 2)

    def queued_count(self) -> int:
        row = self.conn.execute("select count(*) as n from queue_jobs where status = 'queued'").fetchone()
        return int(row["n"])

    def claim_next_queue_job(self, worker_id: str = "", source_kinds: tuple[str, ...] = ()):
        try:
            self.conn.execute("begin immediate")
            kinds = tuple(
                kind for kind in (str(item).strip().lower() for item in source_kinds)
                if kind in {"paper", "article"}
            )
            kind_clause = ""
            params: list[object] = []
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                kind_clause = f"and source_kind in ({placeholders})"
                params.extend(kinds)
            row = self.conn.execute(
                f"""
                select * from queue_jobs
                where status = 'queued'
                  {kind_clause}
                order by priority desc, id asc
                limit 1
                """,
                params,
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
                    updated_at = current_timestamp, stage_updated_at = current_timestamp,
                    suppress_progress_notifications = 0, recovery_attempts = 0
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

    def fail_queue_job(self, job_id: int, error: str, worker_id: str = "", doc_url: str = "") -> bool:
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
                set status = 'failed', error = ?,
                    doc_url = case when ? != '' then ? else doc_url end,
                    worker_id = '', stage = 'failed',
                    workflow_state = ?, finished_at = current_timestamp,
                    updated_at = current_timestamp, stage_updated_at = current_timestamp,
                    suppress_progress_notifications = 0, recovery_attempts = 0
                where id = ?
                """,
                (detail, str(doc_url or ""), str(doc_url or ""), next_state.value, int(job_id)),
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

    def recover_stale_queue_jobs(self, stale_minutes: int, max_recovery_attempts: int = 3) -> int:
        rows = self.conn.execute(
            """
            select id, worker_id, stage, heartbeat_at, started_at, recovery_attempts
            from queue_jobs
            where status = 'running'
              and (heartbeat_at is null or heartbeat_at < datetime('now', ?))
            """,
            (f"-{int(stale_minutes)} minutes",),
        ).fetchall()
        return self._recover_queue_job_rows(rows, "recover_stale", max_recovery_attempts)

    def recover_stale_queue_job(self, job_id: int, stale_minutes: int, max_recovery_attempts: int = 3) -> bool:
        rows = self.conn.execute(
            """
            select id, worker_id, stage, heartbeat_at, started_at, recovery_attempts
            from queue_jobs
            where id = ? and status = 'running'
              and (heartbeat_at is null or heartbeat_at < datetime('now', ?))
            """,
            (int(job_id), f"-{int(stale_minutes)} minutes"),
        ).fetchall()
        return bool(self._recover_queue_job_rows(rows, "project_agent_recover_stale", max_recovery_attempts))

    def recover_dead_worker_queue_jobs(self, host: str, is_pid_alive, max_recovery_attempts: int = 3) -> int:
        rows = self.conn.execute(
            """
            select id, worker_id, stage, heartbeat_at, started_at, recovery_attempts
            from queue_jobs
            where status = 'running' and worker_id != ''
            """
        ).fetchall()
        dead_rows = []
        for row in rows:
            parsed_host, pid = _parse_worker_host_pid(row["worker_id"] or "")
            if parsed_host == host and pid is not None and not is_pid_alive(pid):
                dead_rows.append(row)
        return self._recover_queue_job_rows(dead_rows, "recover_dead_worker", max_recovery_attempts)

    def _recover_queue_job_rows(self, rows, event_type: str, max_recovery_attempts: int = 3) -> int:
        if not rows:
            return 0
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute("begin immediate")
        try:
            current_rows = self.conn.execute(
                f"""
                select id, worker_id, stage, workflow_state, heartbeat_at, started_at, recovery_attempts
                from queue_jobs
                where id in ({placeholders}) and status = 'running'
                """,
                ids,
            ).fetchall()
            if not current_rows:
                self.conn.commit()
                return 0
            handled = 0
            for row in current_rows:
                detail = f"worker={row['worker_id'] or ''} stage={row['stage'] or ''} heartbeat={row['heartbeat_at'] or ''} started={row['started_at'] or ''}"
                from_state = str(row["workflow_state"] or "").strip()
                if not from_state:
                    try:
                        from_state = state_from_legacy("running", row["stage"] or "").value
                    except ValueError:
                        from_state = WorkflowState.CLAIMED.value
                recovery_count = int(row["recovery_attempts"] or 0)
                if recovery_count >= max(0, int(max_recovery_attempts)):
                    recovery_reason = f"服务异常中断，自动恢复次数已达上限 {max_recovery_attempts}（{event_type}）：{detail.strip()}"
                    self.conn.execute(
                        """
                        update queue_jobs
                        set status = 'failed', workflow_state = 'failed', state_version = coalesce(state_version, 0) + 1,
                            error = ?, recovery_reason = ?, worker_id = '', stage = 'recovery_exhausted',
                            heartbeat_at = null, finished_at = current_timestamp, updated_at = current_timestamp,
                            stage_updated_at = current_timestamp, last_event = 'recovery_exhausted',
                            suppress_progress_notifications = 1
                        where id = ? and status = 'running'
                        """,
                        (recovery_reason, recovery_reason, int(row["id"])),
                    )
                    self.conn.execute("update job_watchers set notified = 1 where job_id = ?", (int(row["id"]),))
                    self.conn.execute(
                        """
                        update usage_events set status = 'failed', error = ?, updated_at = current_timestamp
                        where id in (select usage_event_id from job_watchers where job_id = ? and usage_event_id != 0)
                        """,
                        (recovery_reason, int(row["id"])),
                    )
                    self.conn.execute(
                        "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
                        (int(row["id"]), "recovery_exhausted", recovery_reason),
                    )
                    handled += 1
                    continue
                recovery_reason = f"服务异常中断后自动恢复（{event_type}）：{detail.strip()}"
                self.conn.execute(
                    """
                    update queue_jobs
                    set status = 'queued', worker_id = '', stage = 'recovered', started_at = null,
                        heartbeat_at = null, updated_at = current_timestamp, stage_updated_at = current_timestamp,
                        workflow_state = 'queued', state_version = coalesce(state_version, 0) + 1,
                        last_event = 'recover', suppress_progress_notifications = 1,
                        error = ?, recovery_reason = ?, recovery_attempts = coalesce(recovery_attempts, 0) + 1
                    where id = ? and status = 'running'
                    """,
                    (recovery_reason, recovery_reason, int(row["id"])),
                )
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
                handled += 1
            self.conn.commit()
            return handled
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

    def list_queue_jobs(self, limit: int = 50, status: str = "", since: str = "", sender_id: str = ""):
        # Queue rows are created before arXiv metadata is available. Resolve
        # the title from the durable paper/article record when the worker has
        # reached that point, while retaining the queue's final title.
        title_expr = """
            coalesce(
                nullif(q.title, ''),
                nullif(p.title, ''),
                nullif(d.title, ''),
                (
                    select ue.title
                    from usage_events ue
                    where ue.source_kind = q.source_kind and ue.source_id = q.source_id
                      and ue.title <> ''
                    order by ue.id desc
                    limit 1
                ),
                ''
            ) as resolved_title
        """
        watcher_expr = """
            coalesce(
                (
                    select w.sender_id
                    from job_watchers w
                    where w.job_id = q.id and w.sender_id <> ''
                    order by w.id asc
                    limit 1
                ),
                ''
            ) as sender_id
        """
        where = []
        params = []
        if status:
            where.append("q.status = ?")
            params.append(status)
        if since:
            where.append("q.created_at >= ?")
            params.append(since)
        if sender_id:
            where.append("exists (select 1 from job_watchers wf where wf.job_id = q.id and wf.sender_id = ?)")
            params.append(sender_id)
        clause = " where " + " and ".join(where) if where else ""
        rows = self.conn.execute(
            f"""
            select q.*, {title_expr}, {watcher_expr}
            from queue_jobs q
            left join papers p on q.source_kind = 'paper' and p.paper_id = q.source_id
            left join documents d on q.source_kind = 'article' and d.doc_id = q.source_id
            {clause}
            order by q.id desc
            limit ?
            """,
            (*params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_queue_job(self, job_id: int):
        row = self.conn.execute(
            "select * from queue_jobs where id = ?",
            (int(job_id),),
        ).fetchone()
        return dict(row) if row is not None else None


    def add_job_event(self, job_id: int, event_type: str, detail: str = "") -> int:
        cur = self.conn.execute(
            "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
            (int(job_id), str(event_type), str(detail)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_job_events(self, job_id: int = 0, limit: int = 100, since: str = "", sender_id: str = ""):
        where = []
        params = []
        if job_id:
            where.append("e.job_id = ?")
            params.append(int(job_id))
        if since:
            where.append("e.created_at >= ?")
            params.append(since)
        if sender_id:
            where.append("exists (select 1 from job_watchers wf where wf.job_id = e.job_id and wf.sender_id = ?)")
            params.append(sender_id)
        clause = " where " + " and ".join(where) if where else ""
        rows = self.conn.execute(
            f"""
            select e.id, e.job_id, e.event_type, e.detail, e.created_at,
                   coalesce(q.source_kind, '') as source_kind,
                   coalesce(q.source_id, '') as source_id,
                   coalesce(
                       (select w.sender_id from job_watchers w where w.job_id = e.job_id and w.sender_id <> '' order by w.id asc limit 1),
                       ''
                   ) as sender_id
            from job_events e
            left join queue_jobs q on q.id = e.job_id
            {clause}
            order by e.id desc
            limit ?
            """,
            (*params, int(limit)),
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

    def queue_retry_stats(self, job_ids) -> dict[int, dict[str, int]]:
        ids = sorted({int(job_id) for job_id in job_ids if int(job_id or 0) > 0})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""
            select job_id,
                   sum(case when event_type in ('web_retry', 'topic_retry', 'project_agent_retry', 'retry') then 1 else 0 end) as user_retries,
                   sum(case when event_type = 'auto_retry' then 1 else 0 end) as auto_retries,
                   sum(case when event_type in ('recover_dead_worker', 'recover_stale', 'project_agent_recover_stale') then 1 else 0 end) as service_recoveries
            from job_events
            where job_id in ({placeholders})
            group by job_id
            """,
            ids,
        ).fetchall()
        return {
            int(row["job_id"]): {
                "user_retries": int(row["user_retries"] or 0),
                "auto_retries": int(row["auto_retries"] or 0),
                "service_recoveries": int(row["service_recoveries"] or 0),
            }
            for row in rows
        }

    def retry_queue_job(
        self,
        job_id: int,
        reason: str = "manual retry",
        event_type: str = "retry",
        suppress_progress_notifications: bool = False,
        rebuild_pipeline: bool = True,
    ) -> bool:
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
                reason,
                int(row["state_version"] or 0) + 1,
            )
            self.conn.execute(
                """
                update queue_jobs
                set status = 'queued', error = '', worker_id = '', stage = 'retry_queued',
                    started_at = null, finished_at = null, heartbeat_at = null,
                    updated_at = current_timestamp, stage_updated_at = current_timestamp,
                    suppress_progress_notifications = ?, recovery_reason = '', recovery_attempts = 0,
                    auto_retry_count = case when ? then auto_retry_count + 1 else 0 end,
                    checkpoint_json = case when ? then '' else checkpoint_json end,
                    rebuild_pipeline = ?
                where id = ? and workflow_state = 'queued'
                """,
                (
                    1 if suppress_progress_notifications else 0,
                    1 if str(event_type or "") == "auto_retry" else 0,
                    1 if rebuild_pipeline else 0,
                    1 if rebuild_pipeline else 0,
                    int(job_id),
                ),
            )
            self.conn.execute("update job_watchers set notified = 0 where job_id = ?", (int(job_id),))
            self.conn.execute(
                """
                update usage_events
                set status = 'queued', error = '', updated_at = current_timestamp
                where id in (
                    select usage_event_id from job_watchers
                    where job_id = ? and usage_event_id != 0
                )
                """,
                (int(job_id),),
            )
            self._insert_transition_event(job_id, result, reason)
            self.conn.execute(
                "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
                (
                    int(job_id),
                    str(event_type or "retry"),
                    f"{str(reason)[:900]} retry_mode={'rebuild' if rebuild_pipeline else 'resume'}",
                ),
            )
            self.conn.commit()
            return True
        except (InvalidWorkflowTransition, ValueError):
            self.conn.rollback()
            return False
        except Exception:
            self.conn.rollback()
            raise

    def requeue_interrupted_job(self, job_id: int, reason: str) -> bool:
        """Requeue one job recovered by an operator after an infrastructure stop.

        This deliberately does not make the terminal ``cancelled`` state
        generally retryable. It is an explicit, audited recovery operation for
        a task that was stopped by the service rather than by its user.
        """
        detail = str(reason or "service interruption recovery")[:1000]
        self.conn.execute("begin immediate")
        try:
            row = self.conn.execute("select * from queue_jobs where id = ?", (int(job_id),)).fetchone()
            if row is None or str(row["workflow_state"] or "") != WorkflowState.CANCELLED.value:
                self.conn.commit()
                return False
            version = int(row["state_version"] or 0) + 1
            self.conn.execute(
                """
                update queue_jobs
                set status = 'queued', workflow_state = 'queued', state_version = ?,
                    error = ?, recovery_reason = ?, worker_id = '', stage = 'recovered',
                    started_at = null, finished_at = null, heartbeat_at = null,
                    updated_at = current_timestamp, stage_updated_at = current_timestamp,
                    last_event = 'recover', suppress_progress_notifications = 1, recovery_attempts = 0
                where id = ? and workflow_state = 'cancelled'
                """,
                (version, detail, detail, int(job_id)),
            )
            self.conn.execute("update job_watchers set notified = 0 where job_id = ?", (int(job_id),))
            self.conn.execute(
                "insert into job_events (job_id, event_type, detail) values (?, ?, ?)",
                (int(job_id), "operator_recovery", detail),
            )
            self.conn.commit()
            return True
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
                set status = ?, stage = ?, workflow_state = ?, state_version = ?, last_event = ?, doc_url = ?, checkpoint_json = ?,
                    updated_at = current_timestamp, stage_updated_at = current_timestamp
                where id = ?
                """,
                (
                    queue_status_for_state(state),
                    legacy_stage_for_state(state),
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
            set status = ?, stage = ?, workflow_state = ?, state_version = ?, last_event = ?,
                updated_at = current_timestamp, stage_updated_at = current_timestamp
            where id = ?
            """,
            (
                queue_status_for_state(state),
                legacy_stage_for_state(state),
                state.value,
                int(version),
                event.value,
                int(job_id),
            ),
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

    def list_review_issues(
        self,
        limit: int = 50,
        source_kind: str = "",
        source_id: str = "",
        since: str = "",
        sender_id: str = "",
    ):
        where = []
        params = []
        if source_kind:
            where.append("source_kind = ?")
            params.append(source_kind)
        if source_id:
            where.append("source_id = ?")
            params.append(source_id)
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if sender_id:
            where.append(
                "exists (select 1 from usage_events ue where ue.source_kind = review_issues.source_kind "
                "and ue.source_id = review_issues.source_id and ue.sender_id = ?)"
            )
            params.append(sender_id)
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
