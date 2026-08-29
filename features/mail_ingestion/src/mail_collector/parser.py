from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    payload: bytes
    sha256: str
    is_pdf: bool


@dataclass(frozen=True)
class ParsedMessage:
    message_id: str
    subject: str
    sender_name: str
    sender_address: str
    recipients: tuple[str, ...]
    received_at: str | None
    body_text: str
    attachments: tuple[Attachment, ...]
    candidate_score: int
    likely_candidate: bool
    candidate_reasons: tuple[str, ...]
    raw_bytes: bytes


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    chunks: list[str] = []
    for payload, charset in decode_header(value):
        if isinstance(payload, bytes):
            chunks.append(payload.decode(charset or "utf-8", errors="replace"))
        else:
            chunks.append(payload)
    return "".join(chunks).strip()


def _safe_filename(filename: str, index: int) -> str:
    name = Path(filename).name.strip().replace("\x00", "")
    name = re.sub(r"[^\w.()\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE)
    return name[:180] or f"attachment-{index}"


def _candidate_score(subject: str, body: str, attachments: tuple[Attachment, ...], sender: str) -> tuple[int, tuple[str, ...]]:
    score = 0
    reasons: list[str] = []
    combined = f"{subject}\n{body[:5000]}".lower()
    subject_patterns = ("申请", "实习", "硕士", "博士", "phd", "intern", "application", "candidate")
    body_patterns = ("简历", "resume", "curriculum vitae", "科研", "research", "成绩", "排名")

    if any(item.is_pdf or Path(item.filename).suffix.casefold() in {".doc", ".docx", ".odt", ".rtf"} for item in attachments):
        score += 3
        reasons.append("has_resume_document_attachment")
    if any(pattern in subject.lower() for pattern in subject_patterns):
        score += 2
        reasons.append("subject_keyword")
    if any(pattern in combined for pattern in body_patterns):
        score += 1
        reasons.append("body_keyword")
    if "noreply" in sender.lower() or "no-reply" in sender.lower():
        score -= 3
        reasons.append("automated_sender")
    return score, tuple(reasons)


def parse_message(raw_bytes: bytes) -> ParsedMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    subject = _decode(message.get("Subject"))
    senders = getaddresses(message.get_all("From", []))
    sender_name, sender_address = senders[0] if senders else ("", "")
    sender_name = _decode(sender_name)
    recipients = tuple(address for _, address in getaddresses(message.get_all("To", []) + message.get_all("Cc", [])) if address)

    received_at: str | None = None
    if message.get("Date"):
        try:
            parsed = parsedate_to_datetime(message.get("Date"))
            received_at = parsed.isoformat() if parsed else None
        except (TypeError, ValueError, OverflowError):
            received_at = None

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []
    for index, part in enumerate(message.walk(), start=1):
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = _decode(part.get_filename())
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True) or b""

        if disposition == "attachment" or filename:
            clean_name = _safe_filename(filename, index)
            digest = hashlib.sha256(payload).hexdigest()
            is_pdf = content_type == "application/pdf" or clean_name.lower().endswith(".pdf")
            attachments.append(Attachment(clean_name, content_type, payload, digest, is_pdf))
            continue

        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if content_type == "text/plain":
            plain_parts.append(text.strip())
        elif content_type == "text/html":
            html_parts.append(text)

    body_text = "\n\n".join(part for part in plain_parts if part).strip()
    if not body_text and html_parts:
        extractor = _TextExtractor()
        for html in html_parts:
            extractor.feed(html)
        body_text = extractor.text().strip()

    attachment_tuple = tuple(attachments)
    score, reasons = _candidate_score(subject, body_text, attachment_tuple, sender_address)
    message_id = _decode(message.get("Message-ID")) or hashlib.sha256(raw_bytes).hexdigest()
    return ParsedMessage(
        message_id=message_id,
        subject=subject,
        sender_name=sender_name,
        sender_address=sender_address,
        recipients=recipients,
        received_at=received_at,
        body_text=body_text,
        attachments=attachment_tuple,
        candidate_score=score,
        likely_candidate=score >= 3,
        candidate_reasons=reasons,
        raw_bytes=raw_bytes,
    )
