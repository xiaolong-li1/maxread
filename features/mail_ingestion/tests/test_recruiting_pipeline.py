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

from mail_collector.parser import parse_message
from recruiting_pipeline.config import PipelineSettings
from recruiting_pipeline.cli import build_parser
from recruiting_pipeline.base_sync import BaseSync
from recruiting_pipeline.attachment_text import extract_attachment_text
from recruiting_pipeline.models import ProcessedThread, StoredMessage, ThreadEnvelope
from recruiting_pipeline.runner import RecruitingRunner, _merge_status, _other_fields, _within_days
from recruiting_pipeline.llm import _fields_from_json, _strip_json_fence
from recruiting_pipeline.institution_tags import C9, PROJECT_985, classify_institution
from recruiting_pipeline.models import CandidateFields
from recruiting_pipeline.store import PipelineStore
from recruiting_pipeline.threading import HeaderInfo, build_envelope, candidate_address, normalize_subject
from recruiting_pipeline.weekly_report import markdown_to_post, render_weekly_report


class RecruitingPipelineTest(unittest.TestCase):
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
                f"RECRUITING_MAIL_ACCOUNT_ENVS={secondary}\n",
                encoding="utf-8",
            )

            settings = PipelineSettings.load(root, primary)

            self.assertEqual(settings.mailbox_env_files, (primary, secondary))
            self.assertEqual(settings.mailbox_addresses, ("zip.lab@zju.edu.cn", "bohan.zhuang@zju.edu.cn"))

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
            store.upsert_message(1, "key-a", "incoming", "INBOX")
            store.mark_message_processed(1)
            store.upsert_message(1, "key-b", "outgoing", "INBOX")
            self.assertEqual(store.message_processing_state()[1], ("key-b", False))
            store.mark_attachment_uploaded("key", "sha", "resume.pdf", "doc")
            self.assertEqual(store.uploaded_attachment_digests("key"), {"sha"})

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
            self.assertIn("vewaFIevDP", content)
            self.assertIn("vewVVbQsCs", content)
            self.assertIn("vewmpcpnxQ", content)
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
