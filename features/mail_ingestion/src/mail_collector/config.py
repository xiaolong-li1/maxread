from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Load a small .env file without adding a runtime dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    username: str
    mailbox: str
    auth: str
    password: str | None
    oauth2_access_token: str | None
    oauth2_token_cache: Path
    lookback_days: int
    scan_limit: int
    max_attachment_bytes: int
    read_only: bool
    data_dir: Path
    db_path: Path

    @classmethod
    def from_env(cls, env_file: str = ".env", require_credentials: bool = True) -> "Settings":
        load_dotenv(Path(env_file))
        auth = os.getenv("IMAP_AUTH", "oauth2").strip().lower()
        if auth not in {"oauth2", "password"}:
            raise ValueError("IMAP_AUTH must be oauth2 or password")

        host = os.getenv("IMAP_HOST", "").strip()
        username = os.getenv("IMAP_USERNAME", "").strip()
        password = os.getenv("IMAP_PASSWORD") or None
        token = os.getenv("IMAP_OAUTH2_ACCESS_TOKEN") or None
        data_dir = Path(os.getenv("MAIL_DATA_DIR", "./data")).expanduser().resolve()
        token_cache = Path(os.getenv("MS_TOKEN_CACHE", str(data_dir / "secrets" / "outlook-token.json"))).expanduser().resolve()
        read_only = os.getenv("MAIL_READ_ONLY", "1").strip().lower() in {"1", "true", "yes", "on"}
        if not read_only:
            raise ValueError("This collector is permanently read-only; MAIL_READ_ONLY must be 1")
        if require_credentials:
            if not host or not username:
                raise ValueError("IMAP_HOST and IMAP_USERNAME are required")
            if auth == "oauth2" and not token and not token_cache.exists():
                raise ValueError("run outlook-auth first or set IMAP_OAUTH2_ACCESS_TOKEN")
            if auth == "password" and not password:
                raise ValueError("IMAP_PASSWORD is required for password auth")

        db_path = Path(os.getenv("MAIL_DB_PATH", str(data_dir / "mail_collector.sqlite3"))).expanduser().resolve()
        return cls(
            host=host,
            port=_positive_int("IMAP_PORT", 993),
            username=username,
            mailbox=os.getenv("IMAP_MAILBOX", "INBOX").strip() or "INBOX",
            auth=auth,
            password=password,
            oauth2_access_token=token,
            oauth2_token_cache=token_cache,
            lookback_days=_positive_int("MAIL_LOOKBACK_DAYS", 30),
            scan_limit=_positive_int("MAIL_SCAN_LIMIT", 100),
            max_attachment_bytes=_positive_int("MAIL_MAX_ATTACHMENT_MB", 25) * 1024 * 1024,
            read_only=True,
            data_dir=data_dir,
            db_path=db_path,
        )
