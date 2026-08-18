from __future__ import annotations

import json
import re
import socket
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .db import Store
from .feedback import count_feedback_by_status, visible_feedback_rows
from .review import visible_review_issues


DEFAULT_LIMIT = 80
CONTACT_LOOKUP_TIMEOUT_SECONDS = 5


class AdminServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, settings: Settings):
        super().__init__(server_address, handler_class)
        self.settings = settings


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
            self._html(INDEX_HTML)
            return
        if parsed.path == "/api/summary":
            self._json_response(self._with_store(_admin_summary))
            return
        if parsed.path == "/api/usage":
            limit = _limit(parsed.query)
            self._json_response(self._with_store(lambda store: _attach_user_names(self.server.settings, store.list_usage_events(limit), store)))
            return
        if parsed.path == "/api/feedback":
            query = parse_qs(parsed.query)
            limit = _limit(parsed.query)
            status = query.get("status", [""])[0]
            self._json_response(self._with_store(lambda store: _attach_user_names(self.server.settings, visible_feedback_rows(store.list_feedback(limit, status)), store)))
            return
        if parsed.path == "/api/jobs":
            query = parse_qs(parsed.query)
            limit = _limit(parsed.query)
            status = query.get("status", [""])[0]
            self._json_response(self._with_store(lambda store: store.list_queue_jobs(limit, status)))
            return
        if parsed.path == "/api/job-events":
            query = parse_qs(parsed.query)
            limit = _limit(parsed.query)
            job_id = int(query.get("job_id", ["0"])[0] or 0)
            self._json_response(self._with_store(lambda store: store.list_job_events(job_id, limit)))
            return
        if parsed.path == "/api/review-issues":
            query = parse_qs(parsed.query)
            limit = _limit(parsed.query)
            source_kind = query.get("source_kind", [""])[0]
            source_id = query.get("source_id", [""])[0]
            self._json_response(self._with_store(lambda store: visible_review_issues(store.list_review_issues(limit, source_kind, source_id))))
            return
        if parsed.path == "/api/review-stats":
            self._json_response(self._with_store(lambda store: store.review_issue_stats()))
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
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
            ok = self._with_store(lambda store: store.retry_queue_job(job_id))
            self._json_response({"ok": ok, "job_id": job_id})
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _with_store(self, fn):
        store = Store(self.server.settings.db_path)
        try:
            return fn(store)
        finally:
            store.close()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def _html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self._write_body(data)

    def _json_response(self, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
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


def _attach_user_names(settings: Settings, rows, store=None):
    sender_ids = sorted({row.get("sender_id", "") for row in rows if row.get("sender_id", "")})
    if not sender_ids:
        return rows
    names = store.get_user_names(sender_ids) if store else {}
    unresolved_ids = [sender_id for sender_id in sender_ids if sender_id not in names]
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
    for row in rows:
        row["sender_name"] = names.get(row.get("sender_id", ""), "")
    return rows

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
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", sans-serif; color: var(--text); background: var(--bg); font-size: 14px; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 48px; }
    header { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 20px; }
    h1 { margin: 0; font-size: 30px; line-height: 1.15; letter-spacing: 0; }
    .sub { margin-top: 8px; color: var(--muted); }
    .actions { display: flex; gap: 10px; align-items: center; }
    button, select { border: 1px solid var(--line); background: #fff; color: var(--text); border-radius: 8px; padding: 8px 11px; font: inherit; }
    button { cursor: pointer; transition: transform .15s, box-shadow .15s; }
    button:hover { transform: translateY(-1px); box-shadow: 0 6px 16px -12px rgba(0,0,0,.25); }
    .primary { background: var(--primary); border-color: var(--primary); color: #fff; }
    .tabs { display: flex; gap: 8px; border-bottom: 1px solid var(--line); margin-bottom: 18px; overflow-x: auto; }
    .tab { border: 0; background: transparent; border-radius: 0; padding: 12px 8px 11px; color: var(--muted); box-shadow: none; white-space: nowrap; }
    .tab.active { color: var(--primary); border-bottom: 2px solid var(--primary); }
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
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 3px 8px; font-size: 12px; background: var(--soft); color: var(--muted); }
    .pill.done { color: var(--ok); background: rgba(20,122,84,.08); }
    .pill.failed { color: var(--bad); background: rgba(180,35,24,.08); }
    .pill.running, .pill.queued { color: var(--warn); background: rgba(184,110,0,.1); }
    .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }
    .muted { color: var(--muted); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .hidden { display: none; }
    .empty { padding: 26px; color: var(--muted); text-align: center; }
    a { color: var(--primary); text-decoration: none; }
    a:hover { text-decoration: underline; }
    @media (max-width: 860px) { .metric { grid-column: span 6; } header { align-items: flex-start; flex-direction: column; } }
    @media (max-width: 620px) { .metric { grid-column: span 12; } .wrap { padding: 20px 14px 36px; } table { font-size: 13px; } }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>MaxRead 控制台</h1>
        <div class="sub">查看使用记录、反馈、队列和 AI review 问题。仅监听 5090 ZeroTier 内网地址。</div>
      </div>
      <div class="actions"><button class="primary" onclick="refreshAll()">刷新</button></div>
    </header>

    <nav class="tabs">
      <button class="tab active" data-tab="overview">概览</button>
      <button class="tab" data-tab="usage">使用记录</button>
      <button class="tab" data-tab="feedback">反馈</button>
      <button class="tab" data-tab="jobs">任务队列</button>
      <button class="tab" data-tab="review">AI Review</button>
    </nav>

    <section id="overview" class="panel"></section>
    <section id="usage" class="panel hidden"></section>
    <section id="feedback" class="panel hidden"></section>
    <section id="jobs" class="panel hidden"></section>
    <section id="review" class="panel hidden"></section>
  </div>
<script>
const state = { tab: 'overview' };
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = (url, opts={}) => fetch(url, {headers: {'content-type': 'application/json'}, ...opts}).then(r => r.json());
const pill = (v) => `<span class="pill ${esc(v)}">${esc(v || 'unknown')}</span>`;
const link = (url) => url ? `<a href="${esc(url)}" target="_blank">打开</a>` : '<span class="muted">-</span>';

document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.panel').forEach(x => x.classList.add('hidden'));
  state.tab = btn.dataset.tab;
  $(state.tab).classList.remove('hidden');
  refreshAll();
}));

async function refreshAll() {
  if (state.tab === 'overview') return renderOverview();
  if (state.tab === 'usage') return renderUsage();
  if (state.tab === 'feedback') return renderFeedback();
  if (state.tab === 'jobs') return renderJobs();
  if (state.tab === 'review') return renderReview();
}

async function renderOverview() {
  const s = await api('/api/summary');
  $('overview').innerHTML = `<div class="grid">
    ${metric('完成文档', s.docs_done || 0)}
    ${metric('活跃用户', s.active_users || 0)}
    ${metric('新反馈', (s.feedback && s.feedback.new) || 0)}
    ${metric('AI Review 问题', s.review_issues || 0)}
    <div class="card wide"><div class="toolbar"><strong>队列状态</strong><span class="muted">queued / running / failed / done</span></div>${kv(s.jobs || {})}</div>
    <div class="card wide"><div class="toolbar"><strong>使用状态</strong><span class="muted">usage_events.status</span></div>${kv(s.usage || {})}</div>
  </div>`;
}
function metric(label, value) { return `<div class="card metric"><div class="label">${label}</div><div class="value">${value}</div></div>`; }
function kv(obj) { const keys = Object.keys(obj); if (!keys.length) return '<div class="empty">暂无数据</div>'; return keys.map(k => `<div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--soft);padding:8px 0"><span>${esc(k)}</span><b>${esc(obj[k])}</b></div>`).join(''); }

async function renderUsage() {
  $('usage').innerHTML = '<div class="card empty">正在加载使用记录...</div>';
  try {
    const rows = await api('/api/usage?limit=120');
    $('usage').innerHTML = table(['时间','用户','来源','状态','标题','文档'], rows.map(r => [r.created_at, userCell(r), `${r.source_kind}<br><span class="mono">${esc(r.source_id || r.source_url)}</span>`, pill(r.status), esc(r.title || r.error || ''), link(r.doc_url)]));
  } catch (error) {
    $('usage').innerHTML = '<div class="card empty">使用记录加载失败，请稍后刷新。</div>';
  }
}

async function renderFeedback() {
  const rows = await api('/api/feedback?limit=160');
  $('feedback').innerHTML = table(['时间','用户','识别','状态','内容','操作'], rows.map(r => [r.created_at, userCell(r), feedbackOrigin(r), pill(r.status), esc(r.content), feedbackActions(r)]));
}
function feedbackOrigin(r) {
  if (r.feedback_source === 'ai') return `AI · ${esc(r.feedback_category || 'other')}<br><span class="mono muted">${Number(r.feedback_confidence || 0).toFixed(2)}</span>`;
  if (r.feedback_source === 'rule') return '规则';
  return '<span class="muted">历史记录</span>';
}
function feedbackActions(r) {
  return `<select onchange="setFeedback(${r.id}, this.value)">
    ${['new','triaged','planned','done','ignored'].map(s => `<option value="${s}" ${s===r.status?'selected':''}>${s}</option>`).join('')}
  </select>`;
}
async function setFeedback(id, status) { await api(`/api/feedback/${id}/status`, {method:'POST', body: JSON.stringify({status})}); renderFeedback(); renderOverview(); }

async function renderJobs() {
  const rows = await api('/api/jobs?limit=120');
  $('jobs').innerHTML = table(['ID','来源','状态','阶段','尝试','标题 / 错误','文档','操作'], rows.map(r => [r.id, `${r.source_kind}<br><span class="mono">${esc(r.source_id)}</span>`, pill(r.status), esc(r.stage || ''), r.attempts, esc(r.title || r.error || ''), link(r.doc_url), jobActions(r)]));
}
function jobActions(r) { return (r.status === 'failed' || r.status === 'running') ? `<button onclick="retryJob(${r.id})">重试</button>` : '<span class="muted">-</span>'; }
async function retryJob(id) { await api(`/api/jobs/${id}/retry`, {method:'POST', body:'{}'}); renderJobs(); renderOverview(); }

async function renderReview() {
  const rows = await api('/api/review-issues?limit=160');
  $('review').innerHTML = table(['时间','来源','类别','严重度','详情'], rows.map(r => [r.created_at, `${r.source_kind}<br><span class="mono">${esc(r.source_id)}</span>`, esc(r.category), pill(r.severity), esc(r.detail)]));
}

function table(headers, rows) {
  if (!rows.length) return '<div class="card empty">暂无数据</div>';
  return `<div class="card wide"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function userCell(r) { const name = r.sender_name || '未解析用户'; const id = r.sender_id || ''; return `<strong>${esc(name)}</strong><br><span class="mono muted">${esc(id)}</span>`; }
function small(v) { return `<span class="mono">${esc(v)}</span>`; }
refreshAll();
</script>
</body>
</html>
'''
