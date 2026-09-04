from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import PipelineSettings
from .cache_sync import sync_base_to_cache
from .runner import RecruitingRunner, _within_days
from .store import PipelineStore
from .weekly_report import markdown_to_post, render_weekly_report


RECENT_VIEW_ID = "vewVVbQsCs"
OTHER_RECENT_VIEW_ID = "vewmpcpnxQ"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incremental read-only recruiting mailbox pipeline")
    parser.add_argument("--root", default=".", help="maxread repository root")
    parser.add_argument("--env-file", default="features/mail_ingestion/data/accounts/zip-lab.env")
    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("scan-once", help="scan folders and process new/follow-up threads once")
    once.add_argument("--skip-scan", action="store_true")
    once.add_argument("--no-docs", action="store_true", help="skip cloud document writes; useful for offline tests")
    once.add_argument("--no-ai", action="store_true", help="use deterministic unknown fallback")
    once.add_argument("--dry-run", action="store_true", help="scan and extract but do not write Base or cloud documents")
    once.add_argument("--max-threads", type=int, default=None, help="bound this run to the newest N changed threads")
    once.add_argument("--only-candidates", action="store_true", help="skip deterministic service/notification threads")
    once.add_argument("--reprocess", action="store_true", help="re-run extraction for existing threads")
    once.add_argument("--since-days", type=int, default=None, help="only process threads updated within this window")

    run = sub.add_parser("run", help="run scan-once repeatedly")
    run.add_argument("--interval-days", type=float, default=None, help="override RECRUITING_SCAN_INTERVAL_DAYS")
    run.add_argument("--no-docs", action="store_true")
    run.add_argument("--no-ai", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-threads", type=int, default=None)
    run.add_argument("--only-candidates", action="store_true")
    run.add_argument("--reprocess", action="store_true")
    run.add_argument("--since-days", type=int, default=None)
    run.add_argument("--run-immediately", action="store_true", default=True)

    sub.add_parser("status", help="show recent pipeline runs")
    report = sub.add_parser("weekly-report", help="render or send the weekly recruiting summary")
    report.add_argument("--send", action="store_true", help="send the report through lark-cli")
    report.add_argument("--chat-id", default=None, help="target group or private chat oc_xxx")
    report.add_argument("--as", dest="identity", default="bot", choices=("bot", "user"))
    report.add_argument("--idempotency-key", default=None, help="override the default weekly idempotency key for a test send")
    backfill = sub.add_parser("backfill-base", help="rebuild deleted Base rows from durable local mail state")
    backfill.add_argument("--days", type=int, default=30)
    backfill.add_argument("--skip-scan", action="store_true")
    backfill.add_argument("--max-threads", type=int, default=None)
    backfill.add_argument("--dry-run", action="store_true", help="only count candidate threads; do not reset or write")
    backfill.add_argument("--confirm", action="store_true", help="required before resetting Base record mappings")
    backfill.add_argument("--refresh-ai", action="store_true", help="re-run model extraction instead of reusing durable fields")
    backfill.add_argument("--refresh-docs", action="store_true", help="allow material document updates during backfill")
    academics = sub.add_parser("repair-academics", help="audit or repair academic summaries from stored mail and PDFs")
    academics.add_argument("--days", type=int, default=None, help="only inspect threads updated within this window")
    academics.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    academics.add_argument("--confirm", action="store_true", help="required before updating SQLite, Base, and document summaries")
    identities = sub.add_parser("repair-identities", help="recover missing candidate names from stored mail evidence")
    identities.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    identities.add_argument("--confirm", action="store_true", help="required before updating SQLite, Base, and document summaries")
    tags = sub.add_parser("tag-records", help="refresh AI extraction and write school/rank/reply tags to Base")
    tags.add_argument("--days", type=int, default=None, help="only reprocess threads updated within this window")
    tags.add_argument("--max-threads", type=int, default=None, help="bound reprocessing to the newest N threads")
    tags.add_argument("--dry-run", action="store_true", help="show how many durable rows would be processed")
    tags.add_argument("--confirm", action="store_true", help="required before AI calls and Base updates")
    provenance = sub.add_parser("sync-provenance", help="recompute source mailboxes and reply status without AI")
    provenance.add_argument("--days", type=int, default=None)
    provenance.add_argument("--dry-run", action="store_true")
    provenance.add_argument("--confirm", action="store_true")
    sub.add_parser("sync-base-cache", help="refresh the local SQLite read model from authoritative Feishu Base")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    settings = PipelineSettings.load(root, args.env_file)
    store = PipelineStore(settings.db_path)
    store.initialize()

    if args.command == "status":
        print(json.dumps({"ok": True, "runs": store.list_runs()}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync-base-cache":
        print(json.dumps(sync_base_to_cache(settings), ensure_ascii=False, indent=2))
        return 0

    if args.command == "weekly-report":
        if args.send:
            _refresh_recent_view(settings)
        markdown, period_key = render_weekly_report(settings.db_path)
        if not args.send:
            print(markdown)
            return 0
        if not args.chat_id:
            raise SystemExit("--chat-id is required with --send")
        command = [
            settings.lark_cli,
            "im",
            "+messages-send",
            "--as",
            args.identity,
            "--chat-id",
            args.chat_id,
            "--msg-type",
            "post",
            "--content",
            json.dumps(markdown_to_post(markdown), ensure_ascii=False),
            "--idempotency-key",
            args.idempotency_key or f"recruiting-weekly-{period_key}",
            "--format",
            "json",
        ]
        result = subprocess.run(command, cwd=root, env=settings.command_env(), capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or result.stdout.strip() or "weekly report send failed")
        print(result.stdout)
        return 0

    if args.command == "backfill-base":
        planned = store.reset_base_links(args.days, only_candidates=True, dry_run=True)
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "days": args.days, "planned_threads": planned}, ensure_ascii=False, indent=2))
            return 0
        if not args.confirm:
            raise SystemExit(f"backfill would reset {planned} Base mappings; rerun with --confirm")
        reset = store.reset_base_links(args.days, only_candidates=True)
        runner = RecruitingRunner(settings, no_docs=not args.refresh_docs)
        if not args.refresh_ai:
            runner.llm = None
        result = runner.run_once(
            skip_scan=args.skip_scan,
            max_threads=args.max_threads,
            only_candidates=True,
            reprocess=True,
            since_days=args.days,
        )
        result["base_links_reset"] = reset
        result["reused_existing_fields"] = not args.refresh_ai
        result["documents_enabled"] = bool(args.refresh_docs)
        result["recent_view"] = _refresh_recent_view_best_effort(settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "repair-academics":
        if not args.dry_run and not args.confirm:
            raise SystemExit("repair-academics requires --dry-run or --confirm")
        runner = RecruitingRunner(settings)
        result = runner.repair_academics(apply=bool(args.confirm), since_days=args.days)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "repair-identities":
        if not args.dry_run and not args.confirm:
            raise SystemExit("repair-identities requires --dry-run or --confirm")
        runner = RecruitingRunner(settings)
        result = runner.repair_identities(apply=bool(args.confirm))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "tag-records":
        if not args.dry_run and not args.confirm:
            raise SystemExit("tag-records requires --dry-run or --confirm")
        rows = store.list_threads()
        if args.days is not None:
            rows = [row for row in rows if _within_days(str(row["latest_time"] or ""), args.days)]
        if args.max_threads is not None:
            rows = rows[: max(0, args.max_threads)]
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "planned_threads": len(rows)}, ensure_ascii=False, indent=2))
            return 0
        runner = RecruitingRunner(settings, no_docs=True)
        result = runner.run_once(
            skip_scan=True,
            max_threads=args.max_threads,
            reprocess=True,
            since_days=args.days,
        )
        result["tagging"] = {
            "ai_refreshed": True,
            "base_fields": ["院校", "排名", "排名依据", "是否985", "是否C9", "是否已回复", "来源邮箱"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync-provenance":
        if not args.dry_run and not args.confirm:
            raise SystemExit("sync-provenance requires --dry-run or --confirm")
        runner = RecruitingRunner(settings, no_docs=True)
        runner.llm = None
        result = runner.sync_provenance(apply=bool(args.confirm), since_days=args.days)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "scan-once":
        runner = RecruitingRunner(settings, llm=None if args.no_ai else None, no_docs=args.no_docs, dry_run=args.dry_run)
        if args.no_ai:
            runner.llm = None
        result = runner.run_once(
            skip_scan=args.skip_scan,
            max_threads=args.max_threads,
            only_candidates=args.only_candidates,
            reprocess=args.reprocess,
            since_days=args.since_days,
        )
        if not args.dry_run:
            result["recent_view"] = _refresh_recent_view_best_effort(settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "run":
        if args.interval_days is not None:
            settings = replace(settings, interval_days=args.interval_days)
        lock_path = settings.db_path.parent / "recruiting-pipeline.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (ImportError, BlockingIOError):
                raise SystemExit("another recruiting pipeline instance is already running")
            while True:
                runner = RecruitingRunner(settings, no_docs=args.no_docs, dry_run=args.dry_run)
                if args.no_ai:
                    runner.llm = None
                result = runner.run_once(
                    max_threads=args.max_threads,
                    only_candidates=args.only_candidates,
                    reprocess=args.reprocess,
                    since_days=args.since_days,
                )
                if not args.dry_run:
                    result["recent_view"] = _refresh_recent_view_best_effort(settings)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                time.sleep(settings.interval_days * 86400)
        return 0

    return 2


def _refresh_recent_view(settings: PipelineSettings) -> None:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    start = now - timedelta(days=7)
    for view_id, mail_type in (
        (RECENT_VIEW_ID, "候选人来信"),
        (OTHER_RECENT_VIEW_ID, "其他"),
    ):
        payload = {
            "logic": "and",
            "conditions": [
                ["邮件类型", "intersects", [mail_type]],
                ["最新邮件时间", ">", f"ExactDate({start.strftime('%Y-%m-%d %H:%M')})"],
            ],
        }
        command = [
            settings.lark_cli,
            "base",
            "+view-set-filter",
            "--base-token",
            settings.base_token,
            "--table-id",
            settings.table_id,
            "--view-id",
            view_id,
            "--json",
            json.dumps(payload, ensure_ascii=False),
            "--as",
            settings.feishu_as,
            "--format",
            "json",
        ]
        result = subprocess.run(command, cwd=settings.root, env=settings.command_env(), capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"recent view update failed: {mail_type}")


def _refresh_recent_view_best_effort(settings: PipelineSettings) -> dict[str, object]:
    try:
        _refresh_recent_view(settings)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}
