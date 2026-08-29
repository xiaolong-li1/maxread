from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Iterable

from .models import StoredMessage, ThreadEnvelope


_SUBJECT_PREFIX = re.compile(r"^(?:re|fw|fwd|回复|答复|转发)\s*[:：]\s*", re.I)
_FORWARDED_MARKER = re.compile(r"(?:begin forwarded message|原始邮件|转发邮件)", re.I)
_FORWARDED_ADDRESS = re.compile(r"(?:^|\n)\s*(?:from|发件人)\s*:?[^\n<]*<([^>\s]+@[^>\s]+)>", re.I)


@dataclass(frozen=True)
class HeaderInfo:
    message_id: str
    in_reply_to: str
    references: tuple[str, ...]
    subject: str
    sender: str
    recipients: tuple[str, ...]


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (TypeError, ValueError):
        return value.strip()


def read_headers(raw_path: Path) -> HeaderInfo:
    message = BytesParser(policy=policy.default).parsebytes(raw_path.read_bytes())
    addresses = getaddresses(message.get_all("From", []) or [])
    sender = addresses[0][1].strip().lower() if addresses else ""
    recipients = tuple(
        address.strip().lower()
        for _, address in getaddresses((message.get_all("To", []) or []) + (message.get_all("Cc", []) or []))
        if address
    )
    references = tuple(token for token in re.findall(r"<[^>]+>", message.get("References", "")))
    return HeaderInfo(
        message_id=_decode(message.get("Message-ID")),
        in_reply_to=_decode(message.get("In-Reply-To")),
        references=references,
        subject=_decode(message.get("Subject")),
        sender=sender,
        recipients=recipients,
    )


def normalize_subject(subject: str) -> str:
    value = " ".join((subject or "").replace("\u200b", " ").split()).strip()
    previous = None
    while value and value != previous:
        previous = value
        value = _SUBJECT_PREFIX.sub("", value).strip()
    return value.casefold()


def candidate_address(headers: HeaderInfo, mailbox_address: str | Iterable[str], body_text: str = "") -> str:
    mailboxes = _mailbox_addresses(mailbox_address)
    if headers.sender and headers.sender not in mailboxes:
        if _FORWARDED_MARKER.search(body_text) and headers.subject.casefold().startswith(("fwd", "fw", "转发")):
            forwarded = next((item.casefold() for item in _FORWARDED_ADDRESS.findall(body_text) if item.casefold() not in mailboxes | {headers.sender}), "")
            if forwarded:
                return forwarded
        return headers.sender
    for recipient in headers.recipients:
        if recipient not in mailboxes:
            return recipient
    return headers.sender or "unknown"


def thread_key(message: StoredMessage, headers: HeaderInfo, mailbox_address: str | Iterable[str]) -> str:
    # The stable candidate address + normalized subject is deliberately used as
    # a fallback because Outlook folders sometimes omit References when a thread
    # is moved. Message-ID metadata is still recorded in the local audit table.
    address = candidate_address(headers, mailbox_address, message.body_text)
    value = f"{address}|{normalize_subject(headers.subject or message.subject)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def build_envelope(messages: list[tuple[StoredMessage, HeaderInfo]], mailbox_address: str | Iterable[str], key: str | None = None) -> ThreadEnvelope:
    if not messages:
        raise ValueError("cannot build an empty thread")
    source_accounts = frozenset(item.source_account for item, _headers in messages if item.source_account)
    unique_messages: list[tuple[StoredMessage, HeaderInfo]] = []
    seen: set[str] = set()
    for item, headers in messages:
        identity = headers.message_id.casefold() if headers.message_id else f"{item.source_account}|{item.mailbox}|{item.source_uid}"
        if identity in seen:
            continue
        seen.add(identity)
        unique_messages.append((item, headers))
    first_message, first_headers = unique_messages[0]
    key = key or thread_key(first_message, first_headers, mailbox_address)
    candidate = candidate_address(first_headers, mailbox_address, first_message.body_text)
    ordered = tuple(sorted((item[0] for item in unique_messages), key=lambda item: item.received_at or ""))
    # A group member may reply directly from a personal address (e.g. Bohan)
    # while CC'ing the shared mailbox.  Treat only the candidate address as an
    # incoming sender; all other participants are our outgoing replies.
    incoming = tuple(
        item
        for item, headers in unique_messages
        if headers.sender == candidate
        or (
            headers.subject.casefold().startswith(("fwd", "fw", "转发"))
            and _FORWARDED_MARKER.search(item.body_text)
            and candidate in item.body_text.casefold()
        )
    )
    outgoing = tuple(item for item, headers in unique_messages if headers.sender != candidate)
    folders = frozenset(item.mailbox for item, _ in unique_messages)
    return ThreadEnvelope(
        key=key,
        candidate_address=candidate,
        subject=first_headers.subject or first_message.subject,
        messages=ordered,
        incoming=incoming,
        outgoing=outgoing,
        folders=folders,
        source_accounts=source_accounts,
    )


def _mailbox_addresses(value: str | Iterable[str]) -> set[str]:
    values = (value,) if isinstance(value, str) else value
    return {str(item).strip().casefold() for item in values if str(item).strip()}
