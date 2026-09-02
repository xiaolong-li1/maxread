from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .mail_admin import (
    _mail_db_path,
    _mail_root,
    mail_admin_records,
    mail_rejection_context,
    generate_mail_rejection_draft,
    create_mail_rejection_batch,
    mail_rejection_batch,
    queue_mail_rejection_batch_send,
    mail_admin_status,
    reconcile_mail_admin_actions,
    reconcile_mail_rejections,
    reconcile_mail_rejection_batches,
    save_mail_rejection_draft,
    save_mail_rejection_template,
    send_mail_rejection,
    sync_mail_admin_cache,
    trigger_mail_scan,
    update_mail_admin_config,
    update_mail_admin_record,
)


class MailRemoteHandler(BaseHTTPRequestHandler):
    server_version = "MaxReadMailControl/1"

    def do_GET(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json({"ok": True, "execution_host": socket.gethostname()})
            return
        if parsed.path == "/status":
            payload = mail_admin_status()
            payload["execution_host"] = socket.gethostname()
            payload["remote_execution"] = True
            self._json(payload)
            return
        if parsed.path == "/records":
            try:
                self._json(mail_admin_records(parsed.query))
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/rejection":
            try:
                query = parse_qs(parsed.query)
                self._json(mail_rejection_context(str(query.get("thread_key", [""])[0])))
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/rejection-batch":
            try:
                query = parse_qs(parsed.query)
                self._json(mail_rejection_batch(int(query.get("batch_id", ["0"])[0] or 0)))
            except (TypeError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        payload = self._body()
        try:
            if parsed.path == "/scan":
                self._json(trigger_mail_scan(str(payload.get("account") or "all")))
                return
            if parsed.path == "/config":
                self._json(update_mail_admin_config(
                    int(payload.get("scan_interval_minutes") or 0),
                    int(payload.get("report_interval_hours") or 0),
                ))
                return
            if parsed.path == "/record":
                self._json(update_mail_admin_record(
                    str(payload.get("thread_key") or ""),
                    payload.get("changes") if isinstance(payload.get("changes"), dict) else {},
                    str(payload.get("expected_updated_at") or ""),
                ))
                return
            if parsed.path == "/rejection-template":
                self._json(save_mail_rejection_template(
                    str(payload.get("subject") or ""),
                    str(payload.get("body") or ""),
                    str(payload.get("application_type") or "internship"),
                ))
                return
            if parsed.path == "/rejection-batch":
                self._json(create_mail_rejection_batch([
                    str(value) for value in payload.get("thread_keys") or []
                ]))
                return
            if parsed.path == "/rejection-batch-send":
                self._json(queue_mail_rejection_batch_send(
                    int(payload.get("batch_id") or 0),
                    str(payload.get("confirmation") or ""),
                ))
                return
            if parsed.path == "/rejection-generate":
                self._json(generate_mail_rejection_draft(str(payload.get("thread_key") or "")))
                return
            if parsed.path == "/rejection-draft":
                self._json(save_mail_rejection_draft(
                    str(payload.get("thread_key") or ""),
                    str(payload.get("subject") or ""),
                    str(payload.get("body") or ""),
                    str(payload.get("application_type") or "general"),
                    str(payload.get("generation_source") or "manual"),
                ))
                return
            if parsed.path == "/rejection-send":
                self._json(send_mail_rejection(
                    int(payload.get("draft_id") or 0),
                    str(payload.get("confirmation") or ""),
                ))
                return
        except (RuntimeError, ValueError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, _format: str, *_args) -> None:
        return

    def _authorized(self) -> bool:
        expected = str(os.environ.get("MAXREAD_MAIL_REMOTE_TOKEN", ""))
        provided = str(self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if expected and hmac.compare_digest(expected, provided):
            return True
        self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def _body(self) -> dict:
        try:
            size = min(64_000, max(0, int(self.headers.get("Content-Length") or 0)))
            value = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
            return value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, ValueError):
            return {}

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args(argv)
    if not str(os.environ.get("MAXREAD_MAIL_REMOTE_TOKEN", "")).strip():
        raise SystemExit("MAXREAD_MAIL_REMOTE_TOKEN is required")
    threading.Thread(
        target=_reconcile_loop,
        name="maxread-mail-admin-outbox",
        daemon=True,
    ).start()
    threading.Thread(
        target=_base_pull_loop,
        name="maxread-mail-base-cache",
        daemon=True,
    ).start()
    threading.Thread(
        target=_rejection_batch_loop,
        name="maxread-mail-rejection-batches",
        daemon=True,
    ).start()
    ThreadingHTTPServer((args.host, args.port), MailRemoteHandler).serve_forever()
    return 0


def _reconcile_loop() -> None:
    while True:
        try:
            reconcile_mail_admin_actions(_mail_db_path(_mail_root()), limit=10)
            reconcile_mail_rejections(_mail_db_path(_mail_root()), limit=5)
        except Exception:
            pass
        threading.Event().wait(30)


def _base_pull_loop() -> None:
    interval = max(60, int(os.environ.get("MAXREAD_BASE_PULL_INTERVAL_SECONDS", "180")))
    while True:
        try:
            sync_mail_admin_cache()
        except Exception:
            pass
        threading.Event().wait(interval)


def _rejection_batch_loop() -> None:
    while True:
        try:
            reconcile_mail_rejection_batches(_mail_db_path(_mail_root()), prepare_limit=3, send_limit=1)
        except Exception:
            pass
        threading.Event().wait(5)


if __name__ == "__main__":
    raise SystemExit(main())
