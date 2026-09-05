from __future__ import annotations

import tempfile
import unittest
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mail_collector.parser import parse_message
from recruiting_pipeline.config import PipelineSettings
from recruiting_pipeline.docs_sync import DocsSync
from recruiting_pipeline.cli import build_parser
from recruiting_pipeline.base_sync import BASE_RECORD_FIELDS, BaseSync, _cell_url, merge_base_profile
from recruiting_pipeline.attachment_text import extract_attachment_text
from recruiting_pipeline.models import ProcessedThread, StoredMessage, ThreadEnvelope
from recruiting_pipeline.runner import RecruitingRunner, _merge_status, _needs_material_document, _other_fields, _restore_candidate_name, _within_days
from recruiting_pipeline.llm import _fields_from_json, _strip_json_fence
from recruiting_pipeline.institution_tags import C9, PROJECT_985, classify_institution
from recruiting_pipeline.models import CandidateFields
from recruiting_pipeline.store import PipelineStore
from recruiting_pipeline.threading import HeaderInfo, build_envelope, candidate_address, normalize_subject, read_headers, thread_key
from recruiting_pipeline.weekly_report import markdown_to_post, render_weekly_report


class RecruitingPipelineTest(unittest.TestCase):
    def test_missing_candidate_name_is_restored_from_sender_header(self) -> None:
        message = StoredMessage(
            1,
            "1",
            "INBOX",
            "zzb研究方向咨询-吴非桐",
            "吴非桐",
            "3230102212@zju.edu.cn",
            "2026-09-03T13:10:29+08:00",
            "庄老师好，我是计算机学院27届本科生吴非桐。",
            Path("/tmp/message.eml"),
        )
        envelope = ThreadEnvelope(
            "thread",
            "3230102212@zju.edu.cn",
            message.subject,
            (message,),
            (message,),
            (),
            frozenset({"INBOX"}),
        )
        fields = CandidateFields(name="unknown", mail_type="candidate").normalized()

        assert _restore_candidate_name(fields, envelope).name == "吴非桐"

    def test_deterministic_name_fallback_never_overwrites_model_name(self) -> None:
        message = StoredMessage(1, "1", "INBOX", "申请-张三", "张三", "a@example.com", None, "", Path("/tmp/message.eml"))
        envelope = ThreadEnvelope("thread", "a@example.com", message.subject, (message,), (message,), (), frozenset())
        fields = CandidateFields(name="李四", mail_type="candidate").normalized()

        assert _restore_candidate_name(fields, envelope).name == "李四"

    def test_docs_sync_accepts_cloud_attachment_outside_project_root(self) -> None:
        settings = SimpleNamespace(
            root=Path("/home/user/maxread"),
            lark_cli="lark-cli",
            feishu_as="bot",
            command_env=lambda: {},
        )
        sync = DocsSync(settings)
        calls = []
        sync._call = lambda args, cwd=None: calls.append((args, cwd)) or {}

        sync.insert_file("doc", Path("/mnt/data/user/maxread/mail/resume.pdf"))

        args, cwd = calls[0]
        file_index = args.index("--file")
        self.assertEqual(args[file_index + 1], "./resume.pdf")
        self.assertEqual(cwd, Path("/mnt/data/user/maxread/mail"))

    def test_attachment_summary_replacement_includes_section_heading(self) -> None:
        settings = SimpleNamespace(root=Path("/tmp"), lark_cli="lark-cli", feishu_as="bot")
        sync = DocsSync(settings)
        updates = []
        sync._call = lambda args, cwd=None: (
            {"data": {"document": {"content": "正文也提到 resume.pdf\n\n## 附件\n\n- resume.pdf\n\n> footer"}}}
            if "+fetch" in args
            else updates.append(args) or {"ok": True}
        )

        assert sync.replace_attachment_summary("doc", ["- resume.pdf（已附加到本文档）"]) is True

        pattern = updates[0][updates[0].index("--pattern") + 1]
        replacement = updates[0][updates[0].index("--content") + 1]
        self.assertEqual(pattern, "## 附件\n\n- resume.pdf")
        self.assertEqual(replacement, "## 附件\n\n- resume.pdf（已附加到本文档）")

    def test_file_deduplication_keeps_same_name_with_different_sizes(self) -> None:
        settings = SimpleNamespace(root=Path("/tmp"), lark_cli="lark-cli", feishu_as="bot")
        sync = DocsSync(settings)
        updates = []
        xml = (
            '<figure id="first"><source name="materials.zip" size="10"/></figure>'
            '<figure id="replayed"><source name="materials.zip" size="10"/></figure>'
            '<figure id="new-version"><source name="materials.zip" size="20"/></figure>'
        )

        def call(args, cwd=None):
            if "+fetch" in args:
                return {"data": {"document": {"content": xml}}}
            updates.append(args)
            return {"ok": True}

        sync._call = call
        with patch("recruiting_pipeline.docs_sync.time.sleep"):
            removed = sync.deduplicate_files("doc")

        self.assertEqual(removed, 1)
        self.assertIn("replayed", updates[0])
        self.assertNotIn("new-version", updates[0])

    def test_attachment_summary_distinguishes_oversized_file_from_no_attachment(self) -> None:
        runner = RecruitingRunner.__new__(RecruitingRunner)
        runner._attachment_paths = lambda *_args: []
        runner.store = SimpleNamespace(
            uploaded_attachment_digests=lambda _key: set(),
            attachment_inventory=lambda _ids: [{
                "filename": "保研材料.pdf",
                "size_bytes": 29349000,
                "sha256": "digest",
                "local_path": None,
                "skipped_reason": "attachment_too_large",
            }],
        )
        message = StoredMessage(1, "1", "INBOX", "申请", "", "a@example.com", None, "附件附上简历", Path("/tmp/1"))
        envelope = ThreadEnvelope("thread", "a@example.com", "申请", (message,), (message,), (), frozenset())

        lines = runner._attachment_summary_lines(envelope)

        self.assertEqual(lines, ["- 保研材料.pdf（28.0 MB，超过自动下载上限，尚未附加；请从原邮件查看）"])

    def test_only_candidate_mail_materializes_full_document(self) -> None:
        self.assertTrue(_needs_material_document(CandidateFields(mail_type="candidate").normalized()))
        self.assertFalse(_needs_material_document(CandidateFields(mail_type="other").normalized()))

    def test_empty_original_message_body_is_explained_in_document(self) -> None:
        message = StoredMessage(1, "1", "INBOX", "申请", "候选人", "a@example.com", None, "", Path("/tmp/1"))

        self.assertEqual(
            RecruitingRunner._message_body_for_document(message),
            "（原始邮件正文为空；如有材料，请查看文末附件。）",
        )

    def test_read_headers_does_not_load_attachment_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large.eml"
            path.write_bytes(
                b"From: candidate@example.com\r\nTo: lab@example.com\r\nMessage-ID: <m@example.com>\r\n\r\n"
                + b"x" * (2 * 1024 * 1024)
            )
            with patch.object(Path, "read_bytes", side_effect=AssertionError("full EML read")):
                headers = read_headers(path)

        self.assertEqual(headers.message_id, "<m@example.com>")

    def test_processed_message_releases_payload_but_keeps_thread_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "mail.sqlite3"
            message_dir = root / "messages/INBOX/1"
            message_dir.mkdir(parents=True)
            raw = message_dir / "message.eml"
            raw.write_bytes(
                b"From: Candidate <candidate@example.com>\r\n"
                b"To: lab@example.com\r\n"
                b"Message-ID: <m@example.com>\r\n"
                b"References: <root@example.com>\r\n\r\n"
                + b"large body" * 1000
            )
            body = message_dir / "body.txt"
            body.write_text("large body", encoding="utf-8")
            attachment = message_dir / "01-resume.pdf"
            attachment.write_bytes(b"pdf bytes")
            external = message_dir / "external-attachments/external.pdf"
            external.parent.mkdir()
            external.write_bytes(b"external bytes")
            with sqlite3.connect(db) as conn:
                conn.execute("create table messages(id integer primary key,raw_path text,artifacts_released_at text)")
                conn.execute("create table attachments(message_record_id integer,local_path text,skipped_reason text)")
                conn.execute("insert into messages values(1,?,null)", (str(raw),))
                conn.execute("insert into attachments values(1,?,null)", (str(attachment),))
            store = PipelineStore(db)
            store.initialize()
            store.upsert_message(1, "thread", "incoming", "INBOX")
            store.mark_message_processed(1)

            self.assertEqual(store.release_processed_artifacts(), 1)

            self.assertLess(raw.stat().st_size, 300)
            self.assertEqual(read_headers(raw).message_id, "<m@example.com>")
            self.assertFalse(body.exists())
            self.assertFalse(attachment.exists())
            self.assertFalse(external.exists())
            with sqlite3.connect(db) as conn:
                self.assertIsNotNone(conn.execute("select artifacts_released_at from messages where id=1").fetchone()[0])
                self.assertIsNone(conn.execute("select local_path from attachments where message_record_id=1").fetchone()[0])

    def test_duplicate_message_id_in_second_mailbox_inherits_processed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "mail.sqlite3"
            with sqlite3.connect(db) as conn:
                conn.execute("create table messages(id integer primary key,message_id text,raw_path text,artifacts_released_at text)")
                conn.execute("create table attachments(message_record_id integer,local_path text,skipped_reason text)")
                conn.executemany(
                    "insert into messages values(?,?,?,null)",
                    [(1, "<same@example.com>", "/tmp/1.eml"), (2, "<same@example.com>", "/tmp/2.eml")],
                )
            store = PipelineStore(db)
            store.initialize()
            store.upsert_message(1, "thread", "incoming", "INBOX")
            store.upsert_message(2, "thread", "incoming", "Archive")
            store.mark_message_processed(1)

            self.assertEqual(store.mark_duplicate_messages_processed(), 1)
            with sqlite3.connect(db) as conn:
                self.assertIsNotNone(
                    conn.execute("select processed_at from recruiting_messages where message_record_id=2").fetchone()[0]
                )

    def test_thread_latest_time_handles_mixed_timezone_dates(self) -> None:
        first = StoredMessage(1, "1", "INBOX", "s", "", "a@example.com", "2026-08-29T10:00:00", "", Path("/tmp/1"))
        second = StoredMessage(2, "2", "INBOX", "s", "", "a@example.com", "2026-08-29T19:00:00+08:00", "", Path("/tmp/2"))
        envelope = ThreadEnvelope("k", "a@example.com", "s", (first, second), (first, second), (), frozenset({"INBOX"}))

        self.assertEqual(envelope.latest_time, second.received_at)

    def test_docx_resume_text_reaches_evidence_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "resume.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>GPA 3.905/4.0</w:t></w:r></w:p>'
                    '<w:p><w:r><w:t>专业排名 26/556</w:t></w:r></w:p></w:body></w:document>',
                )

            text = extract_attachment_text(path)

        self.assertIn("GPA 3.905/4.0", text)
        self.assertIn("专业排名 26/556", text)

    def test_pipeline_settings_load_multiple_mail_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = root / "primary.env"
            secondary = root / "secondary.env"
            secondary.write_text("IMAP_USERNAME=bohan.zhuang@zju.edu.cn\n", encoding="utf-8")
            primary.write_text(
                "IMAP_USERNAME=zip.lab@zju.edu.cn\n"
                "MAIL_READ_ONLY=1\n"
                "RECRUITING_BASE_TOKEN=base\n"
                "RECRUITING_TABLE_ID=table\n"
                "RECRUITING_TEAM_ADDRESSES=erix025@outlook.com,wangweijie@zju.edu.cn\n"
                f"RECRUITING_MAIL_ACCOUNT_ENVS={secondary}\n",
                encoding="utf-8",
            )

            settings = PipelineSettings.load(root, primary)

            self.assertEqual(settings.mailbox_env_files, (primary, secondary))
            self.assertEqual(settings.mailbox_addresses, ("zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn"))
            self.assertEqual(settings.team_addresses, ("erix025@outlook.com", "wangweijie@zju.edu.cn"))
            self.assertEqual(settings.model, "gpt-5.6-luna")

    def test_official_985_and_c9_lists_have_expected_membership(self) -> None:
        self.assertEqual(len(PROJECT_985), 39)
        self.assertEqual(len(C9), 9)
        self.assertIn("电子科技大学", PROJECT_985)
        self.assertIn("浙江大学", C9)

    def test_institution_aliases_are_tagged_without_substring_false_positive(self) -> None:
        self.assertEqual(classify_institution("浙大").is_c9, "是")
        self.assertEqual(classify_institution("电子科技大学").is_985, "是")
        self.assertEqual(classify_institution("电子科技大学").is_c9, "否")
        self.assertEqual(classify_institution("浙江大学城市学院").is_985, "否")
        self.assertEqual(classify_institution("unknown").is_985, "未知")
        self.assertEqual(classify_institution("—", applicable=False).is_985, "不适用")

    def test_ai_rank_fields_are_preserved_without_numeric_reinterpretation(self) -> None:
        fields = _fields_from_json({
            "name": "张佳怡",
            "mail_type": "candidate",
            "academic_display": "均分 94.73/100",
            "rank": "Top 3%",
            "rank_evidence": "平均学分绩排名：94.73/100（专业前3%）",
        }, None)

        self.assertEqual(fields.academic_display, "均分 94.73/100")
        self.assertEqual(fields.rank, "Top 3%")
        self.assertNotIn("84", fields.rank)

    def test_ai_can_explicitly_report_rank_missing(self) -> None:
        fields = _fields_from_json({
            "name": "李奕博",
            "mail_type": "candidate",
            "academic_display": "均分 93.38/100 · GPA 4.02/4.3",
            "rank": "未提供",
            "rank_evidence": "未提供",
        }, None)

        self.assertEqual(fields.rank, "未提供")

    def test_subject_normalization_merges_replies(self) -> None:
        self.assertEqual(normalize_subject("Re: 回复： 实习生-浙大-大二"), "实习生-浙大-大二")

    def test_unknown_project_is_preserved(self) -> None:
        fields = _fields_from_json({"name": "A", "mail_type": "internship_application", "projects": ["RL", "unknown"]}, None)
        self.assertEqual(fields.projects, ["MLSys"])
        self.assertEqual(fields.mail_type, "candidate")

    def test_json_fence_is_removed(self) -> None:
        self.assertEqual(_strip_json_fence("```json\n{\"name\":\"A\"}\n```"), '{"name":"A"}')

    def test_pipeline_store_migrates_and_saves_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mail.sqlite3"
            store = PipelineStore(path)
            store.initialize()
            with store.connect() as conn:
                conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY)")
                conn.execute("INSERT INTO messages(id) VALUES (1)")
            fields = CandidateFields(name="A", school="浙大", mail_type="internship_application").normalized()
            self.assertEqual(fields.is_985, "是")
            self.assertEqual(fields.is_c9, "是")
            store.save_thread("key", "a@example.com", "subject", fields, screening_status="面试资格", interview_assigned=1)
            row = store.get_thread("key")
            self.assertEqual(row["screening_status"], "面试资格")
            self.assertEqual(row["interview_assigned"], 1)

    def test_pipeline_store_batches_message_thread_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mail.sqlite3"
            store = PipelineStore(path)
            store.initialize()
            with store.connect() as conn:
                conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY)")
                conn.executemany("INSERT INTO messages(id) VALUES (?)", [(1,), (2,)])

            store.upsert_messages([(1, "a", "incoming", "INBOX"), (2, "b", "outgoing", "Sent")])

            self.assertEqual(store.message_thread_keys(), {1: "a", 2: "b"})
            store.upsert_message(1, "key-a", "incoming", "INBOX")
            store.mark_message_processed(1)
            store.upsert_message(1, "key-b", "outgoing", "INBOX")
            self.assertEqual(store.message_processing_state()[1], ("key-b", False))
            store.mark_attachment_uploaded("key", "sha", "resume.pdf", "doc")
            self.assertEqual(store.uploaded_attachment_digests("key"), {"sha"})
            store.mark_document_messages_materialized("key", "doc", [1, 2, 2])
            self.assertEqual(store.document_materialized_message_ids("key", "doc"), {1, 2})
            self.assertEqual(store.document_materialized_message_ids("key", "other-doc"), set())

    def test_document_token_is_checkpointed_before_media_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            attachment = Path(temp) / "resume.pdf"
            attachment.write_bytes(b"%PDF-1.4\n")
            message = StoredMessage(
                1,
                "1",
                "INBOX",
                "申请",
                "候选人",
                "candidate@example.com",
                "2026-09-02T10:00:00+08:00",
                "申请材料",
                Path(temp) / "message.eml",
                attachments=(attachment,),
            )
            envelope = ThreadEnvelope(
                "thread",
                "candidate@example.com",
                "申请",
                (message,),
                (message,),
                (),
                frozenset({"INBOX"}),
                frozenset({"zip.lab@zju.edu.cn"}),
            )
            fields = CandidateFields(
                name="候选人",
                school="浙江大学",
                mail_type="candidate",
                projects=["MLSys"],
                source_accounts=["ZIP Lab"],
            ).normalized()
            saved = []
            materialized = []
            runner = RecruitingRunner.__new__(RecruitingRunner)
            runner._envelope_cache = {"thread": envelope}
            runner._download_external = lambda _envelope: None
            runner._attachment_paths = lambda _envelope, _messages: [attachment]
            runner._document_content = lambda *_args: "content"
            runner.dry_run = False
            runner.store = SimpleNamespace(
                get_thread=lambda _key: None,
                fields_from_row=lambda _row: None,
                message_processing_state=lambda: {},
                document_materialized_message_ids=lambda _key, _doc: set(),
                mark_document_messages_materialized=lambda key, doc, ids: materialized.append((key, doc, list(ids))),
                uploaded_attachment_digests=lambda _key: set(),
                save_thread=lambda *args, **kwargs: saved.append(kwargs),
            )
            runner.base = SimpleNamespace(
                find_existing=lambda *_args: None,
                current_state=lambda _record_id: None,
            )

            def fail_media(_document_id, _path):
                raise RuntimeError("media failed")

            runner.docs = SimpleNamespace(
                create=lambda *_args: ("doc-token", "https://example/doc-token"),
                update_title=lambda *_args: None,
                insert_file=fail_media,
            )
            thread = ProcessedThread(
                "thread",
                "candidate@example.com",
                "2026-09-02 10:00",
                fields,
                None,
                None,
                None,
                None,
                True,
            )

            with self.assertRaisesRegex(RuntimeError, "media failed"):
                runner._sync_thread(thread)

            self.assertEqual(saved[0]["doc_id"], "doc-token")
            self.assertEqual(saved[0]["status"], "publishing")
            self.assertEqual(materialized, [("thread", "doc-token", [1])])

    def test_existing_remote_message_marker_prevents_duplicate_append(self) -> None:
        message = StoredMessage(
            1,
            "42",
            "INBOX",
            "申请",
            "候选人",
            "candidate@example.com",
            "2026-09-02T10:00:00+08:00",
            "申请材料",
            Path("/tmp/message.eml"),
        )
        envelope = ThreadEnvelope(
            "thread",
            "candidate@example.com",
            "申请",
            (message,),
            (message,),
            (),
            frozenset({"INBOX"}),
            frozenset({"zip.lab@zju.edu.cn"}),
        )
        fields = CandidateFields(
            name="候选人",
            school="浙江大学",
            mail_type="candidate",
            projects=["MLSys"],
            source_accounts=["ZIP Lab"],
        ).normalized()
        row = {
            "screening_status": "未筛选",
            "interview_result": "未开始",
            "interview_assigned": 0,
            "base_record_id": "rec",
            "latest_time": "2026-09-02 10:00",
        }
        materialized = []
        appended = []
        runner = RecruitingRunner.__new__(RecruitingRunner)
        runner._envelope_cache = {"thread": envelope}
        runner._download_external = lambda _envelope: None
        runner._attachment_paths = lambda *_args: []
        runner.dry_run = False
        runner.store = SimpleNamespace(
            get_thread=lambda _key: row,
            fields_from_row=lambda _row: fields,
            message_processing_state=lambda: {},
            document_materialized_message_ids=lambda _key, _doc: set(),
            mark_document_messages_materialized=lambda key, doc, ids: materialized.append((key, doc, list(ids))),
            uploaded_attachment_digests=lambda _key: set(),
            attachment_inventory=lambda _ids: [],
            save_thread=lambda *_args, **_kwargs: None,
            mark_message_processed=lambda _id: None,
        )
        runner.base = SimpleNamespace(
            find_existing=lambda *_args: "rec",
            current_state=lambda _id: None,
            upsert=lambda **_kwargs: SimpleNamespace(record_id="rec"),
        )
        marker = runner._document_message_heading(envelope, message)
        runner.docs = SimpleNamespace(
            materialized_markers=lambda _doc, _markers: {marker},
            update_title=lambda *_args: None,
            replace_summary=lambda *_args: False,
            append=lambda *_args: appended.append(True),
            deduplicate_files=lambda *_args: 0,
            replace_attachment_summary=lambda *_args: False,
        )
        thread = ProcessedThread(
            "thread",
            "candidate@example.com",
            "2026-09-02 10:00",
            fields,
            None,
            None,
            "doc-token",
            "https://example/doc-token",
            True,
        )

        runner._sync_thread(thread)

        self.assertEqual(appended, [])
        self.assertEqual(materialized, [("thread", "doc-token", [1])])

    def test_base_backfill_reset_is_windowed_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mail.sqlite3"
            store = PipelineStore(path)
            store.initialize()
            with store.connect() as conn:
                conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY)")
                conn.executemany("INSERT INTO messages(id) VALUES (?)", [(1,), (2,), (3,)])
            candidate = CandidateFields(name="Recent", mail_type="candidate").normalized()
            other = CandidateFields(name="Other", mail_type="other").normalized()
            store.save_thread("recent", "a@example.com", "s", candidate, base_record_id="rec-recent", latest_time="2026-08-20 09:00")
            store.save_thread("old", "b@example.com", "s", candidate, base_record_id="rec-old", latest_time="2026-06-01 09:00")
            store.save_thread("other", "c@example.com", "s", other, base_record_id="rec-other", latest_time="2026-08-22 09:00")
            for message_id, key in ((1, "recent"), (2, "old"), (3, "other")):
                store.upsert_message(message_id, key, "incoming", "INBOX")
                store.mark_message_processed(message_id)
            count = store.reset_base_links(30, now=datetime(2026, 8, 28, tzinfo=timezone.utc))
            self.assertEqual(count, 1)
            self.assertIsNone(store.get_thread("recent")["base_record_id"])
            self.assertEqual(store.get_thread("recent")["status"], "base_backfill_pending")
            self.assertEqual(store.get_thread("old")["base_record_id"], "rec-old")
            self.assertEqual(store.get_thread("other")["base_record_id"], "rec-other")
            self.assertEqual(store.message_processing_state()[1], ("recent", True))
            self.assertEqual(store.message_processing_state()[2], ("old", True))

    def test_migrated_absolute_paths_relocate_to_current_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            message_dir = data_dir / "messages" / "42"
            message_dir.mkdir(parents=True)
            raw = message_dir / "message.eml"
            attachment = message_dir / "01-resume.pdf"
            raw.write_text("From: candidate@example.com\n\nhello", encoding="utf-8")
            attachment.write_bytes(b"%PDF-1.4\n")
            store = PipelineStore(data_dir / "mail.sqlite3")
            store.initialize()
            with store.connect() as conn:
                conn.execute(
                    "CREATE TABLE messages(id INTEGER PRIMARY KEY,source_uid TEXT,mailbox TEXT,subject TEXT,sender_name TEXT,sender_address TEXT,received_at TEXT,body_text TEXT,raw_path TEXT)"
                )
                conn.execute(
                    "CREATE TABLE attachments(id INTEGER PRIMARY KEY,message_record_id INTEGER,local_path TEXT)"
                )
                conn.execute(
                    "INSERT INTO messages VALUES (1,'42','INBOX','s','','candidate@example.com','2026-08-20T09:00:00+08:00','body','/old/host/messages/42/message.eml')"
                )
                conn.execute(
                    "INSERT INTO attachments VALUES (1,1,'/old/host/messages/42/01-resume.pdf')"
                )
            message = store.messages()[0]
            self.assertEqual(message.raw_path, raw)
            self.assertEqual(message.attachments, (attachment,))

    def test_backfill_cli_requires_explicit_confirmation(self) -> None:
        args = build_parser().parse_args(["backfill-base", "--days", "30", "--confirm"])
        self.assertEqual(args.command, "backfill-base")
        self.assertTrue(args.confirm)
        self.assertFalse(args.refresh_ai)
        self.assertFalse(args.refresh_docs)

    def test_academic_repair_cli_requires_explicit_mode(self) -> None:
        preview = build_parser().parse_args(["repair-academics", "--dry-run"])
        apply = build_parser().parse_args(["repair-academics", "--confirm", "--days", "30"])

        self.assertTrue(preview.dry_run)
        self.assertTrue(apply.confirm)
        self.assertEqual(apply.days, 30)

    def test_identity_repair_cli_requires_explicit_mode(self) -> None:
        preview = build_parser().parse_args(["repair-identities", "--dry-run"])
        apply = build_parser().parse_args(["repair-identities", "--confirm"])

        self.assertTrue(preview.dry_run)
        self.assertTrue(apply.confirm)

    def test_tag_records_cli_requires_explicit_mode(self) -> None:
        preview = build_parser().parse_args(["tag-records", "--dry-run"])
        apply = build_parser().parse_args(["tag-records", "--confirm", "--days", "30"])

        self.assertTrue(preview.dry_run)
        self.assertTrue(apply.confirm)
        self.assertEqual(apply.days, 30)

    def test_sync_provenance_cli_requires_explicit_mode(self) -> None:
        preview = build_parser().parse_args(["sync-provenance", "--dry-run", "--days", "183"])
        apply = build_parser().parse_args(["sync-provenance", "--confirm"])

        self.assertTrue(preview.dry_run)
        self.assertTrue(apply.confirm)
        self.assertEqual(preview.days, 183)

    def test_base_payload_contains_school_rank_and_reply_tags(self) -> None:
        sync = BaseSync.__new__(BaseSync)
        sync.find_existing = lambda *_args, **_kwargs: None
        captured = {}

        def call(command, body):
            captured["command"] = command
            captured["body"] = body
            return {"data": {"record_id_list": ["rec-1"]}}

        sync._call = call
        fields = CandidateFields(
            name="A",
            school="浙江大学",
            mail_type="candidate",
            academic_display="GPA 3.9/4.0",
            rank="第 4 / 120 · Top 3.33%",
            rank_evidence="专业排名第 4/120（Top 3.33%）",
            source_accounts=["ZIP Lab", "Bohan"],
        ).normalized()

        sync.upsert(
            record_id=None,
            fields=fields,
            latest_time="2026-08-29 10:00",
            document_url="https://example/doc",
            has_replied=True,
        )

        payload = captured["body"]["create_records"][0]
        self.assertEqual(payload["院校"], "浙江大学")
        self.assertEqual(payload["排名"], "第 4 / 120 · Top 3.33%")
        self.assertEqual(payload["排名依据"], "专业排名第 4/120（Top 3.33%）")
        self.assertEqual(payload["是否985"], ["是"])
        self.assertEqual(payload["是否C9"], ["是"])
        self.assertTrue(payload["是否已回复"])
        self.assertEqual(payload["来源邮箱"], ["ZIP Lab", "Bohan"])

    def test_thread_window_filter(self) -> None:
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        self.assertTrue(_within_days((now - timedelta(days=6)).isoformat(), 7, now))
        self.assertFalse(_within_days((now - timedelta(days=8)).isoformat(), 7, now))

    def test_specialized_repairs_reuse_loaded_envelopes(self) -> None:
        runner = RecruitingRunner.__new__(RecruitingRunner)
        envelope = ThreadEnvelope("key", "a@example.com", "subject", (), (), (), frozenset())
        runner._envelope_cache = {}
        runner._load_threads = lambda: ({"key": envelope}, set())
        runner.store = SimpleNamespace(get_thread=lambda _key: None, fields_from_row=lambda _row: None)
        runner.llm = None

        result = runner.sync_provenance(apply=False)

        self.assertTrue(result["ok"])
        self.assertIs(runner._envelope_cache["key"], envelope)

    def test_message_parser_stays_read_only(self) -> None:
        message = EmailMessage()
        message["Subject"] = "实习生申请"
        message["From"] = "candidate@example.com"
        message["To"] = "zip.lab@example.com"
        message["Date"] = "Mon, 24 Aug 2026 10:00:00 +0800"
        message.set_content("简历和成绩排名见附件")
        parsed = parse_message(message.as_bytes())
        self.assertTrue(parsed.likely_candidate)

    def test_base_sync_does_not_merge_ambiguous_same_name_minute(self) -> None:
        sync = BaseSync.__new__(BaseSync)
        sync._existing_index = ({("—", "2026-08-26 13:18"): ["rec-a", "rec-b"]}, {"https://example/doc-b": "rec-b"})
        self.assertIsNone(sync.find_existing("—", "2026-08-26 13:18", None))
        self.assertEqual(sync.find_existing("—", "2026-08-26 13:18", "https://example/doc-b"), "rec-b")

    def test_base_sync_reads_every_page_and_exposes_profile_state(self) -> None:
        sync = BaseSync.__new__(BaseSync)
        sync._existing_index = None
        sync._record_states = {}
        offsets = []

        def page(offset):
            offsets.append(offset)
            count = 200 if offset == 0 else 1
            rows = []
            ids = []
            for index in range(count):
                number = offset + index
                values = {field: "" for field in BASE_RECORD_FIELDS}
                values.update({
                    "姓名": f"候选人{number}",
                    "最新邮件时间": "2026-09-02T10:00:00+08:00",
                    "院校": "浙江大学",
                    "邮件类型": ["候选人来信"],
                    "申请项目": ["MLSys"],
                    "筛选状态": ["未筛选"],
                })
                rows.append([values[field] for field in BASE_RECORD_FIELDS])
                ids.append(f"rec-{number}")
            return {"record_id_list": ids, "fields": list(BASE_RECORD_FIELDS), "data": rows}

        sync._list_page = page
        states = sync.all_states(refresh=True)

        self.assertEqual(offsets, [0, 200])
        self.assertEqual(len(states), 201)
        self.assertEqual(states["rec-200"]["school"], "浙江大学")
        self.assertEqual(states["rec-200"]["projects"], ["MLSys"])

    def test_non_empty_base_profile_wins_over_mail_extraction(self) -> None:
        extracted = CandidateFields(
            name="旧姓名",
            school="旧学校",
            major="旧专业",
            projects=["MLSys"],
            mail_type="candidate",
            source_accounts=["ZIP Lab"],
        ).normalized()

        merged = merge_base_profile(extracted, {
            "name": "人工姓名",
            "school": "同济大学",
            "major": "计算机科学与技术",
            "projects": ["World Model"],
            "academic_display": "GPA 4.6/5.0",
            "rank": "第 5/191",
            "rank_evidence": "综合成绩第5/191名",
            "purpose_summary": "人工校正摘要",
            "mail_type": "candidate",
        })

        self.assertEqual(merged.name, "人工姓名")
        self.assertEqual(merged.school, "同济大学")
        self.assertEqual(merged.projects, ["World Model"])
        self.assertEqual(merged.source_accounts, ["ZIP Lab"])

    def test_base_markdown_link_keeps_the_complete_material_url(self) -> None:
        url = "https://ccnsbbr30xgq.feishu.cn/docx/ExampleToken"
        self.assertEqual(_cell_url(f"[材料]({url})"), url)
        self.assertEqual(_cell_url(f"[{url}]({url})"), url)

    def test_base_snapshot_refreshes_sqlite_from_authoritative_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "mail.sqlite3"
            store = PipelineStore(db)
            store.initialize()
            fields = CandidateFields(
                name="旧姓名",
                school="旧学校",
                mail_type="candidate",
                projects=["MLSys"],
                source_accounts=["ZIP Lab"],
            ).normalized()
            store.save_thread(
                "thread-a",
                "a@example.com",
                "申请",
                fields,
                base_record_id="rec-a",
                latest_time="2026-09-02 10:00",
                screening_status="未筛选",
                is_interested=1,
            )
            with store.connect() as connection:
                connection.execute("update recruiting_threads set updated_at='2026-09-01T00:00:00+00:00'")
            state = {
                "rec-a": {
                    "name": "人工姓名",
                    "school": "浙江大学",
                    "mail_type": "candidate",
                    "projects": ["World Model"],
                    "screening_status": "面试资格",
                    "interview_assigned": True,
                    "interview_result": "未开始",
                    "document_url": "https://example/doc",
                }
            }

            result = store.apply_base_snapshot(state, snapshot_started_at="2026-09-02T00:00:00+00:00")
            row = store.get_thread("thread-a")

            self.assertEqual(result["updated"], 1)
            self.assertEqual(store.fields_from_row(row).name, "人工姓名")
            self.assertEqual(row["screening_status"], "面试资格")
            self.assertEqual(row["is_interested"], 1)
            self.assertEqual(row["doc_url"], "https://example/doc")

    def test_base_snapshot_does_not_overwrite_pending_or_newer_local_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "mail.sqlite3"
            store = PipelineStore(db)
            store.initialize()
            fields = CandidateFields(
                name="本地姓名",
                school="本地学校",
                mail_type="candidate",
                projects=["MLSys"],
                source_accounts=["ZIP Lab"],
            ).normalized()
            for thread_key, record_id in (("pending", "rec-pending"), ("newer", "rec-newer")):
                store.save_thread(
                    thread_key,
                    f"{thread_key}@example.com",
                    "申请",
                    fields,
                    base_record_id=record_id,
                    latest_time="2026-09-02 10:00",
                    screening_status="面试资格",
                )
            with store.connect() as connection:
                connection.execute(
                    """
                    create table recruiting_admin_actions(
                        id integer primary key,thread_key text,status text
                    )
                    """
                )
                connection.execute(
                    "insert into recruiting_admin_actions values(1,'pending','pending')"
                )
                connection.execute(
                    "update recruiting_threads set updated_at='2026-09-02T01:01:00+00:00' where thread_key='newer'"
                )
            states = {
                record_id: {
                    "name": "远端旧姓名",
                    "school": "远端旧学校",
                    "mail_type": "candidate",
                    "projects": ["World Model"],
                    "screening_status": "未筛选",
                    "interview_assigned": False,
                    "interview_result": "未开始",
                }
                for record_id in ("rec-pending", "rec-newer")
            }

            result = store.apply_base_snapshot(
                states,
                snapshot_started_at="2026-09-02T01:00:00+00:00",
            )

            self.assertEqual(result["skipped_pending"], 1)
            self.assertEqual(result["skipped_newer_local"], 1)
            self.assertEqual(store.fields_from_row(store.get_thread("pending")).name, "本地姓名")
            self.assertEqual(store.fields_from_row(store.get_thread("newer")).name, "本地姓名")

    def test_candidate_mail_types_collapse_to_one_class(self) -> None:
        for value in ("internship_application", "general_inquiry", "候选人来信"):
            fields = _fields_from_json({"name": "A", "mail_type": value, "projects": ["MLSys"]}, None)
            self.assertEqual(fields.mail_type, "candidate")

    def test_model_rejection_signal_is_normalized(self) -> None:
        fields = _fields_from_json({"name": "A", "mail_type": "candidate", "rejection_recommendation": "未通过", "projects": ["MLSys"]}, None)
        self.assertEqual(fields.rejection_recommendation, "未通过")

    def test_promotional_mail_stays_in_other_category(self) -> None:
        message = StoredMessage(
            1,
            "1",
            "INBOX",
            "Get quickstart guides for popular Azure services",
            "",
            "Azure@promomail.microsoft.com",
            "2026-08-27T13:19:47-07:00",
            "Start using your free Azure credit.",
            Path("/tmp/1.eml"),
        )
        envelope = ThreadEnvelope("k", message.sender_address, message.subject, (message,), (message,), (), frozenset({"INBOX"}))
        self.assertTrue(RecruitingRunner._is_obvious_other(envelope))
        fields = _other_fields(envelope)
        self.assertEqual(fields.mail_type, "other")
        self.assertEqual(fields.name, message.subject)
        self.assertEqual(fields.academic_display, "—")

    def test_academic_defense_notice_stays_out_of_candidate_list(self) -> None:
        message = StoredMessage(
            1,
            "1",
            "INBOX",
            "26年夏季大数据技术与工程项目学位论文答辩_第一组",
            "Chen, Wenzhou",
            "wenzhouchen@intl.zju.edu.cn",
            "2026-06-01T13:00:00+08:00",
            "各位老师好，答辩材料已随附件发送，请查阅。",
            Path("/tmp/1.eml"),
        )
        envelope = ThreadEnvelope("k", message.sender_address, message.subject, (message,), (message,), (), frozenset({"INBOX"}))

        self.assertTrue(RecruitingRunner._is_obvious_other(envelope))

    def test_personal_group_reply_stays_in_candidate_thread(self) -> None:
        candidate = StoredMessage(1, "1", "INBOX", "咨询", "", "candidate@example.com", "2026-08-01T10:00:00+08:00", "简历", Path("/tmp/1.eml"))
        reply = StoredMessage(2, "2", "INBOX", "Re: 咨询", "", "bohan@example.com", "2026-08-01T11:00:00+08:00", "CV 很好", Path("/tmp/2.eml"))
        headers = [
            (candidate, HeaderInfo("<m1>", "", (), "咨询", "candidate@example.com", ("zip.lab@example.com",))),
            (reply, HeaderInfo("<m2>", "<m1>", ("<m1>",), "Re: 咨询", "bohan@example.com", ("candidate@example.com", "zip.lab@example.com"))),
        ]
        envelope = build_envelope(headers, "zip.lab@example.com", key="k")
        self.assertEqual([item.sender_address for item in envelope.incoming], ["candidate@example.com"])
        self.assertEqual([item.sender_address for item in envelope.outgoing], ["bohan@example.com"])

    def test_cc_duplicate_across_accounts_is_deduplicated_with_both_sources(self) -> None:
        first = StoredMessage(1, "42", "INBOX", "申请", "", "candidate@example.com", "2026-08-01T10:00:00+08:00", "申请", Path("/tmp/1.eml"), source_account="zip.lab@zju.edu.cn")
        second = StoredMessage(2, "99", "INBOX", "申请", "", "candidate@example.com", "2026-08-01T10:00:00+08:00", "申请", Path("/tmp/2.eml"), source_account="bohan.zhuang@zju.edu.cn")
        headers = HeaderInfo("<same>", "", (), "申请", "candidate@example.com", ("zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn"))

        envelope = build_envelope(
            [(first, headers), (second, headers)],
            ("zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn"),
            key="same-thread",
        )

        self.assertEqual(len(envelope.messages), 1)
        self.assertEqual(envelope.source_accounts, frozenset({"zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn"}))

    def test_reply_from_either_owned_mailbox_is_outgoing(self) -> None:
        candidate = StoredMessage(1, "1", "INBOX", "咨询", "", "candidate@example.com", "2026-08-01T10:00:00+08:00", "申请", Path("/tmp/1.eml"), source_account="zip.lab@zju.edu.cn")
        reply = StoredMessage(2, "2", "Sent", "Re: 咨询", "", "bohan.zhuang@zju.edu.cn", "2026-08-01T11:00:00+08:00", "欢迎交流", Path("/tmp/2.eml"), source_account="bohan.zhuang@zju.edu.cn")
        envelope = build_envelope(
            [
                (candidate, HeaderInfo("<m1>", "", (), "咨询", "candidate@example.com", ("zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn"))),
                (reply, HeaderInfo("<m2>", "<m1>", ("<m1>",), "Re: 咨询", "bohan.zhuang@zju.edu.cn", ("candidate@example.com",))),
            ],
            ("zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn"),
            key="reply-thread",
        )

        self.assertEqual([item.sender_address for item in envelope.incoming], ["candidate@example.com"])
        self.assertEqual([item.sender_address for item in envelope.outgoing], ["bohan.zhuang@zju.edu.cn"])
        self.assertEqual(envelope.source_accounts, frozenset({"zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn"}))

    def test_cced_team_member_reply_is_outgoing_without_references(self) -> None:
        candidate = StoredMessage(1, "1", "INBOX", "申请", "", "candidate@example.com", "2026-08-01T10:00:00+08:00", "申请", Path("/tmp/1.eml"))
        reply = StoredMessage(2, "2", "INBOX", "Re: 申请", "", "wangweijie@zju.edu.cn", "2026-08-01T11:00:00+08:00", "欢迎交流", Path("/tmp/2.eml"))
        participants = ("zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn", "wangweijie@zju.edu.cn")
        candidate_headers = HeaderInfo("<m1>", "", (), "申请", "candidate@example.com", ("zip.lab@zju.edu.cn", "wangweijie@zju.edu.cn"))
        reply_headers = HeaderInfo("<m2>", "", (), "Re: 申请", "wangweijie@zju.edu.cn", ("candidate@example.com", "zip.lab@zju.edu.cn"))

        self.assertEqual(thread_key(candidate, candidate_headers, participants), thread_key(reply, reply_headers, participants))
        envelope = build_envelope([(candidate, candidate_headers), (reply, reply_headers)], participants, key="reply-thread")
        self.assertEqual([item.sender_address for item in envelope.incoming], ["candidate@example.com"])
        self.assertEqual([item.sender_address for item in envelope.outgoing], ["wangweijie@zju.edu.cn"])

    def test_one_ai_future_failure_does_not_discard_other_prepared_threads(self) -> None:
        good_message = StoredMessage(1, "1", "INBOX", "申请", "", "good@example.com", "2026-08-01T10:00:00+08:00", "申请", Path("/tmp/1.eml"))
        bad_message = StoredMessage(2, "2", "INBOX", "申请", "", "bad@example.com", "2026-08-01T11:00:00+08:00", "申请", Path("/tmp/2.eml"))
        good = ThreadEnvelope("good", "good@example.com", "申请", (good_message,), (good_message,), (), frozenset({"INBOX"}))
        bad = ThreadEnvelope("bad", "bad@example.com", "申请", (bad_message,), (bad_message,), (), frozenset({"INBOX"}))
        runner = RecruitingRunner.__new__(RecruitingRunner)
        runner.settings = SimpleNamespace(llm_concurrency=2)
        runner.store = SimpleNamespace(get_thread=lambda _key: None)

        def prepare(envelope):
            if envelope.key == "bad":
                raise RuntimeError("gateway 524")
            return ProcessedThread(envelope.key, envelope.candidate_address, envelope.latest_time, CandidateFields(name="Good", mail_type="candidate").normalized(), None, None, None, None, True)

        runner._prepare_thread = prepare
        results = list(runner._extract_changed({"good": good, "bad": bad}, {"good", "bad"}))

        self.assertEqual(len(results), 2)
        self.assertEqual(sum(thread is not None for thread, _envelope, _error in results), 1)
        self.assertEqual(sum(error is not None for _thread, _envelope, error in results), 1)

    def test_forwarded_mail_uses_original_candidate_address(self) -> None:
        message = StoredMessage(1, "1", "INBOX", "Fwd: 申请", "", "bohan@example.com", None, "Begin forwarded message:\nFrom: Candidate <candidate@example.com>\n申请材料", Path("/tmp/1.eml"))
        headers = HeaderInfo("<m1>", "", (), "Fwd: 申请", "bohan@example.com", ("zip.lab@example.com",))
        self.assertEqual(candidate_address(headers, "zip.lab@example.com", message.body_text), "candidate@example.com")

    def test_status_merge_preserves_human_progress(self) -> None:
        self.assertEqual(_merge_status("未筛选", "面试资格", "未通过"), "面试资格")
        self.assertEqual(_merge_status("未筛选", "未筛选", "未通过"), "未通过")
        self.assertEqual(_merge_status("未筛选", "面试通过", "面试资格"), "面试通过")

    def test_weekly_report_is_compact_and_linked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mail.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute("create table recruiting_threads(fields_json text, latest_time text, screening_status text, doc_url text, base_record_id text, status text)")
            conn.execute("insert into recruiting_threads values(?,?,?,?,?,?)", (json.dumps({"name":"A","mail_type":"candidate","projects":["MLSys"],"academic_display":"4.0/4.0 · Top 5%"}), "2026-08-26 09:00", "未筛选", "https://example/doc", "rec-a", "active"))
            conn.execute("insert into recruiting_threads values(?,?,?,?,?,?)", (json.dumps({"name":"Old","mail_type":"candidate","projects":["MLSys"]}), "2026-06-01 09:00", "未筛选", "https://example/old", None, "inactive"))
            conn.commit(); conn.close()
            content, _ = render_weekly_report(path, datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc))
            self.assertIn("候选池 **1 人**", content)
            self.assertNotIn("vew37TarSs", content)
            self.assertIn("xiaolong-dev.me/maxread/mail", content)
            self.assertNotIn("vewaFIevDP", content)
            self.assertNotIn("vewVVbQsCs", content)
            self.assertNotIn("最近一周其他邮件", content)
            self.assertNotIn("vewmpcpnxQ", content)
            self.assertNotIn("实习生：0 人", content)

    def test_weekly_report_uses_native_clickable_links(self) -> None:
        post = markdown_to_post("## 标题\n\n**[候选人池](https://example/base)**")
        nodes = post["zh_cn"]["content"][0]
        self.assertTrue(any(node.get("tag") == "a" and node.get("href") == "https://example/base" for node in nodes))

    def test_weekly_report_strips_markdown_only_formatting(self) -> None:
        post = markdown_to_post("## 标题\n\n### 候选池入口\n\n- **[候选人池](https://example/base)**\n\n> 多 topic 候选人共用一条记录。")
        paragraphs = post["zh_cn"]["content"]
        rendered = "\n".join(node.get("text", "") for paragraph in paragraphs for node in paragraph)
        self.assertNotIn("###", rendered)
        self.assertNotIn("> ", rendered)
        self.assertIn("候选池入口", rendered)
        self.assertIn("说明：多 topic", rendered)
        heading_nodes = paragraphs[0]
        self.assertIn("bold", heading_nodes[0].get("style", []))


if __name__ == "__main__":
    unittest.main()
