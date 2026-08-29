from __future__ import annotations

import tempfile
import unittest
import json
import time
import os
from argparse import Namespace
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mail_collector.cli import command_configure
from mail_collector.oauth import access_token
from mail_collector.config import Settings
from mail_collector.imap_client import ImapClient, _folder_name_from_list_row, decode_modified_utf7, encode_modified_utf7
from mail_collector.parser import parse_message
from mail_collector.store import Store


def sample_message() -> bytes:
    message = EmailMessage()
    message["Subject"] = "科研实习申请-浙江大学-林同学"
    message["From"] = "Lin <candidate@example.com>"
    message["To"] = "zip.lab@example.com"
    message["Message-ID"] = "<fixture-1@example.com>"
    message["Date"] = "Sun, 24 Aug 2026 10:00:00 +0800"
    message.set_content("老师您好，附件是我的简历，包含科研经历和成绩排名。")
    message.add_attachment(b"%PDF-1.4\nfixture", maintype="application", subtype="pdf", filename="简历.pdf")
    return message.as_bytes()


class ParserTest(unittest.TestCase):
    def test_imap_client_exposes_scan_methods(self) -> None:
        self.assertTrue(callable(ImapClient.search_uids))
        self.assertTrue(callable(ImapClient.fetch_raw))

    def test_parses_candidate_and_pdf(self) -> None:
        parsed = parse_message(sample_message())
        self.assertEqual(parsed.subject, "科研实习申请-浙江大学-林同学")
        self.assertEqual(parsed.sender_address, "candidate@example.com")
        self.assertTrue(parsed.likely_candidate)
        self.assertGreaterEqual(parsed.candidate_score, 3)
        self.assertEqual(len(parsed.attachments), 1)
        self.assertTrue(parsed.attachments[0].is_pdf)

    def test_docx_attachment_is_candidate_evidence(self) -> None:
        message = EmailMessage()
        message["Subject"] = "材料补充"
        message["From"] = "candidate@example.com"
        message["To"] = "lab@example.com"
        message.set_content("附件是我的材料。")
        message.add_attachment(
            b"PK\x03\x04fixture",
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="resume.docx",
        )

        parsed = parse_message(message.as_bytes())

        self.assertTrue(parsed.likely_candidate)
        self.assertIn("has_resume_document_attachment", parsed.candidate_reasons)

    def test_imap_folder_modified_utf7_round_trip(self) -> None:
        for folder in ("Inbox", "初筛", "面试通过", "A&B"):
            self.assertEqual(decode_modified_utf7(encode_modified_utf7(folder)), folder)

    def test_imap_list_preserves_quoted_folder_names_with_spaces(self) -> None:
        self.assertEqual(_folder_name_from_list_row(b'(\\Sent) "/" "Sent Items"'), "Sent Items")
        self.assertEqual(_folder_name_from_list_row(b'(\\Junk) "/" "Junk E-mail"'), "Junk E-mail")


class StoreTest(unittest.TestCase):
    def test_persist_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "collector.sqlite3", root / "data", 1024 * 1024)
            parsed = parse_message(sample_message())
            first_id, first_created = store.persist("fixture", "42", parsed)
            second_id, second_created = store.persist("fixture", "42", parsed)
            self.assertEqual(first_id, second_id)
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(store.summary()["total"], 1)
            self.assertTrue((root / "data" / "messages" / "fixture" / "42" / "01-简历.pdf").exists())

    def test_same_uid_in_different_folders_cannot_overwrite_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "collector.sqlite3", root / "data", 1024 * 1024)
            parsed = parse_message(sample_message())

            inbox_id, _ = store.persist("INBOX", "42", parsed)
            inbox_raw = root / "data" / "messages" / "INBOX" / "42" / "message.eml"
            sent_id, _ = store.persist("Sent", "42", parsed)
            sent_raw = root / "data" / "messages" / "Sent" / "42" / "message.eml"

            self.assertNotEqual(inbox_id, sent_id)
            self.assertTrue(inbox_raw.exists())
            self.assertTrue(sent_raw.exists())
            inbox_raw.write_bytes(b"sentinel")
            store.persist("INBOX", "42", parsed)
            self.assertEqual(inbox_raw.read_bytes(), b"sentinel")


class ConfigureTest(unittest.TestCase):
    def test_secret_is_written_to_private_env_without_being_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            args = Namespace(
                env_file=str(env_path),
                force=False,
                username="group@example.com",
                host="outlook.office365.com",
                port=993,
                mailbox="INBOX",
                auth="password",
                lookback_days=30,
                scan_limit=100,
                max_attachment_mb=25,
            )
            with patch("mail_collector.cli.getpass.getpass", return_value="dummy-secret"):
                command_configure(args)
            self.assertIn("IMAP_PASSWORD=dummy-secret", env_path.read_text(encoding="utf-8"))
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)

    def test_non_read_only_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("MAIL_READ_ONLY=0\n", encoding="utf-8")
            previous = os.environ.pop("MAIL_READ_ONLY", None)
            try:
                with self.assertRaises(ValueError):
                    Settings.from_env(str(env_path), require_credentials=False)
            finally:
                if previous is not None:
                    os.environ["MAIL_READ_ONLY"] = previous

    def test_unexpired_oauth_token_is_loaded_from_private_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "token.json"
            cache_path.write_text(json.dumps({
                "access_token": "dummy-access-token",
                "expires_at": int(time.time()) + 3600,
            }), encoding="utf-8")
            self.assertEqual(access_token(cache_path), "dummy-access-token")


if __name__ == "__main__":
    unittest.main()
