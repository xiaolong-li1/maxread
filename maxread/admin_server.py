from __future__ import annotations

import json
import hashlib
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .admin_architecture import architecture_html, architecture_spec
from .config import Settings
from .db import Store
from .feedback import count_feedback_by_status, visible_feedback_rows
from .mail_admin import (
    MAIL_ADMIN_HTML,
    MAIL_SHARE_HTML,
    create_mail_candidate_share,
    list_mail_candidate_shares,
    mail_admin_records,
    mail_admin_status,
    mail_candidate_share,
    mail_rejection_context,
    generate_mail_rejection_draft,
    create_mail_rejection_batch,
    mail_rejection_batch,
    queue_mail_rejection_batch_send,
    reissue_mail_candidate_share,
    save_mail_rejection_draft,
    save_mail_rejection_template,
    send_mail_rejection,
    revoke_mail_candidate_share,
    trigger_mail_scan,
    update_mail_admin_config,
    update_mail_admin_record,
    update_mail_interest_groups,
)
from .review import visible_review_issues
from .remote_worker import (
    coordinator_claim,
    coordinator_event,
    coordinator_finish,
    coordinator_heartbeat,
    coordinator_transition,
)
from .web_submit import (
    WEB_SESSION_COOKIE,
    WEB_SUBMIT_HTML,
    create_web_project_category,
    delete_web_project_category,
    issue_binding_code,
    new_web_identity,
    organize_web_projects,
    retry_web_job,
    submit_web_papers,
    update_web_project,
    web_identity_payload,
)
from .web_pet import chat_with_project_pet, progress_payload
from .workflow import InvalidWorkflowTransition


DEFAULT_LIMIT = 80
DEFAULT_DAYS = 3
CONTACT_LOOKUP_TIMEOUT_SECONDS = 5
ADMIN_SESSION_COOKIE = "maxread_admin_session"
ADMIN_SESSION_SECONDS = 30 * 24 * 60 * 60
ADMIN_LOGIN_WINDOW_SECONDS = 10 * 60
ADMIN_LOGIN_MAX_FAILURES = 5
WEB_SUBMIT_WINDOW_SECONDS = 10 * 60
WEB_SUBMIT_MAX_REQUESTS = 8
WEB_PET_MAX_REQUESTS = 30


class AdminServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, settings: Settings):
        super().__init__(server_address, handler_class)
        self.settings = settings
        self.admin_login_failures: dict[str, list[float]] = {}
        self.web_submit_requests: dict[str, list[float]] = {}
        self.web_pet_requests: dict[str, list[float]] = {}
        self.admin_lock = threading.Lock()

    def create_admin_session(self, username: str, password: str, client_id: str) -> tuple[str, str]:
        now = time.time()
        with self.admin_lock:
            failures = [
                timestamp
                for timestamp in self.admin_login_failures.get(client_id, [])
                if now - timestamp < ADMIN_LOGIN_WINDOW_SECONDS
            ]
            if len(failures) >= ADMIN_LOGIN_MAX_FAILURES:
                self.admin_login_failures[client_id] = failures
                return "", "too_many_attempts"
            expected_username = str(getattr(self.settings, "admin_username", "") or "").strip().casefold()
            candidate_username = str(username or "").strip().casefold()
            expected = str(getattr(self.settings, "admin_password_hash", "") or "").strip().lower()
            candidate = hashlib.sha256(str(password or "").encode("utf-8")).hexdigest()
            username_ok = bool(expected_username) and secrets.compare_digest(candidate_username, expected_username)
            password_ok = bool(expected) and secrets.compare_digest(candidate, expected)
            if not username_ok or not password_ok:
                failures.append(now)
                self.admin_login_failures[client_id] = failures
                return "", "invalid_password"
            self.admin_login_failures.pop(client_id, None)
            token = secrets.token_urlsafe(32)
            store = Store(self.settings.db_path)
            try:
                store.create_admin_session(_admin_token_hash(token), int(now + ADMIN_SESSION_SECONDS))
            finally:
                store.close()
            return token, ""

    def allow_web_submission(self, client_id: str) -> bool:
        now = time.time()
        with self.admin_lock:
            requests = [
                timestamp for timestamp in self.web_submit_requests.get(client_id, [])
                if now - timestamp < WEB_SUBMIT_WINDOW_SECONDS
            ]
            if len(requests) >= WEB_SUBMIT_MAX_REQUESTS:
                self.web_submit_requests[client_id] = requests
                return False
            requests.append(now)
            self.web_submit_requests[client_id] = requests
            return True

    def allow_pet_chat(self, client_id: str) -> bool:
        now = time.time()
        with self.admin_lock:
            requests = [
                timestamp for timestamp in self.web_pet_requests.get(client_id, [])
                if now - timestamp < WEB_SUBMIT_WINDOW_SECONDS
            ]
            if len(requests) >= WEB_PET_MAX_REQUESTS:
                self.web_pet_requests[client_id] = requests
                return False
            requests.append(now)
            self.web_pet_requests[client_id] = requests
            return True

    def is_admin_session(self, token: str) -> bool:
        if not token:
            return False
        store = Store(self.settings.db_path)
        try:
            return store.is_admin_session(_admin_token_hash(token), int(time.time()))
        finally:
            store.close()

    def delete_admin_session(self, token: str) -> None:
        if not token:
            return
        store = Store(self.settings.db_path)
        try:
            store.delete_admin_session(_admin_token_hash(token))
        finally:
            store.close()


def _admin_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def run_admin_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = AdminServer((host, int(port)), AdminHandler, settings)
    print(f"MaxRead admin: http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class AdminHandler(BaseHTTPRequestHandler):
    server: AdminServer

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._html(WEB_SUBMIT_HTML)
            return
        if parsed.path in {"/admin", "/admin/"}:
            self._html(INDEX_HTML)
            return
        if parsed.path in {"/mail", "/mail/"}:
            self._html(MAIL_ADMIN_HTML)
            return
        if re.fullmatch(r"/mail/share/[A-Za-z0-9_-]{40,64}/?", parsed.path):
            self._html(
                MAIL_SHARE_HTML,
                headers={"cache-control": "no-store", "x-robots-tag": "noindex, nofollow"},
            )
            return
        if parsed.path in {"/submit", "/submit/", "/projects", "/projects/"}:
            self._html(WEB_SUBMIT_HTML)
            return
        if parsed.path == "/assets/web-pet-sprite.png":
            self._binary(Path(__file__).resolve().parent / "static" / "web-pet-sprite.png", "image/png")
            return
        if parsed.path == "/api/web/me":
            self._web_json(lambda _store, identity: web_identity_payload(identity))
            return
        if parsed.path == "/api/web/submissions":
            self._web_json(
                lambda store, identity: store.list_web_submissions(identity["public_id"], 30)
            )
            return
        if parsed.path == "/api/web/messages":
            query = parse_qs(parsed.query)
            try:
                after_id = max(0, int(query.get("after_id", ["0"])[0] or 0))
            except ValueError:
                after_id = 0
            self._web_json(
                lambda store, identity: store.list_web_messages(identity, after_id=after_id, limit=150)
            )
            return
        if parsed.path == "/api/web/progress":
            self._web_json(
                lambda store, identity: progress_payload(self.server.settings, store, identity)
            )
            return
        if parsed.path == "/api/web/admin/accounts":
            if not self._require_admin():
                return
            self._json_response(self._with_store(lambda store: _resolved_web_accounts(self.server.settings, store)))
            return
        if parsed.path == "/architecture":
            self._html(architecture_html())
            return
        if parsed.path == "/api/workflow-spec":
            self._json_response(architecture_spec())
            return
        if parsed.path == "/api/summary":
            if not self._require_admin():
                return
            self._json_response(self._with_store(_admin_summary))
            return
        if parsed.path == "/api/service-status":
            if not self._require_admin():
                return
            self._json_response(self._with_store(lambda store: store.get_service_status()))
            return
        if parsed.path == "/api/admin/status":
            authenticated = self._is_admin()
            self._json_response({
                "authenticated": authenticated,
                "username": str(getattr(self.server.settings, "admin_username", "") or "") if authenticated else "",
            })
            return
        if parsed.path == "/api/admin/mail/status":
            if not self._require_admin():
                return
            try:
                self._json_response(mail_admin_status())
            except Exception as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)[:500])
            return
        if parsed.path == "/api/admin/mail/records":
            if not self._require_admin():
                return
            try:
                self._json_response(mail_admin_records(parsed.query))
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/shares":
            if not self._require_admin():
                return
            try:
                query = parse_qs(parsed.query)
                self._json_response(list_mail_candidate_shares(int(query.get("limit", ["30"])[0] or 30)))
            except (TypeError, ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        public_share_match = re.fullmatch(r"/api/mail/share/([A-Za-z0-9_-]{40,64})", parsed.path)
        if public_share_match:
            try:
                self._json_response(mail_candidate_share(public_share_match.group(1)))
            except (ValueError, RuntimeError):
                self._error(HTTPStatus.NOT_FOUND, "分享不存在或已失效")
            return
        if parsed.path == "/api/admin/mail/rejection":
            if not self._require_admin():
                return
            try:
                query = parse_qs(parsed.query)
                self._json_response(mail_rejection_context(str(query.get("thread_key", [""])[0])))
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/rejection-batch":
            if not self._require_admin():
                return
            try:
                query = parse_qs(parsed.query)
                self._json_response(mail_rejection_batch(int(query.get("batch_id", ["0"])[0] or 0)))
            except (TypeError, ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/usage":
            if not self._require_admin():
                return
            since, sender_id = _record_filters(parsed.query)
            limit = _limit(parsed.query)
            self._json_response(self._with_store(lambda store: _attach_user_names(self.server.settings, store.list_usage_events(limit, since, sender_id), store)))
            return
        if parsed.path == "/api/users":
            if not self._require_admin():
                return
            since, _sender_id = _record_filters(parsed.query)
            self._json_response(self._with_store(lambda store: _attach_user_names(self.server.settings, store.list_usage_users(since), store)))
            return
        if parsed.path == "/api/feedback":
            if not self._require_admin():
                return
            query = parse_qs(parsed.query)
            since, sender_id = _record_filters(parsed.query)
            limit = _limit(parsed.query)
            status = query.get("status", [""])[0]
            self._json_response(self._with_store(lambda store: _attach_user_names(self.server.settings, visible_feedback_rows(store.list_feedback(limit, status, since, sender_id)), store)))
            return
        if parsed.path == "/api/jobs":
            if not self._require_admin():
                return
            query = parse_qs(parsed.query)
            since, sender_id = _record_filters(parsed.query)
            limit = _limit(parsed.query)
            status = query.get("status", [""])[0]
            self._json_response(self._with_store(lambda store: _attach_user_names(self.server.settings, store.list_queue_jobs(limit, status, since, sender_id), store)))
            return
        if parsed.path == "/api/job-events":
            if not self._require_admin():
                return
            query = parse_qs(parsed.query)
            since, sender_id = _record_filters(parsed.query)
            limit = _limit(parsed.query)
            job_id = int(query.get("job_id", ["0"])[0] or 0)
            self._json_response(self._with_store(lambda store: _attach_user_names(self.server.settings, store.list_job_events(job_id, limit, since, sender_id), store)))
            return
        if parsed.path == "/api/review-issues":
            if not self._require_admin():
                return
            query = parse_qs(parsed.query)
            since, sender_id = _record_filters(parsed.query)
            limit = _limit(parsed.query)
            source_kind = query.get("source_kind", [""])[0]
            source_id = query.get("source_id", [""])[0]
            self._json_response(self._with_store(lambda store: visible_review_issues(store.list_review_issues(limit, source_kind, source_id, since, sender_id))))
            return
        if parsed.path == "/api/review-stats":
            if not self._require_admin():
                return
            self._json_response(self._with_store(lambda store: store.review_issue_stats()))
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        worker_handlers = {
            "/api/worker/claim": coordinator_claim,
            "/api/worker/heartbeat": coordinator_heartbeat,
            "/api/worker/transition": coordinator_transition,
            "/api/worker/event": coordinator_event,
            "/api/worker/finish": coordinator_finish,
        }
        if parsed.path in worker_handlers:
            if not self._require_worker():
                return
            try:
                payload = self._read_json()
                self._json_response(
                    self._with_store(
                        lambda store: worker_handlers[parsed.path](self.server.settings, store, payload)
                    )
                )
            except (TypeError, ValueError, InvalidWorkflowTransition) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)[:500])
            return
        if parsed.path == "/api/web/submit":
            if not self.server.allow_web_submission(self._client_id()):
                self._error(HTTPStatus.TOO_MANY_REQUESTS, "提交过于频繁，请稍后再试")
                return
            payload = self._read_json()
            try:
                self._web_json(
                    lambda store, identity: submit_web_papers(
                        self.server.settings,
                        store,
                        identity,
                        payload.get("content", ""),
                    )
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/web/binding-code":
            try:
                self._web_json(
                    lambda store, identity: issue_binding_code(store, identity)
                    if identity.get("_actor_type") != "admin"
                    else (_ for _ in ()).throw(ValueError("管理员代入态不能修改用户绑定"))
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/web/retry":
            payload = self._read_json()
            try:
                self._web_json(
                    lambda store, identity: retry_web_job(
                        self.server.settings, store, identity, int(payload.get("job_id") or 0)
                    )
                )
            except (TypeError, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/web/pet/chat":
            if not self.server.allow_pet_chat(self._client_id()):
                self._error(HTTPStatus.TOO_MANY_REQUESTS, "聊得有点快，稍后再试")
                return
            payload = self._read_json()
            try:
                self._web_json(
                    lambda store, identity: chat_with_project_pet(
                        self.server.settings,
                        store,
                        identity,
                        payload.get("content", ""),
                        job_id=int(payload.get("job_id") or 0),
                        source_id=str(payload.get("source_id") or ""),
                        history=payload.get("history") if isinstance(payload.get("history"), list) else [],
                    )
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/web/project-action":
            payload = self._read_json()
            try:
                self._web_json(
                    lambda store, identity: update_web_project(
                        store,
                        identity,
                        str(payload.get("source_id") or ""),
                        str(payload.get("action") or ""),
                        payload.get("value"),
                    )
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/web/organize":
            payload = self._read_json()
            try:
                self._web_json(
                    lambda store, identity: organize_web_projects(
                        self.server.settings,
                        store,
                        identity,
                        progress_payload(self.server.settings, store, identity).get("recent", []),
                        payload.get("source_ids") if isinstance(payload.get("source_ids"), list) else [],
                    )
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/web/categories":
            payload = self._read_json()
            try:
                self._web_json(
                    lambda store, identity: (
                        delete_web_project_category(
                            store,
                            identity,
                            str(payload.get("name") or ""),
                        )
                        if str(payload.get("action") or "create").strip().lower() == "delete"
                        else create_web_project_category(
                            store,
                            identity,
                            str(payload.get("name") or ""),
                        )
                    )
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/login":
            payload = self._read_json()
            token, error = self.server.create_admin_session(
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                str(self.client_address[0] if self.client_address else "unknown"),
            )
            if error:
                status = HTTPStatus.TOO_MANY_REQUESTS if error == "too_many_attempts" else HTTPStatus.UNAUTHORIZED
                self._error(status, "登录尝试过多，请稍后再试" if error == "too_many_attempts" else "管理员账号或密码错误")
                return
            self._json_response(
                {"ok": True, "authenticated": True, "username": str(getattr(self.server.settings, "admin_username", "") or "")},
                headers={"set-cookie": self._session_cookie(token)},
            )
            return
        if parsed.path == "/api/admin/logout":
            self.server.delete_admin_session(self._admin_token())
            self._json_response(
                {"ok": True, "authenticated": False},
                headers={"set-cookie": self._session_cookie("", max_age=0)},
            )
            return
        if parsed.path == "/api/admin/mail/shares":
            if not self._require_admin():
                return
            payload = self._read_json()
            try:
                action = str(payload.get("action") or "create")
                if action == "revoke":
                    self._json_response(revoke_mail_candidate_share(int(payload.get("share_id") or 0)))
                elif action == "reissue":
                    self._json_response(reissue_mail_candidate_share(
                        int(payload.get("share_id") or 0),
                        int(payload.get("expires_days") if payload.get("expires_days") is not None else 7),
                    ))
                else:
                    self._json_response(create_mail_candidate_share(
                        [str(value) for value in payload.get("thread_keys") or []],
                        str(payload.get("title") or ""),
                        int(payload.get("expires_days") if payload.get("expires_days") is not None else 7),
                    ))
            except (TypeError, ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/interest-groups":
            if not self._require_admin():
                return
            payload = self._read_json()
            try:
                self._json_response(update_mail_interest_groups(
                    str(payload.get("action") or ""),
                    name=str(payload.get("name") or ""),
                    group_id=int(payload.get("group_id") or 0),
                    thread_keys=[str(value) for value in payload.get("thread_keys") or []],
                ))
            except (TypeError, ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if not self._require_admin():
            return
        if parsed.path == "/api/admin/mail/scan":
            payload = self._read_json()
            try:
                self._json_response(trigger_mail_scan(str(payload.get("account") or "all")))
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/config":
            payload = self._read_json()
            try:
                self._json_response(update_mail_admin_config(
                    int(payload.get("scan_interval_minutes") or 0),
                    int(payload.get("report_interval_hours") or 0),
                ))
            except (TypeError, ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/record":
            payload = self._read_json()
            try:
                self._json_response(update_mail_admin_record(
                    str(payload.get("thread_key") or ""),
                    payload.get("changes") if isinstance(payload.get("changes"), dict) else {},
                    str(payload.get("expected_updated_at") or ""),
                ))
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/rejection-template":
            payload = self._read_json()
            try:
                self._json_response(save_mail_rejection_template(
                    str(payload.get("subject") or ""),
                    str(payload.get("body") or ""),
                    str(payload.get("application_type") or "internship"),
                ))
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/rejection-batch":
            payload = self._read_json()
            try:
                values = payload.get("thread_keys") if isinstance(payload.get("thread_keys"), list) else []
                self._json_response(create_mail_rejection_batch([str(value) for value in values]))
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/rejection-batch-send":
            payload = self._read_json()
            try:
                self._json_response(queue_mail_rejection_batch_send(
                    int(payload.get("batch_id") or 0),
                    str(payload.get("confirmation") or ""),
                ))
            except (TypeError, ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/rejection-generate":
            payload = self._read_json()
            try:
                self._json_response(generate_mail_rejection_draft(str(payload.get("thread_key") or "")))
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/rejection-draft":
            payload = self._read_json()
            try:
                self._json_response(save_mail_rejection_draft(
                    str(payload.get("thread_key") or ""),
                    str(payload.get("subject") or ""),
                    str(payload.get("body") or ""),
                    str(payload.get("application_type") or "general"),
                    str(payload.get("generation_source") or "manual"),
                ))
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/admin/mail/rejection-send":
            payload = self._read_json()
            try:
                self._json_response(send_mail_rejection(
                    int(payload.get("draft_id") or 0),
                    str(payload.get("confirmation") or ""),
                ))
            except (TypeError, ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/service-status":
            payload = self._read_json()
            try:
                status = self._with_store(
                    lambda store: store.set_service_status(
                        payload.get("mode", "operational"),
                        payload.get("reason", ""),
                        payload.get("expected_recovery_at", ""),
                        payload.get("updated_by", "admin"),
                    )
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._json_response({"ok": True, **status})
            return
        feedback_match = re.fullmatch(r"/api/feedback/(\d+)/status", parsed.path)
        if feedback_match:
            payload = self._read_json()
            status = str(payload.get("status", ""))
            feedback_id = int(feedback_match.group(1))
            try:
                ok = self._with_store(lambda store: store.update_feedback_status(feedback_id, status))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._json_response({"ok": ok, "id": feedback_id, "status": status})
            return
        retry_match = re.fullmatch(r"/api/jobs/(\d+)/retry", parsed.path)
        if retry_match:
            job_id = int(retry_match.group(1))
            ok = self._with_store(
                lambda store: store.retry_queue_job(
                    job_id,
                    reason="admin dashboard retry",
                    event_type="admin_retry",
                    suppress_progress_notifications=True,
                    reset_watcher_notifications=False,
                )
            )
            self._json_response({"ok": ok, "job_id": job_id})
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _with_store(self, fn):
        store = Store(self.server.settings.db_path)
        try:
            return fn(store)
        finally:
            store.close()

    def _web_json(self, fn) -> None:
        token = self._web_token()
        store = Store(self.server.settings.db_path)
        try:
            session_token, identity = new_web_identity(store, token)
            act_as = str(self.headers.get("x-maxread-act-as", "") or "").strip()
            if act_as:
                if not self._is_admin():
                    self._error(HTTPStatus.UNAUTHORIZED, "管理员代入态需要登录")
                    return
                target = store.get_web_identity_by_public_id(act_as)
                if target is None:
                    self._error(HTTPStatus.NOT_FOUND, "用户不存在")
                    return
                identity = target
                identity["_actor_type"] = "admin"
                identity["_actor_id"] = "admin"
            payload = fn(store, identity)
        finally:
            store.close()
        # Refresh the one-year device session on every successful scoped API
        # call so active users do not have to bind Feishu again annually.
        headers = {"set-cookie": self._web_session_cookie(session_token)}
        self._json_response(payload, headers=headers)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        if not length:
            return {}
        if length > 64 * 1024:
            raise ValueError("request body too large")
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def _admin_token(self) -> str:
        try:
            cookie = SimpleCookie(self.headers.get("cookie", ""))
            morsel = cookie.get(ADMIN_SESSION_COOKIE)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def _client_id(self) -> str:
        forwarded = str(self.headers.get("x-real-ip", "") or "").strip()
        return forwarded[:128] or str(self.client_address[0] if self.client_address else "unknown")

    def _web_token(self) -> str:
        try:
            cookie = SimpleCookie(self.headers.get("cookie", ""))
            morsel = cookie.get(WEB_SESSION_COOKIE)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def _is_admin(self) -> bool:
        return self.server.is_admin_session(self._admin_token())

    def _require_admin(self) -> bool:
        if self._is_admin():
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "需要管理员登录")
        return False

    def _require_worker(self) -> bool:
        expected = str(getattr(self.server.settings, "worker_token", "") or "")
        authorization = str(self.headers.get("authorization", "") or "")
        provided = authorization.removeprefix("Bearer ").strip()
        if expected and provided and secrets.compare_digest(expected, provided):
            return True
        self._error(HTTPStatus.FORBIDDEN, "worker authentication failed")
        return False

    def _session_cookie(self, token: str, max_age: int = ADMIN_SESSION_SECONDS) -> str:
        secure = self.headers.get("x-forwarded-proto", "").lower() == "https"
        parts = [
            f"{ADMIN_SESSION_COOKIE}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={max(0, int(max_age))}",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _web_session_cookie(self, token: str) -> str:
        secure = self.headers.get("x-forwarded-proto", "").lower() == "https"
        parts = [
            f"{WEB_SESSION_COOKIE}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={365 * 24 * 60 * 60}",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _html(self, content: str, headers: dict[str, str] | None = None) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/html; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self._write_body(data)

    def _binary(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "public, max-age=86400")
        self.end_headers()
        self._write_body(data)

    def _json_response(self, payload: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self._write_body(data)

    def _write_body(self, data: bytes) -> None:
        # The ZeroTier bridge can black-hole larger coalesced writes. Keep
        # dashboard responses in small flushed segments without changing the
        # host-wide interface MTU.
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        for offset in range(0, len(data), 1024):
            self.wfile.write(data[offset : offset + 1024])
            self.wfile.flush()

    def _error(self, status: HTTPStatus, message: str) -> None:
        data = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)



def _admin_summary(store: Store):
    summary = store.admin_summary()
    summary["feedback"] = count_feedback_by_status(store.list_feedback(limit=10000))
    summary["review_issues"] = len(visible_review_issues(store.list_review_issues(limit=10000)))
    return summary


def _record_filters(query_string: str) -> tuple[str, str]:
    query = parse_qs(query_string)
    raw_days = query.get("days", [str(DEFAULT_DAYS)])[0]
    try:
        days = max(0, min(3650, int(raw_days)))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    since = ""
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    sender_id = str(query.get("sender_id", [""])[0] or "").strip()[:128]
    return since, sender_id


def _attach_user_names(settings: Settings, rows, store=None):
    sender_ids = sorted({row.get("sender_id", "") for row in rows if row.get("sender_id", "")})
    if not sender_ids:
        return rows
    names = store.get_user_names(sender_ids) if store else {}
    unresolved_ids = [
        sender_id for sender_id in sender_ids
        if sender_id not in names and not sender_id.startswith("guest:")
    ]
    if not unresolved_ids:
        for row in rows:
            row["sender_name"] = names.get(row.get("sender_id", ""), "")
        return rows
    try:
        result = subprocess.run(
            [
                settings.lark_cli,
                "contact",
                "+search-user",
                "--as",
                "user",
                "--user-ids",
                ",".join(unresolved_ids),
                "--format",
                "json",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=CONTACT_LOOKUP_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "contact lookup failed")
        payload = json.loads(result.stdout or "{}")
        data = payload.get("data", {})
        users = data.get("users", []) or data.get("items", [])
        resolved = {}
        for user in users:
            sender_id = user.get("open_id", "") or user.get("user_id", "")
            display_name = (
                user.get("localized_name", "")
                or user.get("name", "")
                or user.get("display_name", "")
                or user.get("zh_name", "")
                or user.get("en_name", "")
            )
            if sender_id and display_name:
                resolved[sender_id] = display_name
        names.update(resolved)
        if store:
            store.save_user_names(resolved)
    except Exception:
        pass
    remaining = {sender_id for sender_id in unresolved_ids if sender_id not in names}
    message_ids = []
    for row in rows:
        message_id = str(row.get("message_id") or "").strip()
        if row.get("sender_id") in remaining and message_id.startswith("om_") and message_id not in message_ids:
            message_ids.append(message_id)
    accounts = store.list_web_accounts() if store else []
    for account in accounts:
        sender_id = str(account.get("feishu_open_id") or "").strip()
        message_id = str(account.get("binding_message_id") or "").strip()
        if sender_id in remaining and message_id.startswith("om_") and message_id not in message_ids:
            message_ids.append(message_id)
    if message_ids:
        try:
            result = subprocess.run(
                [
                    settings.lark_cli,
                    "im",
                    "+messages-mget",
                    "--as",
                    "bot",
                    "--message-ids",
                    ",".join(message_ids[:50]),
                    "--no-reactions",
                    "--format",
                    "json",
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=CONTACT_LOOKUP_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                payload = json.loads(result.stdout or "{}")
                data = payload.get("data", payload)
                resolved = {}
                for message in data.get("messages", []) if isinstance(data, dict) else []:
                    sender = message.get("sender") or {}
                    sender_id = str(sender.get("id") or sender.get("open_id") or "")
                    display_name = str(sender.get("name") or sender.get("display_name") or "").strip()
                    if sender_id in remaining and display_name:
                        resolved[sender_id] = display_name
                names.update(resolved)
                if store:
                    store.save_user_names(resolved)
                    for sender_id, display_name in resolved.items():
                        store.update_web_identity_display_name(sender_id, display_name)
        except Exception:
            pass
    if store:
        for account in accounts:
            sender_id = str(account.get("feishu_open_id") or "").strip()
            if not sender_id or sender_id in names:
                continue
            public_id = str(account.get("public_id") or "").strip()
            display_name = str(account.get("display_name") or "").strip()
            if not display_name or display_name in {"飞书用户", "未解析用户", "游客"}:
                suffix = public_id.removeprefix("web_")[-6:] or sender_id[-6:]
                display_name = f"网页用户 · {suffix}"
            names[sender_id] = display_name
    for row in rows:
        row["sender_name"] = names.get(row.get("sender_id", ""), "")
    return rows


def _resolved_web_accounts(settings: Settings, store: Store) -> list[dict[str, Any]]:
    accounts = store.list_web_accounts()
    probes = [
        {
            "sender_id": str(account.get("feishu_open_id") or ""),
            "message_id": str(account.get("binding_message_id") or ""),
        }
        for account in accounts
        if str(account.get("feishu_open_id") or "").strip()
    ]
    if probes:
        _attach_user_names(settings, probes, store)
        accounts = store.list_web_accounts()
    return accounts

def _limit(query: str) -> int:
    values = parse_qs(query).get("limit", [str(DEFAULT_LIMIT)])
    try:
        return max(1, min(300, int(values[0])))
    except Exception:
        return DEFAULT_LIMIT


INDEX_HTML = r'''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>MaxRead Admin</title>
  <style>
    :root {
      --bg: #fafafa;
      --panel: #ffffff;
      --line: #e7e5df;
      --soft: #f2f0ea;
      --text: #181b22;
      --muted: #727782;
      --primary: #2f6f5e;
      --accent: #c97b3f;
      --bad: #b42318;
      --warn: #b86e00;
      --ok: #147a54;
    }
    * { box-sizing: border-box; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", sans-serif; color: var(--text); background: var(--bg); font-size: 14px; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 48px; }
    header { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 20px; }
    h1 { margin: 0; font-size: 30px; line-height: 1.15; letter-spacing: 0; }
    .sub { margin-top: 8px; color: var(--muted); }
    .actions { display: flex; gap: 10px; align-items: center; }
    .architecture-link { height: 36px; display: inline-flex; align-items: center; gap: 7px; padding: 0 11px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--text); text-decoration: none; }
    .architecture-link:hover { text-decoration: none; border-color: #b9beb7; }
    .architecture-link svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    button, select { border: 1px solid var(--line); background: #fff; color: var(--text); border-radius: 8px; padding: 8px 11px; font: inherit; }
    button { cursor: pointer; transition: transform .15s, box-shadow .15s; }
    button:hover { transform: translateY(-1px); box-shadow: 0 6px 16px -12px rgba(0,0,0,.25); }
    button:disabled { cursor: not-allowed; opacity: .6; transform: none; box-shadow: none; }
    .primary { background: var(--primary); border-color: var(--primary); color: #fff; }
    .tabs { display: flex; gap: 8px; border-bottom: 1px solid var(--line); margin-bottom: 18px; overflow-x: auto; }
    .tab { border: 0; background: transparent; border-radius: 0; padding: 12px 8px 11px; color: var(--muted); box-shadow: none; white-space: nowrap; }
    .tab.active { color: var(--primary); border-bottom: 2px solid var(--primary); }
    .extra-tab { margin-left: auto; align-self: center; min-width: 112px; padding: 7px 10px; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; box-shadow: 0 4px 14px -12px rgba(0,0,0,.18); }
    .metric { grid-column: span 3; }
    .metric .label { color: var(--muted); font-size: 13px; }
    .metric .value { font-size: 28px; font-weight: 700; margin-top: 8px; }
    .wide { grid-column: span 12; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--soft); vertical-align: top; }
    th { color: var(--muted); font-weight: 600; font-size: 12px; }
    td { overflow-wrap: anywhere; }
    .jobs-card { overflow-x: auto; }
    .jobs-table { min-width: 900px; }
    .jobs-table th:nth-child(1) { width: 48px; }
    .jobs-table th:nth-child(2) { width: 150px; }
    .jobs-table th:nth-child(3) { width: 31%; }
    .jobs-table th:nth-child(4) { width: 78px; }
    .jobs-table th:nth-child(5) { width: 108px; }
    .jobs-table th:nth-child(6) { width: 96px; }
    .jobs-table th:nth-child(7) { width: 52px; }
    .jobs-table th:nth-child(8) { width: 78px; }
    .jobs-table th:nth-child(9) { width: 64px; }
    .job-subject strong { display: block; font-size: 14px; line-height: 1.4; overflow-wrap: anywhere; }
    .job-subject-meta { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 9px; margin-top: 5px; color: var(--muted); font-size: 12px; }
    .job-source-link { font-size: 12px; }
    .job-flow { line-height: 1.35; }
    .job-flow .mono, .job-stage .mono { display: block; margin-top: 3px; color: var(--muted); font-size: 10px; }
    .job-error { margin-top: 8px; border: 1px solid var(--soft); border-radius: 7px; background: #fbfaf7; }
    .job-error summary { cursor: pointer; padding: 6px 8px; color: var(--bad); font-size: 12px; }
    .job-error-body { max-height: 150px; overflow: auto; padding: 0 8px 8px; color: var(--muted); font-size: 11px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
    .job-recovery { margin-top: 8px; border: 1px solid rgba(184,110,0,.22); border-radius: 7px; background: rgba(184,110,0,.04); }
    .job-recovery summary { cursor: pointer; padding: 6px 8px; color: var(--warn); font-size: 12px; }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 3px 8px; font-size: 12px; background: var(--soft); color: var(--muted); }
    .pill.done { color: var(--ok); background: rgba(20,122,84,.08); }
    .pill.failed { color: var(--bad); background: rgba(180,35,24,.08); }
    .pill.running, .pill.queued { color: var(--warn); background: rgba(184,110,0,.1); }
    .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }
    .filters { display: flex; align-items: end; gap: 12px; padding: 12px 0 16px; border-top: 1px solid var(--line); }
    .filter-field { display: grid; gap: 5px; min-width: 150px; }
    .filter-field.user { min-width: 240px; }
    .filter-field label { color: var(--muted); font-size: 12px; }
    .muted { color: var(--muted); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .hidden { display: none; }
    .empty { padding: 26px; color: var(--muted); text-align: center; }
    .status-card { border-left: 4px solid var(--ok); }
    .status-card.degraded, .status-card.maintenance, .status-card.outage { border-left-color: var(--warn); }
    .status-grid { display: grid; grid-template-columns: 160px 1fr 220px; gap: 10px; align-items: start; }
    .status-grid textarea, .status-grid input, .status-grid select { width: 100%; min-height: 38px; border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font: inherit; background: #fff; }
    .status-grid textarea { min-height: 72px; resize: vertical; }
    .status-title { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    .status-title strong { font-size: 16px; }
    .status-readonly { display: grid; grid-template-columns: 150px 1fr 220px; gap: 16px; color: var(--muted); }
    .status-readonly strong { color: var(--text); }
    dialog { width: min(390px, calc(100vw - 28px)); border: 1px solid var(--line); border-radius: 10px; padding: 0; color: var(--text); background: var(--panel); box-shadow: 0 24px 70px rgba(0,0,0,.22); }
    dialog::backdrop { background: rgba(24,27,34,.32); }
    .login-form { display: grid; gap: 14px; padding: 20px; }
    .login-form h2 { margin: 0; font-size: 20px; }
    .login-form input { width: 100%; border: 1px solid var(--line); border-radius: 8px; padding: 10px 11px; font: inherit; }
    .login-actions { display: flex; justify-content: flex-end; gap: 9px; }
    .login-error { min-height: 20px; color: var(--bad); font-size: 13px; }
    a { color: var(--primary); text-decoration: none; }
    a:hover { text-decoration: underline; }
    @media (max-width: 860px) { .metric { grid-column: span 6; } header { align-items: flex-start; flex-direction: column; } }
    @media (max-width: 620px) {
      .wrap { padding: 20px 14px 36px; overflow: hidden; }
      header, .actions { width: 100%; }
      .filters { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
      .filter-field, .filter-field.user { min-width: 0; }
      .filter-field select { width: 100%; }
      .grid { grid-template-columns: minmax(0, 1fr); }
      .card, .metric, .wide { grid-column: 1; min-width: 0; }
      .status-title { align-items: flex-start; flex-direction: column; }
      .status-grid { grid-template-columns: minmax(0, 1fr); }
      .status-readonly { grid-template-columns: minmax(0, 1fr); gap: 8px; }
      .tabs { width: 100%; }
      table { font-size: 13px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>MaxRead 控制台</h1>
        <div class="sub">查看使用记录、反馈、队列和 AI review 问题。数据从 5090 实时读取。</div>
      </div>
      <div class="actions">
        <a class="architecture-link" href="submit"><svg viewBox="0 0 24 24"><path d="m5 12 14-7-4 14-3-6-7-1Z"/><path d="M12 13 19 5"/></svg>提交论文</a>
        <a class="architecture-link" href="architecture"><svg viewBox="0 0 24 24"><path d="M4 6h6v6H4zM14 3h6v6h-6zM14 15h6v6h-6zM10 9l4-3M10 11l4 6"/></svg>Pipeline 架构</a>
        <a class="architecture-link" href="mail"><svg viewBox="0 0 24 24"><path d="M4 5h16v14H4z"/><path d="m4 7 8 6 8-6"/></svg>邮件机器人</a>
        <button id="admin-auth-button" onclick="toggleAdminSession()">管理员登录</button>
        <button class="primary" onclick="refreshAll()">刷新</button>
      </div>
    </header>

    <div class="filters">
      <div class="filter-field"><label for="filter-days">时间范围</label><select id="filter-days"><option value="1">最近 1 天</option><option value="3" selected>最近 3 天</option><option value="7">最近 7 天</option><option value="30">最近 30 天</option><option value="0">全部记录</option></select></div>
      <div class="filter-field user"><label for="filter-user">用户</label><select id="filter-user"><option value="">全部用户</option></select></div>
    </div>

    <nav class="tabs">
      <button class="tab active" data-tab="overview">概览</button>
      <button class="tab" data-tab="usage">使用</button>
      <button class="tab" data-tab="jobs">任务</button>
      <button class="tab" data-tab="logs">日志</button>
      <select class="extra-tab" id="extra-tab" aria-label="更多视图">
        <option value="">更多视图</option>
        <option value="feedback">反馈</option>
        <option value="review">AI Review</option>
      </select>
    </nav>

    <section id="overview" class="panel"></section>
    <section id="usage" class="panel hidden"></section>
    <section id="feedback" class="panel hidden"></section>
    <section id="jobs" class="panel hidden"></section>
    <section id="logs" class="panel hidden"></section>
    <section id="review" class="panel hidden"></section>
  </div>
  <dialog id="admin-login-dialog">
    <form class="login-form" onsubmit="loginAdmin(event)">
      <h2>管理员登录</h2>
      <input id="admin-username" type="email" autocomplete="username" placeholder="管理员账号" required />
      <input id="admin-password" type="password" autocomplete="current-password" placeholder="管理员密码" required />
      <div id="admin-login-error" class="login-error"></div>
      <div class="login-actions"><button type="button" onclick="closeAdminDialog()">取消</button><button class="primary" type="submit">登录</button></div>
    </form>
  </dialog>
<script>
const state = { tab: 'overview', days: '3', senderId: '', adminAuthenticated: false };
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const basePath = window.location.pathname.startsWith('/maxread') ? '/maxread' : '';
const api = async (url, opts={}) => {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(`${basePath}${url}`, {headers: {'content-type': 'application/json'}, credentials: 'same-origin', signal: controller.signal, ...opts});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(payload.error || `HTTP ${response.status}`); error.status = response.status; throw error; }
    return payload;
  } finally {
    window.clearTimeout(timeout);
  }
};
const pill = (v) => `<span class="pill ${esc(v)}">${esc(v || 'unknown')}</span>`;
const link = (url) => url ? `<a href="${esc(url)}" target="_blank">打开</a>` : '<span class="muted">-</span>';
const filteredUrl = (path, extra={}) => { const q = new URLSearchParams({days: state.days, ...extra}); if (state.senderId) q.set('sender_id', state.senderId); return `${path}?${q}`; };
const adminNextPage = new URLSearchParams(window.location.search).get('next') === 'mail' ? 'mail' : '';

$('filter-days').addEventListener('change', async (event) => { state.days = event.target.value; await loadUsers(); refreshAll(); });
$('filter-user').addEventListener('change', (event) => { state.senderId = event.target.value; refreshAll(); });

async function loadUsers() {
  const selected = state.senderId;
  try {
    const rows = await api(`/api/users?days=${encodeURIComponent(state.days)}`);
    $('filter-user').innerHTML = '<option value="">全部用户</option>' + rows.map(r => `<option value="${esc(r.sender_id)}">${esc(r.sender_name || r.sender_id)} · ${esc(r.event_count)}</option>`).join('');
    if (rows.some(r => r.sender_id === selected)) { $('filter-user').value = selected; } else { state.senderId = ''; }
  } catch (error) {
    $('filter-user').innerHTML = '<option value="">用户列表暂不可用</option>';
    console.warn('user filter unavailable', error);
  }
}

async function loadAdminStatus() {
  try {
    const result = await api('/api/admin/status');
    state.adminAuthenticated = Boolean(result.authenticated);
    if (state.adminAuthenticated && adminNextPage) { window.location.replace(adminNextPage); return; }
  } catch (_error) {
    state.adminAuthenticated = false;
  }
  updateAdminButton();
}
function updateAdminButton() { $('admin-auth-button').textContent = state.adminAuthenticated ? '退出管理员' : '管理员登录'; }
function toggleAdminSession() {
  if (state.adminAuthenticated) { logoutAdmin(); return; }
  $('admin-login-error').textContent = '';
  $('admin-username').value = '';
  $('admin-password').value = '';
  $('admin-login-dialog').showModal();
  window.setTimeout(() => $('admin-username').focus(), 0);
}
function closeAdminDialog() { $('admin-login-dialog').close(); }
async function loginAdmin(event) {
  event.preventDefault();
  $('admin-login-error').textContent = '';
  try {
    const result = await api('/api/admin/login', {method:'POST', body:JSON.stringify({username:$('admin-username').value,password:$('admin-password').value})});
    state.adminAuthenticated = Boolean(result.authenticated);
    closeAdminDialog();
    updateAdminButton();
    if (state.adminAuthenticated && adminNextPage) { window.location.replace(adminNextPage); return; }
    refreshAll();
  } catch (error) {
    $('admin-login-error').textContent = error.message || '登录失败';
  }
}
async function logoutAdmin() {
  await api('/api/admin/logout', {method:'POST', body:'{}'}).catch(() => null);
  state.adminAuthenticated = false;
  updateAdminButton();
  refreshAll();
}
function adminExpired(error) {
  if (error && error.status === 401) {
    state.adminAuthenticated = false;
    updateAdminButton();
    alert('管理员会话已失效，请重新登录。');
    refreshAll();
    return true;
  }
  return false;
}

document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  $('extra-tab').value = '';
  document.querySelectorAll('.panel').forEach(x => x.classList.add('hidden'));
  state.tab = btn.dataset.tab;
  $(state.tab).classList.remove('hidden');
  refreshAll();
}));
$('extra-tab').addEventListener('change', (event) => {
  const tab = event.target.value;
  if (!tab) return;
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.add('hidden'));
  state.tab = tab;
  $(state.tab).classList.remove('hidden');
  refreshAll();
});

async function refreshAll() {
  const panel = $(state.tab);
  if (!panel) return;
  panel.innerHTML = '<div class="card empty">正在加载...</div>';
  try {
    if (state.tab === 'overview') return await renderOverview();
    if (state.tab === 'usage') return await renderUsage();
    if (state.tab === 'feedback') return await renderFeedback();
    if (state.tab === 'jobs') return await renderJobs();
    if (state.tab === 'logs') return await renderLogs();
    if (state.tab === 'review') return await renderReview();
  } catch (error) {
    console.error('admin data load failed', error);
    panel.innerHTML = `<div class="card empty"><strong>数据加载失败</strong><br><span class="muted">请点击“刷新”重试。</span></div>`;
  }
}

async function renderOverview() {
  const [s, service] = await Promise.all([api('/api/summary'), api('/api/service-status')]);
  $('overview').innerHTML = `<div class="grid">
    ${serviceCard(service)}
    ${metric('完成文档', s.docs_done || 0)}
    ${metric('活跃用户', s.active_users || 0)}
    ${metric('新反馈', (s.feedback && s.feedback.new) || 0)}
    ${metric('AI Review 问题', s.review_issues || 0)}
    <div class="card wide"><div class="toolbar"><strong>队列状态</strong><span class="muted">queued / running / failed / done</span></div>${kv(s.jobs || {})}</div>
  </div>`;
}
function serviceCard(s) {
  const active = s.mode !== 'operational';
  const label = {operational:'正常', degraded:'降级', maintenance:'维护中', outage:'故障'}[s.mode] || s.mode;
  const readonly = `<div class="status-readonly"><div>模式<br><strong>${esc(label)}</strong></div><div>原因<br><strong>${esc(s.reason || '无')}</strong></div><div>预计恢复<br><strong>${esc(s.expected_recovery_at || '未设置')}</strong></div></div>`;
  const editor = `<div class="status-grid">
      <select id="service-mode">
        ${['operational','degraded','maintenance','outage'].map(x => `<option value="${x}" ${x===s.mode?'selected':''}>${({operational:'正常',degraded:'降级',maintenance:'维护中',outage:'故障'})[x]}</option>`).join('')}
      </select>
      <textarea id="service-reason" placeholder="故障或维护原因">${esc(s.reason || '')}</textarea>
      <div><input id="service-eta" value="${esc(s.expected_recovery_at || '')}" placeholder="预计恢复时间"><button class="primary" style="margin-top:8px;width:100%" onclick="saveServiceStatus()">保存服务状态</button></div>
    </div>`;
  return `<div class="card wide status-card ${esc(s.mode)}">
    <div class="status-title"><strong>服务状态：${esc(label)}</strong><span class="muted">${state.adminAuthenticated ? '管理员模式 · ' : '只读 · '}${s.updated_at ? `最后更新 ${esc(s.updated_at)}` : ''}</span></div>
    ${state.adminAuthenticated ? editor : readonly}
    ${active ? `<div class="muted" style="margin-top:10px">新请求会正常入队，但不启动 worker；系统会回复原因和预计恢复时间，成功交付后再通知。</div>` : ''}
  </div>`;
}
async function saveServiceStatus() {
  const mode = $('service-mode').value;
  const reason = $('service-reason').value;
  const expected_recovery_at = $('service-eta').value;
  try {
    const result = await api('/api/service-status', {method:'POST', body: JSON.stringify({mode, reason, expected_recovery_at, updated_by:'admin'})});
    if (!result.ok) { alert(result.error || '保存失败'); return; }
    renderOverview();
  } catch (error) { if (!adminExpired(error)) alert(error.message || '保存失败'); }
}
function metric(label, value) { return `<div class="card metric"><div class="label">${label}</div><div class="value">${value}</div></div>`; }
function kv(obj) { const keys = Object.keys(obj); if (!keys.length) return '<div class="empty">暂无数据</div>'; return keys.map(k => `<div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--soft);padding:8px 0"><span>${esc(k)}</span><b>${esc(obj[k])}</b></div>`).join(''); }

async function renderUsage() {
  $('usage').innerHTML = '<div class="card empty">正在加载使用记录...</div>';
  try {
    const rows = await api(filteredUrl('/api/usage', {limit: 160}));
    $('usage').innerHTML = table(['时间','用户','来源','状态','标题','文档'], rows.map(r => [r.created_at, userCell(r), `${r.source_kind}<br><span class="mono">${esc(r.source_id || r.source_url)}</span>`, pill(r.status), esc(r.title || r.error || ''), link(r.doc_url)]));
  } catch (error) {
    $('usage').innerHTML = '<div class="card empty">使用记录加载失败，请稍后刷新。</div>';
  }
}

async function renderFeedback() {
  const rows = await api(filteredUrl('/api/feedback', {limit: 160}));
  $('feedback').innerHTML = table(['时间','用户','识别','状态','内容','操作'], rows.map(r => [r.created_at, userCell(r), feedbackOrigin(r), pill(r.status), esc(r.content), feedbackActions(r)]));
}
function feedbackOrigin(r) {
  if (r.feedback_source === 'ai') return `AI · ${esc(r.feedback_category || 'other')}<br><span class="mono muted">${Number(r.feedback_confidence || 0).toFixed(2)}</span>`;
  if (r.feedback_source === 'rule') return '规则';
  return '<span class="muted">历史记录</span>';
}
function feedbackActions(r) {
  if (!state.adminAuthenticated) return pill(r.status);
  return `<select onchange="setFeedback(${r.id}, this.value)">
    ${['new','triaged','planned','done','ignored'].map(s => `<option value="${s}" ${s===r.status?'selected':''}>${s}</option>`).join('')}
  </select>`;
}
async function setFeedback(id, status) { try { await api(`/api/feedback/${id}/status`, {method:'POST', body: JSON.stringify({status})}); renderFeedback(); renderOverview(); } catch (error) { if (!adminExpired(error)) alert(error.message || '更新失败'); } }

async function renderJobs() {
  const rows = await api(filteredUrl('/api/jobs', {limit: 160}));
  $('jobs').innerHTML = table(['ID','用户','论文 / 来源','状态','工作流','阶段','尝试','文档','操作'], rows.map(r => [small(r.id), userCell(r), jobSubject(r), jobPill(r.status), jobFlow(r), jobStage(r), small(r.attempts), link(r.doc_url), jobActions(r)]), 'jobs');
}
const jobStatusLabels = {queued: '排队中', running: '处理中', done: '完成', failed: '失败'};
const workflowLabels = {queued: '等待调度', claimed: '已认领', preparing: '材料准备', generating: '生成中', reviewing: '内容审阅', quality_checking: '格式质检', quality_repairing: '格式修复', publishing: '写入飞书', post_publish_checking: '发布后检查', visual_checking: '视觉检查', visual_repairing: '视觉修复', completed: '已完成', failed: '执行失败', quality_failed: '质量未通过'};
const stageLabels = {queued: '排队', claimed: '已认领', downloading: '下载原文', reading: '生成中', reviewing: '审阅中', writing: '写入飞书', publishing: '发布', done: '完成', failed: '失败', cancelled: '已取消'};
function jobPill(v) { return `<span class="pill ${esc(v)}">${esc(jobStatusLabels[v] || v || '未知')}</span>`; }
function jobTitle(r) {
  return r.resolved_title || r.title || (r.source_kind === 'paper' ? `arXiv ${r.source_id}` : r.source_id || '未命名任务');
}
function jobSubject(r) {
  const kind = r.source_kind === 'paper' ? '论文' : (r.source_kind === 'article' ? '文章' : (r.source_kind || '任务'));
  const titleKnown = Boolean(r.resolved_title || r.title);
  const pending = titleKnown ? '' : '<span>标题尚未取得</span>';
  const source = r.source_url ? `<a class="job-source-link" href="${esc(r.source_url)}" target="_blank" rel="noopener">原文</a>` : '';
  const failure = r.status === 'failed' && r.error ? `<details class="job-error"><summary>查看错误详情</summary><div class="job-error-body">${esc(r.error)}</div></details>` : '';
  const recoveryText = r.status !== 'failed' ? (r.recovery_reason || r.error || '') : '';
  const recovery = recoveryText ? `<details class="job-recovery"><summary>查看恢复记录</summary><div class="job-error-body">${esc(recoveryText)}</div></details>` : '';
  return `<div class="job-subject"><strong>${esc(jobTitle(r))}</strong><div class="job-subject-meta"><span>${esc(kind)}</span><span class="mono">${esc(r.source_id || '')}</span>${pending}${source}</div>${failure}${recovery}</div>`;
}
function jobFlow(r) {
  const label = workflowLabels[r.workflow_state] || r.workflow_state || '-';
  return `<div class="job-flow">${esc(label)}<span class="mono">${esc(r.workflow_state || '')}</span></div>`;
}
function jobStage(r) {
  const label = stageLabels[r.stage] || r.stage || '-';
  return `<div class="job-stage">${esc(label)}<span class="mono">${esc(r.stage || '')}</span></div>`;
}
function jobActions(r) {
  if (!state.adminAuthenticated) return '<span class="muted">-</span>';
  if (r.status === 'failed') return `<button onclick="retryJob(${r.id})">重试</button>`;
  if (r.status === 'running') return '<button disabled title="worker 心跳正常；任务失败或租约失效后方可重试">运行中</button>';
  if (r.status === 'queued') return '<button disabled title="任务已在队列中，无需重复提交">排队中</button>';
  return '<span class="muted">-</span>';
}
async function retryJob(id) { try { await api(`/api/jobs/${id}/retry`, {method:'POST', body:'{}'}); renderJobs(); renderOverview(); } catch (error) { if (!adminExpired(error)) alert(error.message || '重试失败'); } }

async function renderLogs() {
  const rows = await api(filteredUrl('/api/job-events', {limit: 240}));
  $('logs').innerHTML = table(['时间','用户','任务','事件','详情'], rows.map(r => [esc(r.created_at), userCell(r), `${small(r.job_id)}<br><span class="mono muted">${esc(r.source_id || 'system')}</span>`, pill(r.event_type), `<span class="mono">${esc(r.detail)}</span>`]));
}

async function renderReview() {
  const rows = await api(filteredUrl('/api/review-issues', {limit: 160}));
  $('review').innerHTML = table(['时间','来源','类别','严重度','详情'], rows.map(r => [r.created_at, `${r.source_kind}<br><span class="mono">${esc(r.source_id)}</span>`, esc(r.category), pill(r.severity), esc(r.detail)]));
}

function table(headers, rows, className='') {
  if (!rows.length) return '<div class="card empty">暂无数据</div>';
  const cardClass = className ? ` ${className}-card` : '';
  const tableClass = className ? ` class="${esc(className)}-table"` : '';
  return `<div class="card wide${cardClass}"><table${tableClass}><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function userCell(r) { const name = r.sender_name || '飞书用户'; const id = r.sender_id || ''; return `<strong>${esc(name)}</strong><br><span class="mono muted">${esc(id)}</span>`; }
function small(v) { return `<span class="mono">${esc(v)}</span>`; }
Promise.all([loadUsers(), loadAdminStatus()]).finally(refreshAll);
</script>
</body>
</html>
'''
