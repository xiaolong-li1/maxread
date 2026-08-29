from __future__ import annotations

import imaplib
import re
import ssl
import base64
from datetime import UTC, datetime, timedelta

from .config import Settings
from .oauth import access_token


def decode_modified_utf7(value: str) -> str:
    """Decode the modified UTF-7 used for non-ASCII IMAP folder names."""
    result: list[str] = []
    index = 0
    while index < len(value):
        amp = value.find("&", index)
        if amp < 0:
            result.append(value[index:])
            break
        result.append(value[index:amp])
        end = value.find("-", amp)
        if end < 0:
            result.append(value[amp:])
            break
        encoded = value[amp + 1:end]
        if not encoded:
            result.append("&")
        else:
            encoded = encoded.replace(",", "/")
            encoded += "=" * (-len(encoded) % 4)
            try:
                result.append(base64.b64decode(encoded).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                result.append(value[amp:end + 1])
        index = end + 1
    return "".join(result)


def encode_modified_utf7(value: str) -> str:
    """Encode a Unicode folder name for IMAP commands."""
    result: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        encoded = base64.b64encode("".join(buffer).encode("utf-16-be")).decode("ascii")
        result.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
        buffer.clear()

    for character in value:
        if 0x20 <= ord(character) <= 0x7E and character != "&":
            flush()
            result.append(character)
        elif character == "&":
            flush()
            result.append("&-")
        else:
            buffer.append(character)
    flush()
    return "".join(result)


class ImapClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ImapClient":
        context = ssl.create_default_context()
        try:
            self.client = imaplib.IMAP4_SSL(
                self.settings.host,
                self.settings.port,
                ssl_context=context,
                timeout=30,
            )
            if self.settings.auth == "oauth2":
                token = access_token(self.settings.oauth2_token_cache, self.settings.oauth2_access_token)
                auth_string = f"user={self.settings.username}\x01auth=Bearer {token}\x01\x01".encode()
                self.client.authenticate("XOAUTH2", lambda _: auth_string)
            else:
                self.client.login(self.settings.username, self.settings.password or "")
            status, _ = self.client.select(self.settings.mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError(f"cannot select mailbox {self.settings.mailbox}")
        except imaplib.IMAP4.error as error:
            raise RuntimeError(f"IMAP authentication failed: {error}") from None
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.client is not None:
            try:
                self.client.logout()
            except imaplib.IMAP4.error:
                pass

    def uid_validity(self) -> str | None:
        return self.uid_validity_for(self.settings.mailbox)

    def uid_validity_for(self, folder: str) -> str | None:
        assert self.client is not None
        status, data = self.client.status(encode_modified_utf7(folder), "(UIDVALIDITY)")
        if status != "OK" or not data or not data[0]:
            return None
        match = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
        return match.group(1).decode() if match else None

    def select_folder(self, folder: str) -> None:
        assert self.client is not None
        status, _ = self.client.select(encode_modified_utf7(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot select mailbox folder {folder}")

    def list_folders(self) -> list[str]:
        assert self.client is not None
        status, data = self.client.list()
        if status != "OK":
            raise RuntimeError("IMAP LIST failed")
        folders: list[str] = []
        for item in data or []:
            if not isinstance(item, bytes):
                continue
            if b"\\Noselect" in item.upper():
                continue
            decoded = _folder_name_from_list_row(item)
            if decoded:
                folders.append(decoded)
        return list(dict.fromkeys(folders))

    def search_uids(self, last_uid: int, limit: int) -> list[int]:
        assert self.client is not None
        if last_uid > 0:
            status, data = self.client.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
        else:
            since = (datetime.now(UTC) - timedelta(days=self.settings.lookback_days)).strftime("%d-%b-%Y")
            status, data = self.client.uid("SEARCH", None, "SINCE", since)
        if status != "OK":
            raise RuntimeError("IMAP UID SEARCH failed")
        uids = [int(value) for value in (data[0] or b"").split()]
        return sorted(uids)[:limit]

    def fetch_raw(self, uid: int) -> bytes:
        assert self.client is not None
        status, data = self.client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError(f"IMAP UID FETCH failed for {uid}")
        chunks = [item[1] for item in data if isinstance(item, tuple) and isinstance(item[1], bytes)]
        if not chunks:
            raise RuntimeError(f"IMAP returned no message body for {uid}")
        return b"".join(chunks)


def _folder_name_from_list_row(item: bytes) -> str:
    match = re.match(rb"\([^)]*\)\s+(?:\"[^\"]*\"|NIL)\s+(.+)$", item.strip())
    raw_name = (match.group(1) if match else item.rsplit(b" ", 1)[-1]).strip()
    if len(raw_name) >= 2 and raw_name[:1] == raw_name[-1:] == b'"':
        raw_name = raw_name[1:-1]
    return decode_modified_utf7(raw_name.decode("ascii", errors="replace"))
