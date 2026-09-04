from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .config import Settings
from .imap_client import ImapClient
from .oauth import DEFAULT_OUTLOOK_CLIENT_ID, IMAP_SCOPES, IMAP_SMTP_SCOPES, begin_device_flow, complete_device_flow
from .parser import parse_message
from .store import Store


DEFAULT_EXCLUDED_FOLDERS = {
    "sent", "sent items", "已发送邮件", "已发送",
    "outbox", "发件箱",
    "drafts", "草稿",
    "deleted items", "deleted", "trash", "已删除邮件", "已删除",
}


def _settings(require_credentials: bool, env_file: str = ".env") -> Settings:
    return Settings.from_env(env_file=env_file, require_credentials=require_credentials)


def _store(settings: Settings) -> Store:
    return Store(settings.db_path, settings.data_dir, settings.max_attachment_bytes)


def command_init(_: argparse.Namespace) -> int:
    settings = _settings(False)
    _store(settings).initialize()
    print(json.dumps({"ok": True, "db_path": str(settings.db_path), "data_dir": str(settings.data_dir)}, ensure_ascii=False))
    return 0


def _single_line(value: str, name: str) -> str:
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} must be a non-empty single line")
    return value


def command_configure(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).expanduser().resolve()
    if env_path.exists() and not args.force:
        raise ValueError(f"{env_path} already exists; pass --force to replace it")

    username = _single_line(args.username or input("Mailbox address: "), "username")
    host = _single_line(args.host, "host")
    auth = args.auth
    secret_name = "IMAP_PASSWORD" if auth == "password" else "IMAP_OAUTH2_ACCESS_TOKEN"
    prompt = "Mailbox/app password (input hidden): " if auth == "password" else "OAuth2 access token (input hidden): "
    secret = _single_line(getpass.getpass(prompt), secret_name)

    values = {
        "IMAP_HOST": host,
        "IMAP_PORT": str(args.port),
        "IMAP_USERNAME": username,
        "IMAP_MAILBOX": args.mailbox,
        "IMAP_AUTH": auth,
        "IMAP_OAUTH2_ACCESS_TOKEN": secret if auth == "oauth2" else "",
        "IMAP_PASSWORD": secret if auth == "password" else "",
        "MS_CLIENT_ID": "",
        "MS_TENANT": "consumers",
        "MS_TOKEN_CACHE": "./data/secrets/outlook-token.json",
        "MAIL_LOOKBACK_DAYS": str(args.lookback_days),
        "MAIL_SCAN_LIMIT": str(args.scan_limit),
        "MAIL_MAX_ATTACHMENT_MB": str(args.max_attachment_mb),
        "MAIL_READ_ONLY": "1",
        "MAIL_DATA_DIR": "./data",
        "MAIL_DB_PATH": "./data/mail_collector.sqlite3",
    }
    env_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    print(json.dumps({
        "ok": True,
        "env_file": str(env_path),
        "username": username,
        "host": host,
        "auth": auth,
        "permissions": oct(env_path.stat().st_mode & 0o777),
    }, ensure_ascii=False))
    return 0


def _replace_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    rendered: list[str] = []
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            rendered.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            rendered.append(f"{key}={remaining.pop(key)}")
        else:
            rendered.append(line)
    rendered.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def command_outlook_auth(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).expanduser().resolve()
    settings = Settings.from_env(args.env_file, require_credentials=False)
    client_id = _single_line(args.client_id, "client_id")
    tenant = _single_line(args.tenant, "tenant")
    scopes = IMAP_SMTP_SCOPES if args.smtp_send else IMAP_SCOPES
    flow = begin_device_flow(client_id, tenant, scopes)
    print(json.dumps({
        "ok": True,
        "verification_uri": flow.get("verification_uri"),
        "user_code": flow.get("user_code"),
        "message": flow.get("message"),
    }, ensure_ascii=False), flush=True)
    complete_device_flow(
        client_id,
        tenant,
        flow,
        settings.oauth2_token_cache,
        expected_username=settings.username,
        scopes=scopes,
    )
    updates = {
        "IMAP_AUTH": "oauth2",
        "IMAP_PASSWORD": "",
        "IMAP_OAUTH2_ACCESS_TOKEN": "",
        "MS_CLIENT_ID": client_id,
        "MS_TENANT": tenant,
        "MS_TOKEN_CACHE": str(settings.oauth2_token_cache),
    }
    if args.smtp_send:
        updates.update({
            "SMTP_HOST": "smtp.office365.com",
            "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls",
            "SMTP_AUTH": "oauth2",
            "SMTP_TIMEOUT": "30",
            "RECRUITING_OUTBOUND_ENABLED": "0",
        })
    _replace_env_values(env_path, updates)
    print(json.dumps({
        "ok": True,
        "authorized": True,
        "authorized_username": json.loads(
            settings.oauth2_token_cache.read_text(encoding="utf-8")
        ).get("authorized_username", ""),
        "token_cache": str(settings.oauth2_token_cache),
        "permissions": oct(settings.oauth2_token_cache.stat().st_mode & 0o777),
    }, ensure_ascii=False))
    return 0


def command_import(args: argparse.Namespace) -> int:
    settings = _settings(False)
    path = Path(args.path).expanduser().resolve()
    raw = path.read_bytes()
    parsed = parse_message(raw)
    source_uid = f"file:{path.stem}:{parsed.message_id}"
    record_id, created = _store(settings).persist(args.mailbox, source_uid, parsed)
    print(json.dumps({
        "ok": True,
        "created": created,
        "record_id": record_id,
        "likely_candidate": parsed.likely_candidate,
        "candidate_score": parsed.candidate_score,
        "attachments": len(parsed.attachments),
    }, ensure_ascii=False))
    return 0


def command_scan(args: argparse.Namespace) -> int:
    settings = _settings(True, args.env_file)
    store = _store(settings)
    created = 0
    candidates = 0
    matched = 0
    folder_stats: list[dict[str, object]] = []

    with ImapClient(settings) as mailbox:
        if args.all_folders:
            folders = mailbox.list_folders()
        else:
            folders = [settings.mailbox]
        excluded = {name.casefold() for name in (args.exclude_folder or [])}
        if not args.include_system_folders:
            excluded.update(DEFAULT_EXCLUDED_FOLDERS)
        folders = [folder for folder in folders if folder.casefold() not in excluded]

        for folder in folders:
            mailbox.select_folder(folder)
            display_folder = settings.mailbox if folder.casefold() == "inbox" else folder
            storage_folder = f"{settings.username.casefold()}::{display_folder}"
            state = store.get_sync_state(storage_folder)
            uid_validity = mailbox.uid_validity_for(folder)
            last_uid = state.last_uid
            if state.uid_validity and uid_validity and state.uid_validity != uid_validity:
                last_uid = 0
            uids = mailbox.search_uids(last_uid, args.limit or settings.scan_limit)
            folder_created = 0
            folder_candidates = 0
            for uid in uids:
                raw = mailbox.fetch_raw(uid)
                parsed = parse_message(raw)
                matched += 1
                folder_candidates += int(parsed.likely_candidate)
                if not args.dry_run:
                    _, was_created = store.persist(storage_folder, str(uid), parsed)
                    created += int(was_created)
                    folder_created += int(was_created)
                    store.set_sync_state(storage_folder, uid_validity, uid)
            candidates += folder_candidates
            folder_stats.append({
                "folder": storage_folder,
                "matched": len(uids),
                "created": folder_created,
                "likely_candidates": folder_candidates,
                "last_uid": uids[-1] if uids else last_uid,
            })

    print(json.dumps({
        "ok": True,
        "dry_run": args.dry_run,
        "mailbox": settings.username,
        "folders": len(folders),
        "matched": matched,
        "created": created,
        "likely_candidates": candidates,
        "folder_stats": folder_stats,
    }, ensure_ascii=False))
    return 0


def command_restore_attachments(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.confirm:
        raise ValueError("restore-attachments requires --dry-run or --confirm")
    settings = _settings(True, args.env_file)
    folder = args.folder or settings.mailbox
    with ImapClient(settings) as mailbox:
        mailbox.select_folder(folder)
        parsed = parse_message(mailbox.fetch_raw(args.uid))
    storage_folder = f"{settings.username.casefold()}::{folder}"
    result = _store(settings).restore_attachments(
        storage_folder,
        str(args.uid),
        parsed,
        apply=bool(args.confirm),
    )
    print(json.dumps({"ok": True, "dry_run": not args.confirm, **result}, ensure_ascii=False, indent=2))
    return 0


def command_folders(args: argparse.Namespace) -> int:
    settings = _settings(True, args.env_file)
    with ImapClient(settings) as mailbox:
        print(json.dumps({"ok": True, "folders": mailbox.list_folders()}, ensure_ascii=False, indent=2))
    return 0


def command_status(args: argparse.Namespace) -> int:
    settings = _settings(False)
    store = _store(settings)
    print(json.dumps({"ok": True, "summary": store.summary(), "recent": store.recent(args.limit)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only local recruiting mailbox collector")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize the local SQLite database")
    init_parser.set_defaults(func=command_init)

    configure_parser = subparsers.add_parser("configure", help="securely create a local .env file; the secret is entered without echo")
    configure_parser.add_argument("--username", default=None, help="mailbox address; prompted when omitted")
    configure_parser.add_argument("--host", default="outlook.office365.com")
    configure_parser.add_argument("--port", type=int, default=993)
    configure_parser.add_argument("--mailbox", default="INBOX")
    configure_parser.add_argument("--auth", choices=("oauth2", "password"), default="password")
    configure_parser.add_argument("--lookback-days", type=int, default=30)
    configure_parser.add_argument("--scan-limit", type=int, default=100)
    configure_parser.add_argument("--max-attachment-mb", type=int, default=64)
    configure_parser.add_argument("--env-file", default=".env")
    configure_parser.add_argument("--force", action="store_true")
    configure_parser.set_defaults(func=command_configure)

    oauth_parser = subparsers.add_parser("outlook-auth", help="authorize Outlook IMAP through Microsoft device-code OAuth")
    oauth_parser.add_argument(
        "--client-id",
        default=DEFAULT_OUTLOOK_CLIENT_ID,
        help="Microsoft public client ID; defaults to better-email-mcp's bundled local client",
    )
    oauth_parser.add_argument("--tenant", default="consumers", help="use consumers for an outlook.com account")
    oauth_parser.add_argument("--env-file", default=".env")
    oauth_parser.add_argument("--smtp-send", action="store_true", help="also request Outlook SMTP.Send; outbound remains disabled")
    oauth_parser.set_defaults(func=command_outlook_auth)

    import_parser = subparsers.add_parser("import-eml", help="import a local .eml file for offline testing")
    import_parser.add_argument("path")
    import_parser.add_argument("--mailbox", default="local-fixture")
    import_parser.set_defaults(func=command_import)

    scan_parser = subparsers.add_parser("scan", help="incrementally scan an IMAP mailbox without changing message flags")
    scan_parser.add_argument("--env-file", default=".env", help="account-specific environment file")
    scan_parser.add_argument("--limit", type=int, default=None)
    scan_parser.add_argument("--all-folders", action="store_true", help="scan all selectable folders except sent/drafts/deleted by default")
    scan_parser.add_argument("--include-system-folders", action="store_true", help="include sent, drafts, and deleted folders")
    scan_parser.add_argument("--exclude-folder", action="append", default=[], help="exclude a folder name; may be repeated")
    scan_parser.add_argument("--dry-run", action="store_true", help="read and parse without writing SQLite or advancing the watermark")
    scan_parser.set_defaults(func=command_scan)

    restore_parser = subparsers.add_parser("restore-attachments", help="refetch skipped attachments for one existing IMAP UID")
    restore_parser.add_argument("--env-file", default=".env")
    restore_parser.add_argument("--folder", default="INBOX")
    restore_parser.add_argument("--uid", type=int, required=True)
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.add_argument("--confirm", action="store_true")
    restore_parser.set_defaults(func=command_restore_attachments)

    folders_parser = subparsers.add_parser("folders", help="list selectable IMAP folders without changing mailbox state")
    folders_parser.add_argument("--env-file", default=".env")
    folders_parser.set_defaults(func=command_folders)

    status_parser = subparsers.add_parser("status", help="show local collection counts and recent message metadata")
    status_parser.add_argument("--limit", type=int, default=10)
    status_parser.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, OSError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
