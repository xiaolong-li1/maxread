from __future__ import annotations

import json
import hashlib
import re
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .attachment_text import SUPPORTED_DOCUMENT_SUFFIXES, extract_attachment_text
from .base_sync import BaseSync, merge_base_profile
from .config import PipelineSettings
from .docs_sync import DocsSync
from .external_attachments import download_external_pdfs
from .llm import RecruitingLLM
from .models import CandidateFields, ProcessedThread, StoredMessage, ThreadEnvelope
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
    artifacts_released: int = 0

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
        self._participant_addresses = tuple(dict.fromkeys((*self._mailbox_addresses, *settings.team_addresses)))

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
            known_threads = {str(row["thread_key"]) for row in self.store.list_threads()}
            stats.new_threads = sum(1 for key in changed_keys if key not in known_threads)
            stats.updated_threads = len(changed_keys) - stats.new_threads

            completed_threads = 0
            for thread, envelope, preparation_error in self._extract_changed(envelopes, changed_keys):
                completed_threads += 1
                progress_error = ""
                if preparation_error is not None or thread is None:
                    stats.failed_threads += 1
                    error = preparation_error or RuntimeError("thread preparation failed")
                    progress_error = str(error)[:200]
                    self._record_preparation_failure(envelope, error)
                    print(json.dumps({
                        "event": "recruiting_thread_progress",
                        "completed": completed_threads,
                        "total": len(changed_keys),
                        "failed": stats.failed_threads,
                        "name": envelope.candidate_address,
                        "mail_type": "unknown",
                        "error": progress_error,
                    }, ensure_ascii=False), flush=True)
                    continue
                try:
                    result = self._sync_thread(thread)
                    stats.documents_created += int(result.get("document_created", False))
                    stats.documents_updated += int(result.get("document_updated", False))
                except Exception as exc:  # one candidate must not block the batch
                    stats.failed_threads += 1
                    progress_error = str(exc)[:200]
                    self._record_thread_failure(thread, exc)
                print(json.dumps({
                    "event": "recruiting_thread_progress",
                    "completed": completed_threads,
                    "total": len(changed_keys),
                    "failed": stats.failed_threads,
                    "name": thread.fields.name,
                    "mail_type": thread.fields.mail_type,
                    "error": progress_error,
                }, ensure_ascii=False), flush=True)
            self.store.mark_duplicate_messages_processed()
            if self.settings.clean_processed_artifacts:
                stats.artifacts_released = self.store.release_processed_artifacts()
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
        message_updates: list[tuple[int, str, str, str]] = []
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
                thread_key(message, headers, self._participant_addresses),
            )
            # Preserve the canonical Message-ID mapping so a reply from a
            # group member's personal address is joined to the candidate's
            # thread instead of becoming a second candidate.
            if headers.message_id:
                message_id_to_key[headers.message_id] = key
            direction = "outgoing" if headers.sender != candidate_address(headers, self._participant_addresses, message.body_text) else "incoming"
            message_updates.append((message.id, key, direction, message.mailbox))
            grouped.setdefault(key, []).append((message, headers))
            old = processing.get(message.id)
            if old is None or not old[1] or old[0] != key:
                changed.add(key)
        self.store.upsert_messages(message_updates)
        thread_rows = {str(row["thread_key"]): row for row in self.store.list_threads()}
        # A backfill Base write can fail after durable mail/document state is
        # complete. The explicit pending marker retries only that repair;
        # unrelated historical rows with an intentionally absent mapping stay
        # dormant until a genuinely new message changes their thread.
        for key in grouped:
            row = thread_rows.get(key)
            if (
                row is not None
                and not str(row["base_record_id"] or "")
                and str(row["status"] or "") == "base_backfill_pending"
            ):
                changed.add(key)
        return {key: build_envelope(items, self._participant_addresses, key=key) for key, items in grouped.items()}, changed

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
        attachment_texts = self._attachment_texts(envelope)
        if self._is_obvious_other(envelope):
            fields = _other_fields(envelope)
        elif self.llm:
            fields = self.llm.extract(envelope, attachment_texts, previous=previous_fields)
            if fields.mail_type == "other":
                fields = _other_fields(envelope, summary=fields.purpose_summary)
        elif previous_fields:
            fields = previous_fields
        else:
            fields = CandidateFields(name=envelope.candidate_address, purpose_summary=envelope.subject).normalized()
        fields = _restore_candidate_name(fields, envelope)
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
        """Re-extract academic fields with AI; never infer ranks from number patterns."""

        envelopes, _changed = self._load_threads()
        self._envelope_cache = envelopes
        changes: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        ordered = sorted(envelopes.items(), key=lambda item: item[1].latest_time or "", reverse=True)
        targets: list[tuple[str, ThreadEnvelope, Any, CandidateFields]] = []
        for key, envelope in ordered:
            if since_days is not None and not _within_days(envelope.latest_time, since_days):
                continue
            row = self.store.get_thread(key)
            fields = self.store.fields_from_row(row)
            if row is None or fields is None or fields.mail_type != "candidate":
                continue
            targets.append((key, envelope, row, fields))

        if self.llm is None:
            raise RuntimeError("AI is required for academic repair")

        def extract(target: tuple[str, ThreadEnvelope, Any, CandidateFields]) -> CandidateFields:
            _key, envelope, _row, fields = target
            attachment_texts = self._attachment_texts(envelope)
            refreshed = self.llm.extract(envelope, attachment_texts, previous=fields)
            refreshed.source_accounts = sorted({_source_account_label(account) for account in envelope.source_accounts})
            return refreshed

        completed = 0
        with ThreadPoolExecutor(max_workers=max(1, self.settings.llm_concurrency)) as executor:
            futures = {executor.submit(extract, target): target for target in targets}
            for future in as_completed(futures):
                key, envelope, row, fields = futures[future]
                completed += 1
                error_message = ""
                try:
                    refreshed = future.result()
                    before = {
                        "school": fields.school,
                        "academic_display": fields.academic_display,
                        "rank": fields.rank,
                        "rank_evidence": fields.rank_evidence,
                    }
                    after = {
                        "school": refreshed.school,
                        "academic_display": refreshed.academic_display,
                        "rank": refreshed.rank,
                        "rank_evidence": refreshed.rank_evidence,
                    }
                    if before != after:
                        changes.append({"thread_key": key, "name": fields.name, "before": before, "after": after})
                    if apply:
                        self._sync_thread(
                            ProcessedThread(
                                thread_key=key,
                                candidate_address=str(row["candidate_address"] or envelope.candidate_address),
                                latest_time=str(row["latest_time"] or _base_time(envelope.latest_time) or ""),
                                fields=refreshed,
                                folder_status=None,
                                interview_assigned=None,
                                document_id=str(row["doc_id"] or "") or None,
                                document_url=str(row["doc_url"] or "") or None,
                                changed=True,
                            )
                        )
                except Exception as exc:
                    error_message = str(exc)[:500]
                    failures.append({"thread_key": key, "name": fields.name, "error": error_message})
                print(json.dumps({
                    "event": "academic_reaudit_progress",
                    "completed": completed,
                    "total": len(targets),
                    "changed": len(changes),
                    "failed": len(failures),
                    "name": fields.name,
                    "error": error_message[:200],
                }, ensure_ascii=False), flush=True)
        return {
            "ok": not failures,
            "dry_run": not apply,
            "changed": len(changes),
            "failed": len(failures),
            "changes": changes,
            "failures": failures,
        }

    def repair_identities(self, *, apply: bool = False) -> dict[str, Any]:
        """Recover missing names and remove obvious administrative mail from candidates."""
        envelopes, _changed = self._load_threads()
        self._envelope_cache = envelopes
        changes: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        for row in self.store.list_threads():
            key = str(row["thread_key"])
            envelope = envelopes.get(key)
            fields = self.store.fields_from_row(row)
            if envelope is None or fields is None or fields.mail_type != "candidate" or not _candidate_name_is_missing(fields.name):
                continue
            before = fields.name
            if self._is_obvious_other(envelope):
                refreshed = _other_fields(envelope, summary=fields.purpose_summary)
                action = "reclassified_other"
            else:
                refreshed = _restore_candidate_name(fields, envelope)
                if _candidate_name_is_missing(refreshed.name):
                    failures.append({"thread_key": key, "name": before, "error": "no deterministic name evidence"})
                    continue
                action = "name_recovered"
            refreshed.source_accounts = sorted({_source_account_label(account) for account in envelope.source_accounts})
            changes.append({
                "thread_key": key,
                "before": before,
                "after": refreshed.name,
                "action": action,
            })
            if not apply:
                continue
            try:
                self._sync_thread(
                    ProcessedThread(
                        thread_key=key,
                        candidate_address=str(row["candidate_address"] or envelope.candidate_address),
                        latest_time=str(row["latest_time"] or _base_time(envelope.latest_time) or ""),
                        fields=refreshed,
                        folder_status=None,
                        interview_assigned=None,
                        document_id=str(row["doc_id"] or "") or None,
                        document_url=str(row["doc_url"] or "") or None,
                        changed=True,
                    ),
                    authoritative_identity=True,
                )
            except Exception as exc:
                failures.append({"thread_key": key, "name": before, "error": str(exc)[:500]})
        return {
            "ok": not failures,
            "dry_run": not apply,
            "changed": len(changes),
            "failed": len(failures),
            "changes": changes,
            "failures": failures,
        }

    def sync_provenance(self, *, apply: bool = False, since_days: int | None = None) -> dict[str, Any]:
        envelopes, _changed = self._load_threads()
        self._envelope_cache = envelopes
        changes: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for key, envelope in sorted(envelopes.items(), key=lambda item: item[1].latest_time or "", reverse=True):
            if since_days is not None and not _within_days(envelope.latest_time, since_days):
                continue
            row = self.store.get_thread(key)
            fields = self.store.fields_from_row(row)
            if row is None or fields is None:
                continue
            sources = sorted({_source_account_label(account) for account in envelope.source_accounts})
            replied = bool(envelope.outgoing)
            previous_replied = bool(str(row["last_outgoing_time"] or ""))
            if fields.source_accounts == sources and previous_replied == replied:
                continue
            changes.append({
                "thread_key": key,
                "name": fields.name,
                "before_sources": fields.source_accounts,
                "after_sources": sources,
                "before_replied": previous_replied,
                "after_replied": replied,
            })
            if not apply:
                continue
            fields.source_accounts = sources
            try:
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

    def _sync_thread(self, thread: ProcessedThread, *, authoritative_identity: bool = False) -> dict[str, bool]:
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
        resolved_fields = merge_base_profile(thread.fields, base_state)
        if authoritative_identity:
            resolved_fields.name = thread.fields.name
            resolved_fields.mail_type = thread.fields.mail_type
            resolved_fields.normalized()
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
        if self.docs and _needs_material_document(resolved_fields):
            document_messages: list[StoredMessage] = []
            if thread.document_id:
                materialized_ids = self.store.document_materialized_message_ids(thread.thread_key, thread.document_id)
                document_messages = [message for message in pending_messages if message.id not in materialized_ids]
                if document_messages:
                    markers = {message.id: self._document_message_heading(envelope, message) for message in document_messages}
                    existing_markers = self.docs.materialized_markers(thread.document_id, set(markers.values()))
                    recovered_ids = [message_id for message_id, marker in markers.items() if marker in existing_markers]
                    self.store.mark_document_messages_materialized(thread.thread_key, thread.document_id, recovered_ids)
                    document_messages = [message for message in document_messages if message.id not in recovered_ids]
                self.docs.update_title(thread.document_id, f"{resolved_fields.name}｜真实邮件材料")
                summary_changed = bool(row and (previous_fields != resolved_fields or str(row["latest_time"] or "") != str(thread.latest_time or "")))
                if summary_changed:
                    document_updated = self.docs.replace_summary(thread.document_id, resolved_fields, thread.latest_time) or document_updated
                # A reprocess must be idempotent: never append the complete
                # structured summary again.  Only genuinely new messages and
                # their newly materialized attachments are appended.
                if document_messages:
                    self.docs.append(thread.document_id, self._document_delta_content(envelope, document_messages))
                    self.store.mark_document_messages_materialized(
                        thread.thread_key,
                        thread.document_id,
                        [message.id for message in document_messages],
                    )
                    document_updated = True
                document_id, document_url = thread.document_id, thread.document_url
            else:
                content = self._document_content(envelope, resolved_fields, None)
                document_id, document_url = self.docs.create(f"{resolved_fields.name}｜真实邮件材料", content)
                self.docs.update_title(document_id, f"{resolved_fields.name}｜真实邮件材料")
                document_created = True
                # Document creation and attachment insertion are separate
                # remote operations. Persist the token as a recovery
                # checkpoint so a later media failure resumes in place.
                self.store.save_thread(
                    thread.thread_key,
                    thread.candidate_address,
                    envelope.subject,
                    resolved_fields,
                    base_record_id=base_record_id,
                    doc_id=document_id,
                    doc_url=document_url,
                    latest_time=thread.latest_time or "",
                    last_incoming_time=max((item.received_at or "" for item in envelope.incoming), default=""),
                    last_outgoing_time=max((item.received_at or "" for item in envelope.outgoing), default=""),
                    screening_status=status,
                    interview_assigned=int(assigned),
                    interview_result=previous_result,
                    status="publishing",
                    last_error="",
                )
                self.store.mark_document_messages_materialized(
                    thread.thread_key,
                    document_id,
                    [message.id for message in envelope.messages],
                )
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
            if summary_changed or document_messages or uploaded_any:
                # Some document update calls replay tokenized media blocks;
                # remove any replayed file cards before the run completes.
                self.docs.deduplicate_files(document_id)
            if attachment_messages:
                self.docs.replace_attachment_summary(document_id, self._attachment_summary_lines(envelope))
            document_updated = document_updated or uploaded_any
        else:
            document_id, document_url = thread.document_id, thread.document_url

        base_result = self.base.upsert(
            record_id=base_record_id or None,
            fields=resolved_fields,
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
            resolved_fields,
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
                thread_key(message, headers, self._participant_addresses),
            )
            if headers.message_id:
                message_id_to_key[headers.message_id] = resolved
            grouped.setdefault(resolved, []).append((message, headers))
        items = grouped.get(key, [])
        return build_envelope(items, self._participant_addresses, key=key)

    def _attachment_texts(self, envelope: ThreadEnvelope) -> dict[str, str]:
        paths = [
            path
            for path in self._attachment_paths(envelope)
            if path.suffix.casefold() in SUPPORTED_DOCUMENT_SUFFIXES
        ]
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
            pairs = list(executor.map(lambda path: (path.name, extract_attachment_text(path)), unique_paths))
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
                "学位论文答辩",
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
            f"- 排名依据：{fields.rank_evidence}",
            f"- 院校标签：985={fields.is_985}；C9={fields.is_c9}",
            f"- 来源邮箱：{'、'.join(fields.source_accounts)}",
            "- 申请目的 / 科研摘要：",
            fields.purpose_summary,
            f"- 最新邮件时间：{envelope.latest_time or 'unknown'}",
            "",
            "## 新增邮件线程片段",
        ]
        for message in messages or list(envelope.messages):
            parts.extend([
                f"### {self._document_message_heading(envelope, message)}",
                f"发件人：{message.sender_address}",
                "```text",
                message.body_text[:50000],
                "```",
                "",
            ])
        attachment_lines = self._attachment_summary_lines(envelope, messages)
        parts.extend(["## 附件", "", *attachment_lines, "", "> 本文档由 ZIP Lab 招聘邮箱只读管线生成。"])
        return "\n".join(parts)

    def _attachment_summary_lines(
        self,
        envelope: ThreadEnvelope,
        messages: list[StoredMessage] | None = None,
    ) -> list[str]:
        selected = messages or list(envelope.messages)
        paths = self._attachment_paths(envelope, selected)
        inventory = self.store.attachment_inventory(message.id for message in selected)
        lines: list[str] = []
        uploaded = self.store.uploaded_attachment_digests(envelope.key)
        indexed_paths: set[Path] = set()
        for item in inventory:
            name = str(item.get("filename") or "附件")
            digest = str(item.get("sha256") or "")
            reason = str(item.get("skipped_reason") or "")
            local_path = Path(str(item["local_path"])).resolve() if item.get("local_path") else None
            if local_path:
                indexed_paths.add(local_path)
            if digest and digest in uploaded:
                lines.append(f"- {name}（已附加到本文档）")
            elif local_path and local_path.exists():
                lines.append(f"- {name}")
            elif reason == "attachment_too_large":
                size_mb = int(item.get("size_bytes") or 0) / (1024 * 1024)
                lines.append(f"- {name}（{size_mb:.1f} MB，超过自动下载上限，尚未附加；请从原邮件查看）")
            elif reason and reason != "processed_cleanup":
                lines.append(f"- {name}（未能自动附加：{reason}）")
        for path in paths:
            if path.resolve() not in indexed_paths:
                lines.append(f"- {path.name}")
        if lines:
            return list(dict.fromkeys(lines))
        if any(token in message.body_text for message in selected for token in ("超大附件", "在线预览", "下载")):
            return ["- 邮件包含邮箱超大附件外链；IMAP 未同步文件实体，请从原邮件打开链接下载。"]
        return ["- 未发现可下载的本地附件。"]

    def _document_delta_content(self, envelope: ThreadEnvelope, messages: list[StoredMessage]) -> str:
        """Render only newly arrived thread material for an existing doc."""
        parts = ["## 新增邮件线程片段", ""]
        for message in messages:
            parts.extend([
                f"### {self._document_message_heading(envelope, message)}",
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

    @staticmethod
    def _document_message_heading(envelope: ThreadEnvelope, message: StoredMessage) -> str:
        direction = "我方回复" if message in envelope.outgoing else "候选人来信"
        return f"UID {message.source_uid}｜{direction}｜{message.received_at or 'unknown'}｜{message.subject}"


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


_MISSING_CANDIDATE_NAMES = {"", "unknown", "未知", "未提供", "none", "n/a", "-", "—"}


def _restore_candidate_name(fields: CandidateFields, envelope: ThreadEnvelope) -> CandidateFields:
    """Recover a missing model field from deterministic sender evidence."""
    if fields.mail_type != "candidate" or not _candidate_name_is_missing(fields.name):
        return fields
    candidate_address = str(envelope.candidate_address or "").strip().casefold()
    for message in reversed(envelope.incoming):
        if str(message.sender_address or "").strip().casefold() != candidate_address:
            continue
        name = _clean_candidate_name(message.sender_name)
        if name:
            fields.name = name
            return fields
    for message in reversed(envelope.incoming):
        name = _candidate_name_from_subject(message.subject)
        if name:
            fields.name = name
            return fields
    for message in reversed(envelope.incoming):
        name = _candidate_name_from_body(message.body_text)
        if name:
            fields.name = name
            return fields
    return fields


def _candidate_name_is_missing(value: str) -> bool:
    text = str(value or "").strip()
    return text.casefold() in _MISSING_CANDIDATE_NAMES or "@" in text


def _clean_candidate_name(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n\"'<>，,；;")
    if _candidate_name_is_missing(text) or text.isdigit():
        return ""
    if text.casefold() in {"zip lab", "bohan", "bohan zhuang", "招生", "老师", "同学"}:
        return ""
    if re.fullmatch(r"[\u3400-\u9fff·]{2,8}", text):
        return text
    if 2 <= len(text) <= 60 and re.fullmatch(r"[A-Za-z][A-Za-z .'-]+", text):
        return text
    return ""


def _candidate_name_from_subject(subject: str) -> str:
    text = str(subject or "").strip()
    patterns = (
        r"(?:申请|咨询|简历|cv)\s*[-—_：:/]+\s*([\u3400-\u9fff·]{2,8})(?=$|[-—_：:/])",
        r"^([\u3400-\u9fff·]{2,8})\s*[-—_]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            name = _clean_candidate_name(match.group(1))
            if name:
                return name
    return ""


def _candidate_name_from_body(body: str) -> str:
    text = str(body or "")[:4000]
    patterns = (
        r"(?:我叫|姓名\s*[：:])\s*([\u3400-\u9fff·]{2,8})",
        r"(?:本科生|硕士生|博士生|研究生|学生)\s*([\u3400-\u9fff·]{2,8})(?=[，。,.；;\s])",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = _clean_candidate_name(match.group(1))
            if name:
                return name
    return ""


def _needs_material_document(fields: CandidateFields) -> bool:
    return fields.mail_type == "candidate"


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
