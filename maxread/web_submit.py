from __future__ import annotations

import hashlib
import secrets
import uuid
from pathlib import Path

from .db import Store
from .project_metadata import PROJECT_CATEGORIES
from .sources import extract_supported_inputs


WEB_SESSION_COOKIE = "maxread_web_session"
WEB_SESSION_BYTES = 32
WEB_BINDING_TTL_MINUTES = 10
WEB_SUBMISSION_LIMIT = 5
WEB_RATE_LIMIT = 10


def session_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def new_web_identity(store: Store, token: str = ""):
    session_token = str(token or "").strip() or secrets.token_urlsafe(WEB_SESSION_BYTES)
    digest = session_hash(session_token)
    identity = store.get_web_identity(digest)
    identity = store.get_or_create_web_identity(
        digest,
        "" if identity else f"web_{secrets.token_hex(6)}",
    )
    return session_token, identity


def web_identity_payload(identity) -> dict:
    bound = bool(str(identity.get("feishu_open_id") or ""))
    return {
        "public_id": str(identity.get("public_id") or ""),
        "account_type": "feishu" if bound else "guest",
        "display_name": str(identity.get("display_name") or ("飞书用户" if bound else "游客")),
        "bound": bound,
        "acting_as": str(identity.get("_actor_type") or "") == "admin",
    }


def issue_binding_code(store: Store, identity) -> dict:
    code = f"{secrets.randbelow(1_000_000):06d}"
    store.issue_web_binding_code(
        int(identity["id"]),
        hashlib.sha256(code.encode("ascii")).hexdigest(),
        WEB_BINDING_TTL_MINUTES,
    )
    return {
        "code": code,
        "command": f"绑定 {code}",
        "expires_in_seconds": WEB_BINDING_TTL_MINUTES * 60,
    }


def claim_binding_code(store: Store, code: str, feishu_open_id: str):
    clean = str(code or "").strip()
    if len(clean) != 6 or not clean.isdigit():
        return None
    return store.claim_web_binding_code(
        hashlib.sha256(clean.encode("ascii")).hexdigest(),
        feishu_open_id,
    )


def submit_web_papers(settings, store: Store, identity, content: str) -> dict:
    text = str(content or "").strip()
    if not text:
        raise ValueError("请粘贴 arXiv 链接或论文 ID")
    if len(text) > 8_000:
        raise ValueError("提交内容过长")
    paper_refs, web_refs = extract_supported_inputs(text)
    if web_refs and not paper_refs:
        raise ValueError("当前网页入口只接受 arXiv、HuggingFace Papers 或 papers.cool 论文链接")
    if not paper_refs:
        raise ValueError("没有识别到 arXiv 论文")
    if len(paper_refs) > WEB_SUBMISSION_LIMIT:
        raise ValueError(f"一次最多提交 {WEB_SUBMISSION_LIMIT} 篇")
    if store.recent_web_submission_count(identity["public_id"], 10) >= WEB_RATE_LIMIT:
        raise ValueError("提交过于频繁，请稍后再试")

    sender_id = store.web_identity_sender(identity)
    chat_id = f"web:{identity['public_id']}"
    conversation = store.ensure_web_conversation(identity)
    user_message_id = f"web-message:{uuid.uuid4().hex}"
    store.append_web_message(
        int(conversation["id"]),
        user_message_id,
        "user",
        text,
        kind="submission",
        channel="web",
        actor_type=str(identity.get("_actor_type") or "user"),
        actor_id=str(identity.get("_actor_id") or sender_id),
    )
    service_status = store.get_service_status()
    service_available = service_status["mode"] == "operational"
    event_id = f"web-event:{uuid.uuid4().hex}"
    items = []
    for ref in paper_refs:
        store.restore_web_project(identity, ref.paper_id)
        message_id = user_message_id
        record = store.get_paper(ref.paper_id)
        usage_id = store.add_usage_event(
            event_id,
            message_id,
            chat_id,
            "web",
            sender_id,
            "paper",
            ref.paper_id,
            ref.url,
            title=record.title if record else "",
            status="queued",
        )
        if record and record.status == "done" and record.doc_url:
            store.update_usage_event(usage_id, "done", doc_url=record.doc_url, title=record.title)
            items.append({
                "paper_id": ref.paper_id,
                "usage_id": usage_id,
                "status": "done",
                "cached": True,
                "doc_url": record.doc_url,
                "title": record.title,
            })
            store.upsert_web_task(
                identity,
                0,
                ref.paper_id,
                f"这篇已有可用文档：{record.title or ref.paper_id}",
                kind="result",
                doc_url=record.doc_url,
                status="done",
            )
            continue
        queued = store.enqueue_job(
            "paper",
            ref.paper_id,
            ref.url,
            event_id,
            message_id,
            chat_id,
            "web",
            sender_id,
            usage_id,
            suppress_progress_notifications=False,
        )
        if not queued["created"]:
            store.update_usage_event(usage_id, "watching")
        position = store.queue_position(int(queued["job_id"]))
        duration = store.recent_job_duration_seconds("paper")
        worker_count = max(1, int(settings.queue_workers))
        batch = max(1, (max(1, position) - 1) // worker_count + 1)
        items.append({
            "paper_id": ref.paper_id,
            "usage_id": usage_id,
            "job_id": int(queued["job_id"]),
            "status": "queued" if service_available else "waiting_for_service",
            "cached": False,
            "queue_position": position,
            "estimated_wait_seconds": max(0, batch - 1) * duration,
            "estimated_total_seconds": batch * duration,
        })
        store.update_web_job_progress(
            {"chat_type": "web", "message_id": user_message_id, "sender_id": sender_id},
            int(queued["job_id"]),
            ref.paper_id,
            (
                f"已加入队列，第 {max(1, position)} 位。"
                f"预计等待 {max(0, batch - 1) * duration // 60} 分钟，"
                f"预计完成约 {max(1, batch * duration // 60)} 分钟。"
            ),
            "queued" if service_available else "waiting_for_service",
        )
    return {
        "ok": True,
        "items": items,
        "service": service_status,
    }


def retry_web_job(settings, store: Store, identity, job_id: int) -> dict:
    target_id = int(job_id or 0)
    job = store.get_web_identity_job(identity, target_id)
    if job is None:
        raise ValueError("任务不在当前账号范围")
    if str(job.get("status") or "") != "failed":
        raise ValueError("只有失败任务可以重试")
    error = str(job.get("error") or "")
    has_publish_checkpoint = bool(str(job.get("checkpoint_json") or "") and str(job.get("doc_url") or ""))
    resume_published = has_publish_checkpoint and any(
        marker in error.lower()
        for marker in (
            "visual-qa:",
            "post-publish",
            "发布后质检",
            "pdf export",
            "table-overflow",
            "table-clipped",
        )
    )
    actor_type = str(identity.get("_actor_type") or "user")
    actor_id = str(identity.get("_actor_id") or store.web_identity_sender(identity))
    ok = store.retry_queue_job(
        target_id,
        reason=f"web retry requested by {actor_type}:{actor_id}",
        event_type="web_retry",
        suppress_progress_notifications=False,
        rebuild_pipeline=not resume_published,
    )
    if not ok:
        raise ValueError("任务状态已变化，请刷新后再试")
    store.upsert_web_task(
        identity,
        target_id,
        str(job["source_id"]),
        "已重新加入队列。" if not resume_published else "已从已发布文档继续视觉验收。",
        status="queued",
    )
    return {"ok": True, "job_id": target_id, "resume_published": resume_published}


def update_web_project(store: Store, identity, source_id: str, action: str, value=None) -> dict:
    clean_action = str(action or "").strip().lower()
    if clean_action == "favorite":
        favorite = value is True or str(value or "").strip().lower() in {"1", "true", "yes", "on"}
        return {"ok": True, **store.set_web_project_favorite(identity, source_id, favorite)}
    if clean_action == "category":
        category = str(value or "").strip()
        if category not in PROJECT_CATEGORIES:
            raise ValueError("不支持的项目分类")
        return {"ok": True, **store.set_web_project_category(identity, source_id, category)}
    if clean_action == "delete":
        return {"ok": True, **store.delete_web_project(identity, source_id)}
    raise ValueError("不支持的项目操作")


WEB_SUBMIT_HTML = (Path(__file__).resolve().parent / "static" / "web_submit.html").read_text(encoding="utf-8")
