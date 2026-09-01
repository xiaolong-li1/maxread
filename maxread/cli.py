from __future__ import annotations

import argparse
import json
import re
import sys
from hashlib import sha256
from pathlib import Path
from typing import Iterable, List, Tuple

from .admin_server import run_admin_server
from .arxiv import ArxivClient, extract_arxiv_refs
from .article_pipeline import ArticlePipeline
from .cache_cleanup import cleanup_completed_cache, local_date_cutoff_utc
from .config import Settings
from .db import Store
from .feedback import classify_feedback_text, is_feedback_text as _is_feedback_text
from .feishu import FeishuClient, parse_event
from .help import group_intro_message, intro_message, plain_message_text, should_send_intro
from .job_queue import QueueManager, _queue_eta_text, enqueue_event_items, run_worker_forever
from .openai_client import OpenAIClient
from .pipeline import MaxReadPipeline
from .remote_worker import run_remote_paper_worker
from .models import PaperRef
from .retry_policy import retry_requires_rebuild
from .sources import WebRef, extract_supported_inputs, is_supported_web_article_url
from .web_article import WebArticleClient
from .visual_qa import VisualQAController
from .duty import run_duty_command
from .web_submit import claim_binding_code


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maxread")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_extract = sub.add_parser("extract", help="Extract arXiv IDs from text")
    p_extract.add_argument("text", nargs="+")

    p_process = sub.add_parser("process", help="Process one message worth of arXiv IDs")
    p_process.add_argument("text", nargs="+")
    p_process.add_argument("--no-openai", action="store_true", help="Use fallback document instead of OpenAI")

    p_import = sub.add_parser("import-source", help="Import an arXiv source package downloaded manually")
    p_import.add_argument("paper_id")
    p_import.add_argument("source_path")

    p_listen = sub.add_parser("listen", help="Listen to Feishu IM events and enqueue supported messages")
    p_listen.add_argument("--no-openai", action="store_true", help="Use fallback document instead of OpenAI")

    p_worker = sub.add_parser("worker", help="Run queue workers without listening to Feishu events")
    p_worker.add_argument("--no-openai", action="store_true", help="Use fallback document instead of OpenAI")
    sub.add_parser("paper-worker", help="Run a remote paper worker against the Aliyun coordinator")

    p_event = sub.add_parser("handle-event", help="Handle one Feishu event JSON from stdin")
    p_event.add_argument("--no-openai", action="store_true")

    p_usage = sub.add_parser("usage", help="List recent MaxRead usage events")
    p_usage.add_argument("--limit", type=int, default=50)
    p_usage.add_argument("--resolve-users", action="store_true", help="Resolve sender open_id to names with contact API")

    p_feedback = sub.add_parser("feedback", help="List recent private-chat feedback")
    p_feedback.add_argument("--limit", type=int, default=50)
    p_feedback.add_argument("--status", default="", help="Filter feedback by status, default all")
    p_feedback.add_argument("--resolve-users", action="store_true", help="Resolve sender open_id to names with contact API")

    p_jobs = sub.add_parser("jobs", help="List global queue jobs")
    p_jobs.add_argument("--limit", type=int, default=50)
    p_jobs.add_argument("--status", default="", help="Filter by queued/running/done/failed")

    p_retry = sub.add_parser("retry-job", help="Retry a failed or stuck queue job")
    p_retry.add_argument("job_id", type=int)

    p_job_events = sub.add_parser("job-events", help="List queue job lifecycle events")
    p_job_events.add_argument("--job-id", type=int, default=0)
    p_job_events.add_argument("--limit", type=int, default=100)

    p_review = sub.add_parser("review-issues", help="List AI review issues")
    p_review.add_argument("--limit", type=int, default=50)
    p_review.add_argument("--source-kind", default="", help="Filter by paper/article")
    p_review.add_argument("--source-id", default="", help="Filter by arXiv id or article id")

    sub.add_parser("review-stats", help="Show AI review issue stats")

    sub.add_parser("job-stats", help="Show queue status counts")

    p_cleanup = sub.add_parser("cache-cleanup", help="Delete rebuildable source/render caches for completed work")
    p_cleanup.add_argument("--older-than-hours", type=float, default=1.0)
    p_cleanup.add_argument("--dry-run", action="store_true")

    p_invalidate = sub.add_parser("invalidate-cache", help="Mark completed documents before a local date as legacy")
    p_invalidate.add_argument("--before", required=True, help="Local date YYYY-MM-DD; records before 00:00 become legacy")
    p_invalidate.add_argument("--timezone", default="Asia/Shanghai")

    p_admin = sub.add_parser("admin", help="Run local MaxRead admin web UI")
    p_admin.add_argument("--host", default="127.0.0.1", help="Bind host, default localhost only")
    p_admin.add_argument("--port", type=int, default=8765, help="Bind port")

    p_duty = sub.add_parser("duty", help="Manage independent daily duty reminders")
    duty_sub = p_duty.add_subparsers(dest="action", required=True)
    p_duty_set = duty_sub.add_parser("set", help="Replace the duty roster")
    p_duty_set.add_argument("--member", action="append", required=True, help="Name=ou_xxx or ou_xxx; repeatable")
    duty_sub.add_parser("list", help="List the duty roster")
    duty_sub.add_parser("today", help="Show today's duty member")
    p_duty_history = duty_sub.add_parser("history", help="List reminder send history")
    p_duty_history.add_argument("--limit", type=int, default=30)
    p_duty_send = duty_sub.add_parser("send", help="Send or preview a reminder")
    p_duty_send.add_argument("--date", default="", help="Target date YYYY-MM-DD")
    p_duty_send.add_argument("--dry-run", action="store_true")
    duty_sub.add_parser("daemon", help="Run the independent reminder daemon")

    args = parser.parse_args(argv)

    if args.cmd == "extract":
        refs, _web_refs = extract_supported_inputs(" ".join(args.text))
        print(json.dumps([r.paper_id for r in refs], ensure_ascii=False))
        return 0

    settings = Settings.load(Path.cwd())
    # The admin UI is an independent read-only surface. Start it before
    # constructing model, source, and visual-QA clients so a slow dependency
    # or work-directory filesystem cannot leave systemd reporting an active
    # process with no listening port.
    if args.cmd == "admin":
        run_admin_server(settings, host=args.host, port=args.port)
        return 0
    if args.cmd == "paper-worker":
        settings.workdir.mkdir(parents=True, exist_ok=True)
        run_remote_paper_worker(settings)
        return 0

    settings.workdir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    if args.cmd == "duty":
        store.close()
        return run_duty_command(settings, args)
    if args.cmd == "cache-cleanup":
        result = cleanup_completed_cache(
            store,
            settings.workdir,
            args.older_than_hours,
            dry_run=args.dry_run,
        )
        store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "invalidate-cache":
        cutoff = local_date_cutoff_utc(args.before, args.timezone)
        result = store.mark_cache_legacy_before(cutoff)
        store.close()
        print(json.dumps({"ok": True, "cutoff_utc": cutoff, **result}, ensure_ascii=False, indent=2))
        return 0
    arxiv = ArxivClient(
        settings.workdir,
        timeout=settings.arxiv_timeout,
        parallel_streams=settings.arxiv_parallel_streams,
        parallel_min_bytes=settings.arxiv_parallel_min_bytes,
    )
    web = WebArticleClient(settings.workdir, timeout=settings.arxiv_timeout)
    feishu = FeishuClient(settings.lark_cli, settings.feishu_as)
    llm = None if getattr(args, "no_openai", False) else _maybe_llm(settings)
    feedback_llm = None
    if args.cmd in {"listen", "handle-event"} and not getattr(args, "no_openai", False):
        feedback_llm = _maybe_llm(settings, timeout=min(settings.openai_timeout, 20), reasoning_effort="low")
    visual_qa = VisualQAController.from_settings(settings, llm=llm)
    pipeline = MaxReadPipeline(
        store,
        arxiv,
        feishu,
        llm,
        require_source=settings.require_source,
        review_reasoning_effort=settings.openai_review_reasoning_effort,
        visual_qa=visual_qa,
        generation_repair_rounds=settings.generation_repair_rounds,
        sectional_generation_enabled=settings.sectional_generation_enabled,
        sectional_generation_workers=settings.sectional_generation_workers,
        quality_repair_rounds=settings.quality_repair_rounds,
    )
    article_pipeline = ArticlePipeline(
        store,
        web,
        feishu,
        llm,
        review_reasoning_effort=settings.openai_review_reasoning_effort,
        visual_qa=visual_qa,
        quality_repair_rounds=settings.quality_repair_rounds,
    )

    if args.cmd == "import-source":
        source_path = Path(args.source_path).expanduser()
        imported = arxiv.import_source(args.paper_id, source_path)
        store.upsert_paper(args.paper_id, "source_imported", source_path=str(imported), error="")
        print(json.dumps({"paper_id": args.paper_id, "source_path": str(imported)}, ensure_ascii=False))
        return 0

    if args.cmd == "process":
        refs, web_refs = extract_supported_inputs(" ".join(args.text))
        if not refs and not web_refs:
            print("No arXiv IDs found")
            return 0
        for ref in refs:
            result = pipeline.process_ref(ref)
            print(json.dumps(result.__dict__, ensure_ascii=False))
        for ref in web_refs:
            result = article_pipeline.process_ref(ref)
            print(json.dumps(result.__dict__, ensure_ascii=False))
        return 0

    if args.cmd == "handle-event":
        event = parse_event(json.loads(sys.stdin.read()))
        _handle_event(pipeline, article_pipeline, event, feedback_llm)
        return 0

    if args.cmd == "usage":
        rows = store.list_usage_events(args.limit)
        if args.resolve_users:
            rows = _attach_user_names(settings, rows)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "feedback":
        rows = store.list_feedback(args.limit, args.status)
        if args.resolve_users:
            rows = _attach_user_names(settings, rows)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "jobs":
        print(json.dumps(store.list_queue_jobs(args.limit, args.status), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "job-events":
        print(json.dumps(store.list_job_events(args.job_id, args.limit), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "review-issues":
        print(json.dumps(store.list_review_issues(args.limit, args.source_kind, args.source_id), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "review-stats":
        print(json.dumps(store.review_issue_stats(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "job-stats":
        print(json.dumps(store.queue_stats(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "retry-job":
        ok = store.retry_queue_job(args.job_id)
        print(json.dumps({"ok": ok, "job_id": args.job_id}, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    if args.cmd == "admin":
        store.close()
        return 0

    if args.cmd == "worker":
        store.close()
        run_worker_forever(settings, no_openai=getattr(args, "no_openai", False))
        return 0

    if args.cmd == "listen":
        manager = QueueManager(settings, no_openai=getattr(args, "no_openai", False))
        manager.start_background_workers()
        for event in feishu.event_stream():
            _handle_event(pipeline, article_pipeline, event, feedback_llm)
        return 0

    return 2


def _maybe_llm(settings: Settings, timeout: int | None = None, reasoning_effort: str | None = None) -> OpenAIClient | None:
    if not settings.openai_api_key:
        return None
    return OpenAIClient(
        settings.openai_api_key,
        settings.model,
        timeout=timeout or settings.openai_timeout,
        base_url=settings.openai_base_url,
        sub_module=settings.openai_sub_module,
        reasoning_effort=reasoning_effort or settings.openai_reasoning_effort,
        api_mode=settings.openai_api_mode,
    )


def _handle_event(pipeline: MaxReadPipeline, article_pipeline: ArticlePipeline, event, feedback_llm=None) -> None:
    settings = Settings.load(Path.cwd())
    store = pipeline.store

    if not _should_accept_event(event):
        return

    _capture_event_sender_name(store, article_pipeline.feishu, event)

    if _handle_web_binding_event(settings, store, article_pipeline.feishu, event):
        return

    retry_requested = _is_retry_command(event.content)
    if retry_requested and _handle_retry_event(settings, store, article_pipeline.feishu, event):
        return
    refs, web_refs = _extract_event_supported_inputs(article_pipeline.feishu, event)

    if _is_private_chat(event) and store.should_send_intro_to_user(event.sender_id):
        _reply_intro(article_pipeline.feishu, event, settings.feedback_url)
        store.mark_intro_sent(event.sender_id)

    if not refs and not web_refs:
        if retry_requested:
            _reply_retry_missing(article_pipeline.feishu, event)
            return
        if _is_private_chat(event):
            if _record_feedback(store, article_pipeline.feishu, event, feedback_llm):
                return
            if should_send_intro(event.content):
                _reply_intro(article_pipeline.feishu, event, settings.feedback_url)
        elif getattr(event, "mentioned_bot", False):
            _reply_group_intro(article_pipeline.feishu, event)
        return
    enqueue_event_items(
        settings,
        store,
        article_pipeline.feishu,
        event,
        refs,
        web_refs,
        retry_requested=retry_requested,
    )


def _capture_event_sender_name(store: Store, feishu: FeishuClient, event) -> str:
    """Persist a human name on first contact using event/message evidence."""
    sender_id = str(getattr(event, "sender_id", "") or "").strip()
    if not sender_id or sender_id.startswith("guest:"):
        return ""
    cached = str(store.get_user_names([sender_id]).get(sender_id, "") or "").strip()
    if cached and cached not in {"飞书用户", "未解析用户"}:
        store.update_web_identity_display_name(sender_id, cached)
        return cached

    name = _event_sender_name(getattr(event, "raw", {}) or {}, sender_id)
    if not name:
        try:
            name = feishu.message_sender_name(
                str(getattr(event, "message_id", "") or ""),
                expected_sender_id=sender_id,
            )
        except Exception:
            name = ""
    name = str(name or "").strip()
    if not name or name in {"飞书用户", "未解析用户"}:
        return ""
    store.save_user_names({sender_id: name})
    store.update_web_identity_display_name(sender_id, name)
    return name


def _event_sender_name(payload, expected_sender_id: str) -> str:
    if isinstance(payload, dict):
        candidate_id = str(
            payload.get("open_id")
            or payload.get("sender_id")
            or payload.get("user_id")
            or payload.get("id")
            or ""
        ).strip()
        if candidate_id == expected_sender_id:
            for key in ("sender_name", "localized_name", "display_name", "name", "user_name"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for value in payload.values():
            found = _event_sender_name(value, expected_sender_id)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _event_sender_name(value, expected_sender_id)
            if found:
                return found
    return ""


def _extract_event_supported_inputs(feishu: FeishuClient, event) -> Tuple[List[PaperRef], List[WebRef]]:
    refs, web_refs = extract_supported_inputs(event.content)
    if refs or web_refs:
        return refs, web_refs
    retry_requested = _is_retry_command(event.content)
    if _is_private_chat(event) and not retry_requested:
        return refs, web_refs
    if not getattr(event, "mentioned_bot", False) and not (
        retry_requested and (_is_private_chat(event) or _is_thread_reply(event))
    ):
        return refs, web_refs
    context = feishu.fetch_related_message_text(event)
    if not context:
        return refs, web_refs
    return extract_supported_inputs(event.content + "\n" + context)


def _handle_retry_event(settings: Settings, store: Store, feishu: FeishuClient, event) -> bool:
    """Retry durable topic jobs without parsing bot failure text as input."""
    explicit_papers, explicit_web = extract_supported_inputs(event.content)
    explicit_keys = tuple(
        [store.dedupe_key("paper", ref.paper_id) for ref in explicit_papers]
        + [store.dedupe_key("article", ref.url) for ref in explicit_web]
    )
    related_ids = _retry_related_message_ids(
        getattr(event, "raw", {}) or {},
        exclude={str(getattr(event, "message_id", "") or "")},
    )
    if not related_ids and hasattr(feishu, "fetch_related_message_ids"):
        try:
            # Thread messages are fetched in ascending order; the first one is
            # the root request. Previous retry replies may already contain
            # poisoned historical jobs and must not expand the source set.
            related_ids = tuple(feishu.fetch_related_message_ids(event)[:1])
        except Exception:
            related_ids = ()
    jobs = store.find_retryable_queue_jobs(
        str(getattr(event, "chat_id", "") or ""),
        str(getattr(event, "sender_id", "") or ""),
        message_ids=related_ids,
        dedupe_keys=explicit_keys,
    )
    jobs = [job for job in jobs if _is_valid_retry_source(job)]
    retried = []
    for job in jobs:
        if store.retry_queue_job(
            int(job["id"]),
            reason=f"topic retry requested by {getattr(event, 'sender_id', '')}",
            event_type="topic_retry",
            rebuild_pipeline=_retry_requires_rebuild(job),
        ):
            retried.append(job)
    if retried:
        lines = [f"收到 {len(retried)} 篇，已重新加入全局队列。"]
        for job in retried:
            position = store.queue_position(int(job["id"]))
            duration = store.recent_job_duration_seconds(str(job.get("source_kind") or ""))
            lines.append(
                f"- {job.get('source_id') or job.get('source_url')}："
                f"{_queue_eta_text(position, settings.queue_workers, duration)}"
            )
        _reply_retry_result(feishu, event, "\n".join(lines))
        return True
    if explicit_papers or explicit_web:
        # No owned failed row exists for this explicit source. Treat it as a
        # fresh request, while still using the retry wording in the receipt.
        enqueue_event_items(
            settings,
            store,
            feishu,
            event,
            explicit_papers,
            explicit_web,
            retry_requested=True,
        )
        return True
    return False


def _retry_requires_rebuild(job: dict) -> bool:
    return retry_requires_rebuild(job)


def _is_valid_retry_source(job: dict) -> bool:
    kind = str(job.get("source_kind") or "")
    if kind == "paper":
        return bool(str(job.get("source_id") or "").strip())
    if kind == "article":
        return is_supported_web_article_url(str(job.get("source_url") or job.get("source_id") or ""))
    return False


def _retry_related_message_ids(payload: dict, exclude: set[str] | None = None) -> tuple[str, ...]:
    excluded = {item for item in (exclude or set()) if item}
    root_keys = {"root_id", "root_message_id", "thread_root_id"}
    parent_keys = {"parent_id", "parent_message_id"}
    roots: list[str] = []
    parents: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                text = str(child or "")
                lowered = str(key).lower()
                target = roots if lowered in root_keys else parents if lowered in parent_keys else None
                if target is not None and text.startswith("om_") and text not in excluded and text not in target:
                    target.append(text)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return tuple(roots or parents)


def _reply_retry_result(feishu: FeishuClient, event, text: str) -> None:
    key = sha256(f"retry-result:{event.event_id}:{event.message_id}".encode("utf-8")).hexdigest()[:32]
    try:
        feishu.reply_text(event.message_id, text, idempotency_key=key)
    except Exception:
        pass


def _should_accept_event(event) -> bool:
    if _is_private_chat(event):
        return True
    if getattr(event, "mentioned_bot", False):
        return True
    return _is_retry_command(getattr(event, "content", "")) and _is_thread_reply(event)


def _is_private_chat(event) -> bool:
    return str(getattr(event, "chat_type", "")).lower() in {"p2p", "private"}


def _handle_web_binding_event(settings: Settings, store: Store, feishu: FeishuClient, event) -> bool:
    if not _is_private_chat(event):
        return False
    text = plain_message_text(str(getattr(event, "content", "") or "")).strip()
    match = re.fullmatch(r"绑定\s*[:：]?\s*(\d{6})", text)
    if not match:
        return False
    identity = claim_binding_code(store, match.group(1), str(getattr(event, "sender_id", "") or ""))
    if identity:
        rows = _attach_user_names(settings, [{"sender_id": str(getattr(event, "sender_id", "") or "")}])
        display_name = str(rows[0].get("sender_name") or "").strip()
        if display_name:
            store.save_user_names({str(event.sender_id): display_name})
            store.update_web_identity_display_name(str(event.sender_id), display_name)
    key = sha256(f"web-bind:{event.event_id}:{event.message_id}".encode("utf-8")).hexdigest()[:32]
    message = "网页账号已绑定，之后的网页提交会计入你的飞书账号。" if identity else "绑定码无效或已过期，请回网页重新生成。"
    try:
        feishu.reply_text(event.message_id, message, idempotency_key=key, reply_in_thread=False)
    except Exception:
        pass
    return True


def _is_retry_command(content: str) -> bool:
    text = plain_message_text(str(content or "")).strip()
    text = re.sub(r"@(?:读不动了|maxread|_user_\d+)\s*", "", text, flags=re.I).strip()
    return bool(re.fullmatch(r"(?:请|麻烦)?\s*(?:帮我)?\s*(?:重试|再试一次|重新生成|重新读)(?:\s+.*)?[。.!！]?", text, flags=re.I))


def _is_thread_reply(event) -> bool:
    keys = {"thread_id", "root_id", "parent_id", "root_message_id", "parent_message_id", "thread_root_id"}

    def walk(value) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in keys and str(child or "").startswith(("om_", "omt_")):
                    return True
                if walk(child):
                    return True
        elif isinstance(value, list):
            return any(walk(child) for child in value)
        return False

    return walk(getattr(event, "raw", {}) or {})


def _reply_intro(feishu: FeishuClient, event, feedback_url: str) -> None:
    key = sha256(f"intro:{event.event_id}:{event.message_id}".encode("utf-8")).hexdigest()[:32]
    try:
        feishu.reply_text(event.message_id, intro_message(feedback_url), idempotency_key=key, reply_in_thread=False)
    except Exception:
        pass


def _reply_group_intro(feishu: FeishuClient, event) -> None:
    key = sha256(f"group-intro:{event.event_id}:{event.message_id}".encode("utf-8")).hexdigest()[:32]
    try:
        feishu.reply_text(event.message_id, group_intro_message(), idempotency_key=key)
    except Exception:
        pass


def _reply_no_supported_link(feishu: FeishuClient, event) -> None:
    key = sha256(f"no-link:{event.event_id}:{event.message_id}".encode("utf-8")).hexdigest()[:32]
    try:
        feishu.reply_text(event.message_id, "我在这个话题里没找到 arXiv 或支持的链接。", idempotency_key=key)
    except Exception:
        pass


def _reply_retry_missing(feishu: FeishuClient, event) -> None:
    key = sha256(f"retry-missing:{event.event_id}:{event.message_id}".encode("utf-8")).hexdigest()[:32]
    try:
        feishu.reply_text(
            event.message_id,
            "我在这个话题里没找到可重试的论文或文章。请回复「重试 + 论文 ID」，或重新带上原链接。",
            idempotency_key=key,
        )
    except Exception:
        pass


def _attach_user_names(settings: Settings, rows):
    sender_ids = sorted({row.get("sender_id", "") for row in rows if row.get("sender_id", "")})
    if not sender_ids:
        return rows
    try:
        import subprocess

        result = subprocess.run(
            [settings.lark_cli, "contact", "+search-user", "--as", "user", "--user-ids", ",".join(sender_ids), "--format", "json"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            return rows
        payload = json.loads(result.stdout)
        users = payload.get("data", {}).get("users", [])
        names = {user.get("open_id", ""): user.get("localized_name", "") or user.get("name", "") for user in users}
        for row in rows:
            row["sender_name"] = names.get(row.get("sender_id", ""), "")
    except Exception:
        return rows
    return rows


def _record_feedback(store: Store, feishu: FeishuClient, event, feedback_llm=None) -> bool:
    text = plain_message_text(str(event.content or "")).strip()
    if not text:
        return False
    decision = classify_feedback_text(feedback_llm, text)
    if not decision.is_feedback:
        return False
    feedback_id = store.add_feedback(
        event.event_id,
        event.message_id,
        event.chat_id,
        event.chat_type,
        event.sender_id,
        text,
        source=decision.source,
        category=decision.category,
        confidence=decision.confidence,
    )
    key = sha256(f"feedback:{event.event_id}:{feedback_id}".encode("utf-8")).hexdigest()[:32]
    try:
        feishu.reply_text(event.message_id, f"收到，我先记下这条反馈了（#{feedback_id}）。", idempotency_key=key, reply_in_thread=False)
    except Exception:
        pass
    return True


if __name__ == "__main__":
    raise SystemExit(main())
