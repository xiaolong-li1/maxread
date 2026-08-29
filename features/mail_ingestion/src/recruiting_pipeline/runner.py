from __future__ import annotations

import json
import hashlib
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .academic import normalize_academic_display
from .base_sync import BaseSync
from .config import PipelineSettings
from .docs_sync import DocsSync
from .external_attachments import download_external_pdfs
from .llm import RecruitingLLM
from .models import CandidateFields, ProcessedThread, StoredMessage, ThreadEnvelope
from .pdf_text import extract_pdf_text
from .retry import is_transient_error, retry_call
from .store import PipelineStore
from .threading import build_envelope, candidate_address, read_headers, thread_key


FOLDER_STATUS = {"面试寄": "面试资格", "面试通过": "面试通过"}


@dataclass
class RunStats:
    run_id: str
    scanned_messages: int = 0
    new_threads: int = 0
    updated_threads: int = 0
    failed_threads: int = 0
    documents_created: int = 0
    documents_updated: int = 0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class RecruitingRunner:
    def __init__(self, settings: PipelineSettings, *, llm: RecruitingLLM | None = None, no_docs: bool = False, dry_run: bool = False):
        self.settings = settings
        self.store = PipelineStore(settings.db_path)
        self.store.initialize()
        self.llm = llm or (RecruitingLLM(
            settings.api_key,
            settings.api_base_url,
            settings.model,
            settings.reasoning_effort,
            settings.api_timeout,
            settings.retry_attempts,
            settings.retry_base_seconds,
        ) if settings.api_key else None)
        self.base = BaseSync(settings)
        self.docs = None if no_docs else DocsSync(settings)
        self.dry_run = dry_run
        self._envelope_cache: dict[str, ThreadEnvelope] = {}
        self._mailbox_addresses = settings.mailbox_addresses or (settings.mailbox_address,)

    def run_once(
        self,
        *,
        skip_scan: bool = False,
        max_threads: int | None = None,
        only_candidates: bool = False,
        reprocess: bool = False,
        since_days: int | None = None,
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        stats = RunStats(run_id=run_id)
        self.store.recover_stale_runs(self.settings.run_stale_minutes)
        self.store.start_run(run_id)
        try:
            if not skip_scan:
                self._scan_mailbox()
            envelopes, changed_keys = self._load_threads()
            self._envelope_cache = envelopes
            if reprocess:
                changed_keys = set(envelopes)
            if only_candidates:
                changed_keys = {key for key in changed_keys if key in envelopes and not self._is_obvious_other(envelopes[key])}
            if since_days is not None:
                changed_keys = {
                    key
                    for key in changed_keys
                    if key in envelopes and _within_days(envelopes[key].latest_time, since_days)
                }
            if max_threads is not None:
                selected = sorted((envelopes[key] for key in changed_keys if key in envelopes), key=lambda item: item.latest_time or "", reverse=True)[:max_threads]
                changed_keys = {item.key for item in selected}
            stats.scanned_messages = sum(len(envelope.messages) for envelope in envelopes.values())
            stats.new_threads = sum(1 for key in changed_keys if self.store.get_thread(key) is None)
            stats.updated_threads = len(changed_keys) - stats.new_threads

            for thread, envelope, preparation_error in self._extract_changed(envelopes, changed_keys):
                if preparation_error is not None or thread is None:
                    stats.failed_threads += 1
                    self._record_preparation_failure(envelope, preparation_error or RuntimeError("thread preparation failed"))
                    continue
                try:
                    result = self._sync_thread(thread)
                    stats.documents_created += int(result.get("document_created", False))
                    stats.documents_updated += int(result.get("document_updated", False))
                except Exception as exc:  # one candidate must not block the batch
                    stats.failed_threads += 1
                    self._record_thread_failure(thread, exc)
            run_counts = {key: value for key, value in stats.as_dict().items() if key in {"scanned_messages", "new_threads", "updated_threads", "failed_threads"}}
            self.store.finish_run(run_id, "completed", **run_counts, error="")
            return {"ok": True, **stats.as_dict()}
        except Exception as exc:
            run_counts = {key: value for key, value in stats.as_dict().items() if key in {"scanned_messages", "new_threads", "updated_threads", "failed_threads"}}
            self.store.finish_run(run_id, "failed", **run_counts, error=str(exc)[:1000])
            raise

    def _scan_mailbox(self) -> None:
        account_envs = self.settings.mailbox_env_files or (self.settings.mailbox_env_file,)
        for account_env in account_envs:
            command = [
                str(self.settings.root / "features/mail_ingestion/bin/mail-collector"),
                "scan",
                "--env-file",
                str(account_env),
                "--all-folders",
                "--include-system-folders",
                "--limit",
                str(self.settings.scan_limit),
            ]
            for folder in ("arXiv", "Drafts", "Outbox", "Junk", "Junk E-mail", "Virus Items", "Deleted", "Trash", "Notes"):
                command.extend(("--exclude-folder", folder))
            completed = subprocess.run(command, cwd=self.settings.collector_root, capture_output=True, text=True, timeout=600)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"mail scan failed for {account_env.name}: "
                    + (completed.stderr.strip() or completed.stdout.strip() or "collector failed")
                )

    def _load_threads(self) -> tuple[dict[str, ThreadEnvelope], set[str]]:
        messages = self.store.messages()
        processing = self.store.message_processing_state()
        grouped: dict[str, list[tuple[StoredMessage, Any]]] = {}
        changed: set[str] = set()
        message_id_to_key: dict[str, str] = {}
        for message in messages:
            try:
                headers = read_headers(message.raw_path)
            except (OSError, ValueError):
                continue
            key = next(
                (
                    message_id_to_key[parent]
                    for parent in (headers.message_id, headers.in_reply_to, *headers.references)
                    if parent in message_id_to_key
                ),
                thread_key(message, headers, self._mailbox_addresses),
            )
            # Preserve the canonical Message-ID mapping so a reply from a
            # group member's personal address is joined to the candidate's
            # thread instead of becoming a second candidate.
            if headers.message_id:
                message_id_to_key[headers.message_id] = key
            direction = "outgoing" if headers.sender != candidate_address(headers, self._mailbox_addresses, message.body_text) else "incoming"
            self.store.upsert_message(message.id, key, direction, message.mailbox)
            grouped.setdefault(key, []).append((message, headers))
            old = processing.get(message.id)
            if old is None or not old[1] or old[0] != key:
                changed.add(key)
        # A backfill Base write can fail after durable mail/document state is
        # complete. The explicit pending marker retries only that repair;
        # unrelated historical rows with an intentionally absent mapping stay
        # dormant until a genuinely new message changes their thread.
        for key in grouped:
            row = self.store.get_thread(key)
            if (
                row is not None
                and not str(row["base_record_id"] or "")
                and str(row["status"] or "") == "base_backfill_pending"
            ):
                changed.add(key)
        return {key: build_envelope(items, self._mailbox_addresses, key=key) for key, items in grouped.items()}, changed

    def _extract_changed(self, envelopes: dict[str, ThreadEnvelope], changed_keys: set[str]):
        candidates: list[ThreadEnvelope] = []
        for key in changed_keys:
            envelope = envelopes.get(key)
            if not envelope:
                continue
            # An isolated message from our own mailbox is only an audit event;
            # it must not create a fake candidate record. If a prior incoming
            # thread exists, it is still appended and can update that thread.
            if not envelope.incoming and self.store.get_thread(key) is None:
                if not self.dry_run:
                    for message in envelope.messages:
                        self.store.mark_message_processed(message.id)
                continue
            candidates.append(envelope)
        if not candidates:
            return
        with ThreadPoolExecutor(max_workers=self.settings.llm_concurrency) as executor:
            futures = {executor.submit(self._prepare_thread, envelope): envelope for envelope in candidates}
            for future in as_completed(futures):
                envelope = futures[future]
                try:
                    yield future.result(), envelope, None
                except Exception as exc:
                    yield None, envelope, exc

    def _record_preparation_failure(self, envelope: ThreadEnvelope, error: BaseException) -> None:
        row = self.store.get_thread(envelope.key)
        previous = self.store.fields_from_row(row)
        fields = previous or (
            _other_fields(envelope)
            if self._is_obvious_other(envelope)
            else CandidateFields(
                name=envelope.candidate_address,
                mail_type="candidate",
                purpose_summary=envelope.subject,
            ).normalized()
        )
        attempts = int(row["attempt_count"] if row else 0) + 1
        self.store.save_thread(
            envelope.key,
            envelope.candidate_address,
            envelope.subject,
            fields,
            latest_time=_base_time(envelope.latest_time) or "",
            status="extract_failed",
            attempt_count=attempts,
            last_error=str(error)[:1000],
        )

    def _prepare_thread(self, envelope: ThreadEnvelope) -> ProcessedThread:
        previous_row = self.store.get_thread(envelope.key)
        previous_fields = self.store.fields_from_row(previous_row)
        self._download_external(envelope)
        pdf_texts = self._pdf_texts(envelope)
        if self._is_obvious_other(envelope):
            fields = _other_fields(envelope)
        elif self.llm:
            fields = self.llm.extract(envelope, pdf_texts, previous=previous_fields)
            if fields.mail_type == "other":
                fields = _other_fields(envelope, summary=fields.purpose_summary)
        elif previous_fields:
            fields = previous_fields
        else:
            fields = CandidateFields(name=envelope.candidate_address, purpose_summary=envelope.subject).normalized()
        if fields.mail_type == "candidate":
            fields.academic_display = normalize_academic_display(
                fields.academic_display,
                self._academic_material(envelope, pdf_texts),
            )
        fields.source_accounts = sorted({_source_account_label(account) for account in envelope.source_accounts})
        folder_status = next((FOLDER_STATUS[name] for name in ("面试通过", "面试寄") if name in envelope.folders), None)
        assigned = self.settings.mark_interview_assigned if "面试寄" in envelope.folders else None
        if "面试通过" in envelope.folders:
            assigned = True
        return ProcessedThread(
            thread_key=envelope.key,
            candidate_address=envelope.candidate_address,
            latest_time=_base_time(envelope.latest_time),
            fields=fields,
            folder_status=folder_status,
            interview_assigned=assigned,
            document_id=str(previous_row["doc_id"] or "") if previous_row else None,
            document_url=str(previous_row["doc_url"] or "") if previous_row else None,
            changed=True,
        )

    def repair_academics(self, *, apply: bool = False, since_days: int | None = None) -> dict[str, Any]:
        """Audit and optionally repair academic summaries without another LLM call."""

        envelopes, _changed = self._load_threads()
        changes: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        ordered = sorted(envelopes.items(), key=lambda item: item[1].latest_time or "", reverse=True)
        for key, envelope in ordered:
            if since_days is not None and not _within_days(envelope.latest_time, since_days):
                continue
            row = self.store.get_thread(key)
            fields = self.store.fields_from_row(row)
            if row is None or fields is None or fields.mail_type != "candidate":
                continue
            try:
                pdf_texts = self._pdf_texts(envelope)
                repaired = normalize_academic_display(
                    fields.academic_display,
                    self._academic_material(envelope, pdf_texts),
                )
                if repaired == fields.academic_display:
                    continue
                changes.append({"thread_key": key, "name": fields.name, "before": fields.academic_display, "after": repaired})
                if not apply:
                    continue
                fields.academic_display = repaired
                self._sync_thread(
                    ProcessedThread(
                        thread_key=key,
                        candidate_address=str(row["candidate_address"] or envelope.candidate_address),
                        latest_time=str(row["latest_time"] or _base_time(envelope.latest_time) or ""),
                        fields=fields,
                        folder_status=None,
                        interview_assigned=None,
                        document_id=str(row["doc_id"] or "") or None,
                        document_url=str(row["doc_url"] or "") or None,
                        changed=True,
                    )
                )
            except Exception as exc:
                failures.append({"thread_key": key, "name": fields.name, "error": str(exc)[:500]})
        return {
            "ok": not failures,
            "dry_run": not apply,
            "changed": len(changes),
            "failed": len(failures),
            "changes": changes,
            "failures": failures,
        }

    @staticmethod
    def _academic_material(envelope: ThreadEnvelope, pdf_texts: dict[str, str]) -> str:
        return "\n".join(
            [message.body_text for message in envelope.messages]
            + [text for text in pdf_texts.values() if text]
        )

    def _sync_thread(self, thread: ProcessedThread) -> dict[str, bool]:
        envelope = self._envelope_cache.get(thread.thread_key) or self._load_envelope(thread.thread_key)
        self._download_external(envelope)
        row = self.store.get_thread(thread.thread_key)
        previous_fields = self.store.fields_from_row(row)
        previous_status = str(row["screening_status"] if row and "screening_status" in row.keys() else "未筛选") if row else "未筛选"
        previous_result = str(row["interview_result"] if row and "interview_result" in row.keys() else "未开始") if row else "未开始"
        previous_assigned = bool(row["interview_assigned"]) if row else False
        status = thread.folder_status or previous_status or "未筛选"
        if previous_status == "未筛选" and thread.fields.rejection_recommendation == "未通过":
            status = "未通过"
        assigned = thread.interview_assigned if thread.interview_assigned is not None else previous_assigned
        base_record_id = str(row["base_record_id"] or "") if row else ""
        if not base_record_id:
            base_record_id = self.base.find_existing(
                thread.fields.name,
                thread.latest_time,
                thread.document_url,
            ) or ""
        base_state = self.base.current_state(base_record_id)
        if base_state:
            status = _merge_status(previous_status, str(base_state.get("screening_status") or ""), status)
            assigned = bool(base_state.get("interview_assigned")) or assigned
            base_result = str(base_state.get("interview_result") or "")
            if base_result and base_result != "未开始":
                previous_result = base_result
        if self.dry_run:
            return {"document_created": False, "document_updated": False}
        document_created = False
        document_updated = False
        summary_changed = False
        processing_state = self.store.message_processing_state()
        pending_messages = [message for message in envelope.messages if not processing_state.get(message.id, ("", False))[1]]
        if self.docs:
            if thread.document_id:
                self.docs.update_title(thread.document_id, f"{thread.fields.name}｜真实邮件材料")
                summary_changed = bool(row and (previous_fields != thread.fields or str(row["latest_time"] or "") != str(thread.latest_time or "")))
                if summary_changed:
                    document_updated = self.docs.replace_summary(thread.document_id, thread.fields, thread.latest_time) or document_updated
                # A reprocess must be idempotent: never append the complete
                # structured summary again.  Only genuinely new messages and
                # their newly materialized attachments are appended.
                if pending_messages:
                    self.docs.append(thread.document_id, self._document_delta_content(envelope, pending_messages))
                    document_updated = True
                document_id, document_url = thread.document_id, thread.document_url
            else:
                content = self._document_content(envelope, thread.fields, None)
                document_id, document_url = self.docs.create(f"{thread.fields.name}｜真实邮件材料", content)
                self.docs.update_title(document_id, f"{thread.fields.name}｜真实邮件材料")
                document_created = True
            attachment_messages = pending_messages if thread.document_id else list(envelope.messages)
            uploaded_digests = self.store.uploaded_attachment_digests(thread.thread_key)
            uploaded_any = False
            for path in self._attachment_paths(envelope, attachment_messages):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest in uploaded_digests:
                    continue
                self.docs.insert_file(document_id, path)
                self.store.mark_attachment_uploaded(thread.thread_key, digest, path.name, document_id)
                uploaded_digests.add(digest)
                uploaded_any = True
            if summary_changed or pending_messages or uploaded_any:
                # Some document update calls replay tokenized media blocks;
                # remove any replayed file cards before the run completes.
                self.docs.deduplicate_files(document_id)
        else:
            document_id, document_url = thread.document_id, thread.document_url

        base_result = self.base.upsert(
            record_id=base_record_id or None,
            fields=thread.fields,
            latest_time=thread.latest_time,
            document_url=document_url,
            status=status,
            interview_assigned=assigned,
            interview_result=previous_result,
            has_replied=bool(envelope.outgoing),
        )
        self.store.save_thread(
            thread.thread_key,
            thread.candidate_address,
            envelope.subject,
            thread.fields,
            base_record_id=base_result.record_id,
            doc_id=document_id or "",
            doc_url=document_url or "",
            latest_time=thread.latest_time or "",
            last_incoming_time=max((item.received_at or "" for item in envelope.incoming), default=""),
            last_outgoing_time=max((item.received_at or "" for item in envelope.outgoing), default=""),
            screening_status=status,
            interview_assigned=int(assigned),
            interview_result=previous_result,
            status="active",
            last_error="",
        )
        for message in envelope.messages:
            self.store.mark_message_processed(message.id)
        return {"document_created": document_created, "document_updated": document_updated}

    def _record_thread_failure(self, thread: ProcessedThread, error: BaseException) -> None:
        row = self.store.get_thread(thread.thread_key)
        attempts = int(row["attempt_count"] if row else 0) + 1
        self.store.save_thread(thread.thread_key, thread.candidate_address, thread.thread_key, thread.fields, attempt_count=attempts, last_error=str(error)[:1000])

    def _load_envelope(self, key: str) -> ThreadEnvelope:
        messages = self.store.messages()
        grouped: dict[str, list[tuple[StoredMessage, Any]]] = {}
        message_id_to_key: dict[str, str] = {}
        for message in messages:
            try:
                headers = read_headers(message.raw_path)
            except (OSError, ValueError):
                continue
            resolved = next(
                (
                    message_id_to_key[parent]
                    for parent in (headers.message_id, headers.in_reply_to, *headers.references)
                    if parent in message_id_to_key
                ),
                thread_key(message, headers, self._mailbox_addresses),
            )
            if headers.message_id:
                message_id_to_key[headers.message_id] = resolved
            grouped.setdefault(resolved, []).append((message, headers))
        items = grouped.get(key, [])
        return build_envelope(items, self._mailbox_addresses, key=key)

    def _pdf_texts(self, envelope: ThreadEnvelope) -> dict[str, str]:
        # Every local attachment is uploaded to the material document, but
        # only PDFs are sent through the text extractor/LLM context.
        paths = [path for path in self._attachment_paths(envelope) if path.suffix.lower() == ".pdf"]
        if not paths:
            return {}
        unique_paths: list[Path] = []
        seen_digests: set[str] = set()
        for path in paths:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest not in seen_digests:
                seen_digests.add(digest)
                unique_paths.append(path)
        with ThreadPoolExecutor(max_workers=self.settings.pdf_workers) as executor:
            pairs = list(executor.map(lambda path: (path.name, extract_pdf_text(path)), unique_paths))
        return dict(pairs)

    def _attachment_paths(self, envelope: ThreadEnvelope, messages: list[StoredMessage] | None = None) -> list[Path]:
        paths: list[Path] = []
        seen_digests: set[str] = set()
        for message in messages or list(envelope.messages):
            for path in message.attachments:
                resolved = path
                if not resolved.exists():
                    candidate_dir = self.settings.data_dir / "messages" / message.source_uid
                    matches = list(candidate_dir.glob(f"*{path.name}"))
                    if matches:
                        resolved = matches[0]
                # Keep all locally materialized MIME attachments.  Filtering
                # to PDF belongs in _pdf_texts; upload must preserve images,
                # transcripts, slide decks, and other candidate materials too.
                if resolved.exists() and resolved.is_file():
                    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
                    if digest not in seen_digests:
                        seen_digests.add(digest)
                        paths.append(resolved)
            external_dir = message.raw_path.parent / "external-attachments"
            if external_dir.exists():
                for resolved in sorted(external_dir.glob("*.pdf")):
                    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
                    if digest not in seen_digests:
                        seen_digests.add(digest)
                        paths.append(resolved)
        return paths

    def _download_external(self, envelope: ThreadEnvelope) -> None:
        for message in envelope.messages:
            target = message.raw_path.parent / "external-attachments"
            download_external_pdfs(message.raw_path, target)

    @staticmethod
    def _is_obvious_other(envelope: ThreadEnvelope) -> bool:
        senders = " ".join(item.sender_address for item in envelope.messages).casefold()
        if any(
            token in senders
            for token in (
                "accountprotection.microsoft.com",
                "azure-noreply@microsoft.com",
                "noreply@microsoft.com",
                "promomail.microsoft.com",
            )
        ):
            return True
        combined = (envelope.subject + "\n" + "\n".join(item.body_text[:1000] for item in envelope.incoming)).casefold()
        return not envelope.incoming or any(
            token in combined
            for token in (
                "account-security-noreply",
                "azure free account",
                "azure free trial",
                "quickstart guides for popular azure services",
                "云程奖",
            )
        )

    def _document_content(self, envelope: ThreadEnvelope, fields: CandidateFields, messages: list[StoredMessage] | None = None) -> str:
        parts = [
            "## 结构化摘要",
            f"- 姓名：{fields.name}",
            f"- 邮件类型：{'其他' if fields.mail_type == 'other' else '候选人来信'}",
            f"- 院校 / 就读信息：{fields.school_study_display}",
            f"- 院校：{fields.school}",
            f"- 专业信息：{fields.major}",
            f"- 申请项目：{'、'.join(fields.projects)}",
            f"- 学业表现：{fields.academic_display}",
            f"- 排名：{fields.rank}",
            f"- 院校标签：985={fields.is_985}；C9={fields.is_c9}",
            f"- 来源邮箱：{'、'.join(fields.source_accounts)}",
            "- 申请目的 / 科研摘要：",
            fields.purpose_summary,
            f"- 最新邮件时间：{envelope.latest_time or 'unknown'}",
            "",
            "## 新增邮件线程片段",
        ]
        for message in messages or list(envelope.messages):
            direction = "我方回复" if message in envelope.outgoing else "候选人来信"
            parts.extend([
                f"### UID {message.source_uid}｜{direction}｜{message.received_at or 'unknown'}｜{message.subject}",
                f"发件人：{message.sender_address}",
                "```text",
                message.body_text[:50000],
                "```",
                "",
            ])
        names = [path.name for path in self._attachment_paths(envelope, messages)]
        if names:
            attachment_lines = [f"- {name}" for name in names]
        elif any(token in message.body_text for message in envelope.messages for token in ("超大附件", "在线预览", "下载")):
            attachment_lines = ["- 邮件包含邮箱超大附件外链；IMAP 未同步文件实体，请从原邮件打开链接下载。"]
        else:
            attachment_lines = ["- 未发现可下载的本地附件。"]
        parts.extend(["## 附件", "", *attachment_lines, "", "> 本文档由 ZIP Lab 招聘邮箱只读管线生成。"])
        return "\n".join(parts)

    def _document_delta_content(self, envelope: ThreadEnvelope, messages: list[StoredMessage]) -> str:
        """Render only newly arrived thread material for an existing doc."""
        parts = ["## 新增邮件线程片段", ""]
        for message in messages:
            direction = "我方回复" if message in envelope.outgoing else "候选人来信"
            parts.extend([
                f"### UID {message.source_uid}｜{direction}｜{message.received_at or 'unknown'}｜{message.subject}",
                f"发件人：{message.sender_address}",
                "```text",
                message.body_text[:50000],
                "```",
                "",
            ])
        names = [path.name for path in self._attachment_paths(envelope, messages)]
        if names:
            parts.extend(["## 新增附件", "", *[f"- {name}" for name in names], ""])
        return "\n".join(parts)


def _base_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return value[:16].replace("T", " ")


def _other_fields(envelope: ThreadEnvelope, summary: str | None = None) -> CandidateFields:
    title = (envelope.subject or "其他邮件").strip()[:100] or "其他邮件"
    return CandidateFields(
        name=title,
        school="—",
        major="—",
        mail_type="other",
        projects=["unknown"],
        academic_display="—",
        purpose_summary=(summary or title).strip()[:500],
    ).normalized()


def _source_account_label(address: str) -> str:
    normalized = str(address or "").strip().casefold()
    if normalized == "bohan.zhuang@zju.edu.cn":
        return "Bohan"
    if normalized:
        return "ZIP Lab"
    raise ValueError("source mailbox is missing")


def _within_days(value: str | None, days: int, now: datetime | None = None) -> bool:
    if not value:
        return False
    now = now or datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) >= now.astimezone(UTC) - timedelta(days=max(1, int(days)))


def _merge_status(previous_local: str, current_base: str, requested: str) -> str:
    """Merge folder/model requests with the current human-edited Base state."""
    current = current_base or previous_local or "未筛选"
    if current == "未通过":
        return current
    if requested == "未通过":
        return "未通过" if current == "未筛选" else current
    order = {"未筛选": 0, "面试资格": 1, "面试通过": 2, "实习生": 3}
    if current in order and requested in order:
        return requested if order[requested] >= order[current] else current
    return current
