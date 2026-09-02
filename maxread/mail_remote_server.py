from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .mail_admin import (
    _mail_db_path,
    _mail_root,
    mail_admin_records,
    mail_admin_status,
    reconcile_mail_admin_actions,
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
    ThreadingHTTPServer((args.host, args.port), MailRemoteHandler).serve_forever()
    return 0


def _reconcile_loop() -> None:
    while True:
        try:
            reconcile_mail_admin_actions(_mail_db_path(_mail_root()), limit=10)
        except Exception:
            pass
        threading.Event().wait(30)


if __name__ == "__main__":
    raise SystemExit(main())
