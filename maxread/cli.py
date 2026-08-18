from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Iterable, List, Tuple

from .admin_server import run_admin_server
from .arxiv import ArxivClient, extract_arxiv_refs
from .article_pipeline import ArticlePipeline
from .config import Settings
from .db import Store
from .feedback import classify_feedback_text, is_feedback_text as _is_feedback_text
from .feishu import FeishuClient, parse_event
from .help import group_intro_message, intro_message, plain_message_text, should_send_intro
from .job_queue import QueueManager, enqueue_event_items, run_worker_forever
from .openai_client import OpenAIClient
from .pipeline import MaxReadPipeline
from .models import PaperRef
from .sources import WebRef, extract_supported_inputs
from .web_article import WebArticleClient
from .visual_qa import VisualQAController
from .duty import run_duty_command


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
        refs = extract_arxiv_refs(" ".join(args.text))
        print(json.dumps([r.paper_id for r in refs], ensure_ascii=False))
        return 0

    settings = Settings.load(Path.cwd())
    settings.workdir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    if args.cmd == "duty":
        store.close()
        return run_duty_command(settings, args)
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
        feedback_llm = _maybe_llm(settings, timeout=min(settings.openai_timeout, 20), reasoning_effort="minimal")
    visual_qa = VisualQAController.from_settings(settings, llm=llm)
    pipeline = MaxReadPipeline(
        store,
        arxiv,
        feishu,
        llm,
        require_source=settings.require_source,
        review_reasoning_effort=settings.openai_review_reasoning_effort,
        visual_qa=visual_qa,
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
        run_admin_server(settings, host=args.host, port=args.port)
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
    )


def _handle_event(pipeline: MaxReadPipeline, article_pipeline: ArticlePipeline, event, feedback_llm=None) -> None:
    settings = Settings.load(Path.cwd())
    store = pipeline.store

    if not _should_accept_event(event):
        return

    refs, web_refs = _extract_event_supported_inputs(article_pipeline.feishu, event)

    if _is_private_chat(event) and store.should_send_intro_to_user(event.sender_id):
        _reply_intro(article_pipeline.feishu, event, settings.feedback_url)
        store.mark_intro_sent(event.sender_id)

    if not refs and not web_refs:
        if _is_private_chat(event):
            if _record_feedback(store, article_pipeline.feishu, event, feedback_llm):
                return
            if should_send_intro(event.content):
                _reply_intro(article_pipeline.feishu, event, settings.feedback_url)
        elif getattr(event, "mentioned_bot", False):
            _reply_group_intro(article_pipeline.feishu, event)
        return
    enqueue_event_items(settings, store, article_pipeline.feishu, event, refs, web_refs)


def _extract_event_supported_inputs(feishu: FeishuClient, event) -> Tuple[List[PaperRef], List[WebRef]]:
    refs, web_refs = extract_supported_inputs(event.content)
    if refs or web_refs or _is_private_chat(event):
        return refs, web_refs
    if not getattr(event, "mentioned_bot", False):
        return refs, web_refs
    context = feishu.fetch_related_message_text(event)
    if not context:
        return refs, web_refs
    return extract_supported_inputs(event.content + "\n" + context)


def _should_accept_event(event) -> bool:
    if _is_private_chat(event):
        return True
    return bool(getattr(event, "mentioned_bot", False))


def _is_private_chat(event) -> bool:
    return str(getattr(event, "chat_type", "")).lower() in {"p2p", "private"}


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
