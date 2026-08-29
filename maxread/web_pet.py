from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import Store
from .openai_client import OpenAIClient
from .project_metadata import PROJECT_CATEGORIES, auto_project_category, load_generated_project_context, load_generated_project_summary


PROGRESS_STATES = {
    "queued": (5, "等待调度"),
    "claimed": (10, "任务已认领"),
    "preparing": (16, "准备论文材料"),
    "generating": (38, "生成初稿"),
    "generation_checking": (46, "检查初稿"),
    "generation_repairing": (42, "修复初稿"),
    "reviewing": (57, "内容审阅"),
    "quality_checking": (69, "格式质检"),
    "quality_repairing": (65, "修复格式"),
    "publishing": (79, "写入飞书"),
    "post_publish_checking": (85, "发布后检查"),
    "visual_checking": (92, "视觉验收"),
    "visual_repairing": (89, "修复页面"),
    "completed": (100, "完成交付"),
    "done": (100, "完成交付"),
    "failed": (100, "执行失败"),
    "quality_failed": (100, "质量未通过"),
    "cancelled": (100, "已取消"),
}

STAGE_EXPLANATIONS = {
    "queued": "任务已进入全局队列，等待 worker 认领。",
    "preparing": "正在获取并解析论文源码、图片、公式和表格。",
    "generating": "模型正在根据论文证据生成结构化初稿。",
    "reviewing": "正在核对方法上下文、实验结论和图文对应。",
    "quality_checking": "正在检查公式、表格和格式化字符。",
    "publishing": "正在把通过检查的内容写入飞书文档。",
    "visual_checking": "正在检查飞书真实渲染页面中的公式、图片和排版。",
    "completed": "文档已经通过交付检查。",
    "failed": "任务已经停止，需要查看失败卡片后决定是否重试。",
}

PET_TOOLS = {
    "get_project": "读取当前项目的准确状态。",
    "inspect_project": "读取当前项目的时间线、worker 心跳和检查产物摘要。",
    "explain_stage": "解释一个 MaxRead 工作流阶段。",
    "retry_project": "仅在用户明确要求修复或重试时，重试当前失败项目。",
    "recover_stale_project": "仅在用户明确要求处理且心跳过期时，恢复当前项目。",
}

@dataclass(frozen=True)
class PetScope:
    public_id: str
    feishu_open_id: str
    actor_type: str
    actor_id: str


class WebPetAgent:
    """A bounded, identity-scoped companion agent for the web UI."""

    MAX_STEPS = 3

    def __init__(self, settings, store: Store, identity) -> None:
        self.settings = settings
        self.store = store
        self.identity = identity
        self.scope = PetScope(
            public_id=str(identity.get("public_id") or ""),
            feishu_open_id=str(identity.get("feishu_open_id") or ""),
            actor_type=str(identity.get("_actor_type") or "user"),
            actor_id=str(identity.get("_actor_id") or store.web_identity_sender(identity)),
        )
        self.project: dict | None = None
        self.request_text = ""

    def reply(
        self,
        text: str,
        job_id: int = 0,
        source_id: str = "",
        history: list[dict] | None = None,
    ) -> tuple[str, dict]:
        progress = progress_payload(self.settings, self.store, self.identity)
        self.request_text = str(text or "")
        if re.search(r"按钮|页面怎么用|项目台怎么用|一键整理|分类折叠", self.request_text, re.I):
            return button_guide_answer(), progress
        target_id = int(job_id or 0)
        target_source = str(source_id or "").strip()
        self.project = next(
            (
                item for item in progress.get("recent", [])
                if (target_id and int(item["job_id"]) == target_id)
                or (target_source and item["source_id"] == target_source)
            ),
            None,
        )
        if (target_id or target_source) and self.project is None:
            raise ValueError("项目不在当前账号范围")
        scoped = {
            **progress,
            "active": self.project if self.project and self.project["status"] in {"queued", "running"} else None,
            "recent": [self.project] if self.project else progress.get("recent", []),
        }
        if self.project and re.search(r"重试|再试|修复|恢复|处理一下|解决", text, re.I):
            return self._repair_project(), scoped
        if self.project and re.search(r"卡住|卡在|怎么回事|为什么不动|调查|诊断|查一下|有问题", text, re.I):
            return self._diagnose_project(), scoped
        if self.project and re.search(r"进度|到哪|多久|状态|失败|完成|还要|排队|为什么|阶段", text, re.I):
            return project_status_answer(self.project, text), scoped
        if re.search(r"进度|到哪|多久|状态|卡住|失败|完成|还要|排队|为什么", text, re.I):
            return deterministic_status_answer(scoped), scoped
        if not str(getattr(self.settings, "openai_api_key", "") or ""):
            return "我在这儿陪你等。你可以问我这个项目的阶段含义或预计还要多久。", scoped
        return self._agent_loop(text, scoped, history or []), scoped

    def _agent_loop(self, text: str, progress: dict, history: list[dict]) -> str:
        transcript = [
            {
                "role": str(item.get("role") or "user")[:16],
                "content": str(item.get("content") or "")[:500],
            }
            for item in history[-8:]
            if str(item.get("role") or "") in {"user", "assistant"}
        ]
        transcript.append({"role": "user", "content": text})
        for _step in range(self.MAX_STEPS):
            response = self._model_call(transcript)
            action = _parse_agent_action(response)
            if action.get("type") == "answer":
                answer = str(action.get("text") or "").strip()
                if answer:
                    return answer[:1200]
            tool = str(action.get("tool") or "")
            if action.get("type") != "tool" or tool not in PET_TOOLS:
                return str(response or "").strip()[:1200] or "我在这儿。要不要看看当前任务？"
            result = self._run_tool(tool, action.get("args") or {}, progress)
            transcript.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            transcript.append({"role": "tool", "name": tool, "content": json.dumps(result, ensure_ascii=False)})
        return "我查了几步还没组织好答案。你可以换个更具体的问题，比如“当前任务到哪了”。"

    def _run_tool(self, tool: str, args: dict, progress: dict):
        if tool == "get_project":
            return self.project or {"error": "当前没有选中的项目"}
        if tool == "inspect_project":
            return self._diagnostic_snapshot()
        if tool == "explain_stage":
            stage = str(args.get("stage") or "")
            return {"stage": stage, "explanation": STAGE_EXPLANATIONS.get(stage, "这是内部工作流阶段，当前没有更细说明。")}
        if tool == "retry_project":
            return {"result": self._repair_project(only="retry")}
        if tool == "recover_stale_project":
            return {"result": self._repair_project(only="recover")}
        return {"error": "工具不在允许范围"}

    def _owned_job(self) -> dict | None:
        if not self.project or not int(self.project.get("job_id") or 0):
            return None
        job_id = int(self.project["job_id"])
        return next(
            (job for job in self.store.list_web_identity_jobs(self.identity, 200) if int(job["id"]) == job_id),
            None,
        )

    def _diagnostic_snapshot(self) -> dict:
        job = self._owned_job()
        if job is None:
            return {"project": self.project or {}, "conclusion": "这是缓存项目，没有活动队列任务。"}
        job_id = int(job["id"])
        events = self.store.list_job_events(job_id, 16)
        heartbeat_age = _elapsed_seconds(job.get("heartbeat_at")) if job.get("heartbeat_at") else None
        stage_age = _elapsed_seconds(job.get("stage_updated_at") or job.get("updated_at"))
        return {
            "project": {
                "job_id": job_id,
                "source_id": str(job.get("source_id") or ""),
                "status": str(job.get("status") or ""),
                "workflow_state": str(job.get("workflow_state") or ""),
                "stage": str(job.get("stage") or ""),
                "attempts": int(job.get("attempts") or 0),
                "heartbeat_age_seconds": heartbeat_age,
                "stage_age_seconds": stage_age,
                "error": _friendly_error(str(job.get("error") or "")),
                "has_publish_checkpoint": bool(str(job.get("checkpoint_json") or "").strip()),
            },
            "events": [
                {
                    "event": str(event.get("event_type") or ""),
                    "detail": str(event.get("detail") or "")[:320],
                    "at": str(event.get("created_at") or ""),
                }
                for event in events[:10]
            ],
            "artifacts": _project_artifact_snapshot(self.settings, str(job.get("source_id") or "")),
            "service": self.store.get_service_status(),
        }

    def _diagnose_project(self) -> str:
        snapshot = self._diagnostic_snapshot()
        project = snapshot.get("project") or {}
        status = str(project.get("status") or "")
        stage = str(project.get("workflow_state") or project.get("stage") or "")
        heartbeat_age = project.get("heartbeat_age_seconds")
        stage_age = int(project.get("stage_age_seconds") or 0)
        artifacts = snapshot.get("artifacts") or {}
        recent_files = artifacts.get("recent_files") or []
        evidence = f"最近产物：{recent_files[0]}。" if recent_files else "目前还没有新的阶段产物。"
        if status == "queued":
            position = self.store.queue_position(int(project.get("job_id") or 0))
            return f"我查了队列和事件：任务仍在等待调度，当前约第 {max(1, position)} 位，没有被 worker 卡死。{evidence}"
        if status == "running":
            stale_minutes = max(1, int(getattr(self.settings, "queue_stale_minutes", 10)))
            if heartbeat_age is None or heartbeat_age > stale_minutes * 60:
                return f"我查到 worker 心跳已经超过 {stale_minutes} 分钟，属于可恢复的执行中断，不是正常生成等待。你可以让我“处理一下”，我会只恢复这个项目。"
            return (
                f"我查了 worker 心跳、阶段时间线和产物：心跳在 {max(0, int(heartbeat_age))} 秒前仍正常，"
                f"当前状态是“{self.project.get('label') if self.project else stage}”，这一阶段已持续约 {max(1, round(stage_age / 60))} 分钟。"
                f"进程没有挂；阶段标签会在当前模型调用或检查步骤结束后才推进。{evidence}"
            )
        if status == "failed":
            error = project.get("error") or "没有记录到明确错误"
            checkpoint = "已有发布检查点，可从实页检查继续。" if project.get("has_publish_checkpoint") else "没有发布检查点，重试会重建生成流程。"
            return f"我查了失败记录、事件和产物：{error}。{checkpoint}{evidence}"
        if status == "done":
            return "我核对了任务终态：项目已经完成并通过交付，文档入口在项目卡上。"
        return f"我查到当前状态为 {status or stage or '未知'}。{evidence}"

    def _repair_project(self, only: str = "") -> str:
        if not re.search(r"重试|再试|修复|恢复|处理|解决", self.request_text, re.I):
            return "我没有执行操作，因为你还没有明确要求修复或重试。"
        job = self._owned_job()
        if job is None:
            return "这个项目没有可操作的队列任务。"
        job_id = int(job["id"])
        status = str(job.get("status") or "")
        if status == "failed" and only != "recover":
            error = str(job.get("error") or "")
            resume = bool(str(job.get("checkpoint_json") or "").strip()) and (
                "visual-qa:remote-error" in error or "Feishu PDF export failed" in error
            )
            ok = self.store.retry_queue_job(
                job_id,
                reason=f"project agent retry requested by {self.scope.actor_type}:{self.scope.actor_id}",
                event_type="project_agent_retry",
                suppress_progress_notifications=False,
                rebuild_pipeline=not resume,
            )
            return "已重新加入队列，并保留历史检查结果避免重复犯错。" if ok else "任务状态刚刚发生变化，我没有重复操作。"
        if status == "running" and only != "retry":
            stale_minutes = max(1, int(getattr(self.settings, "queue_stale_minutes", 10)))
            recovered = self.store.recover_stale_queue_job(job_id, stale_minutes)
            return "确认心跳过期，已只恢复这个项目并静默重新排队。" if recovered else "复查后心跳仍正常，我没有强行中断正在执行的 worker。"
        return "这个项目当前不需要恢复；我没有做无效重试。"

    def _model_call(self, transcript: list[dict]) -> str:
        system = (
            "你叫 Max，是 MaxRead 网页里的绿色小狗任务伙伴，也是一个受限、只读、类似 Codex 的小型 agent。"
            "你固定陪在一张论文项目卡旁，只讨论用户当前点开的这一篇论文项目，也可以回答轻松的普通问题。"
            "遇到故障或卡住问题必须先用 inspect_project 调查时间线、心跳和检查产物，再给出有证据的结论。"
            "你只能使用 get_project、inspect_project、explain_stage、retry_project、recover_stale_project。"
            "只有用户明确说重试、修复、恢复、处理或解决时才可调用后两个写工具。"
            "你不能访问原始 SQL、任意文件、密钥、全局队列、其他用户、任意网络，也不能提交、删除或修改其他任务。"
            "你的聊天是临时侧边对话，不属于正式项目记录。管理员代入不会扩大数据范围。"
            "状态事实必须来自工具，不能猜百分比、失败原因或完成时间。"
            "每轮只输出一个 JSON：调用工具时为 {\"type\":\"tool\",\"tool\":\"工具名\",\"args\":{}}；"
            "回答时为 {\"type\":\"answer\",\"text\":\"自然简短的中文\"}。普通回复 2 到 4 句。"
        )
        user = "对话记录：\n" + json.dumps(transcript, ensure_ascii=False)
        try:
            client = OpenAIClient(
                self.settings.openai_api_key,
                self.settings.model,
                timeout=min(45, int(self.settings.openai_timeout)),
                base_url=self.settings.openai_base_url,
                sub_module=self.settings.openai_sub_module,
                reasoning_effort="low",
                api_mode=self.settings.openai_api_mode,
            )
            return str(client.responses_text(system, user, reasoning_effort="low") or "")
        except Exception:
            return '{"type":"answer","text":"我刚才走神了一下，不过任务状态仍在正常记录。你可以直接问我现在到哪了。"}'


def progress_payload(settings, store: Store, identity) -> dict:
    jobs = store.list_web_identity_jobs(identity, 200)
    duration = max(60, int(store.recent_job_duration_seconds("paper") or 300))
    latest_jobs: dict[str, dict] = {}
    for job in jobs:
        source_id = str(job.get("source_id") or "")
        current = latest_jobs.get(source_id)
        if source_id and (current is None or int(job.get("id") or 0) > int(current.get("id") or 0)):
            latest_jobs[source_id] = job
    payload = [
        _progress_row(store, job, duration, settings.queue_workers)
        for job in latest_jobs.values()
    ]
    known_sources = {item["source_id"] for item in payload}
    for usage in store.list_web_identity_usage(identity, 80):
        source_id = str(usage.get("source_id") or "")
        if not source_id or source_id in known_sources:
            continue
        status = str(usage.get("status") or "")
        payload.append({
            "job_id": 0,
            "source_id": source_id,
            "title": str(usage.get("title") or ""),
            "summary": str(usage.get("project_summary") or ""),
            "status": "done" if status == "done" else status,
            "workflow_state": "completed" if status == "done" else status,
            "stage": "completed" if status == "done" else status,
            "label": "完成交付" if status == "done" else (status or "已记录"),
            "percent": 100 if status == "done" else 5,
            "remaining_seconds": 0,
            "elapsed_seconds": 0,
            "overdue": False,
            "attempts": 0,
            "doc_url": str(usage.get("doc_url") or ""),
            "error": _friendly_error(str(usage.get("error") or "")),
            "updated_at": str(usage.get("updated_at") or usage.get("created_at") or ""),
        })
        known_sources.add(source_id)
    for item in payload:
        if item.get("summary"):
            continue
        generated_summary = load_generated_project_summary(
            Path(getattr(settings, "workdir", ".")),
            item["source_id"],
        )
        if generated_summary:
            item["summary"] = generated_summary
            store.set_paper_project_summary(item["source_id"], generated_summary)
    preferences = store.web_project_preferences(identity)
    visible = []
    auto_assignments = {}
    for item in payload:
        preference = preferences.get(item["source_id"], {})
        if preference.get("deleted_at"):
            continue
        stored_category = str(preference.get("category") or "").strip()
        stored_source = str(preference.get("category_source") or "").strip()
        item["favorite"] = bool(preference.get("favorite"))
        if item.get("status") != "done":
            item["category"] = "进行中"
            item["category_source"] = "status"
        elif stored_category:
            item["category"] = stored_category
            item["category_source"] = stored_source or "manual"
        else:
            context = load_generated_project_context(
                Path(getattr(settings, "workdir", ".")),
                item["source_id"],
                fallback=item.get("summary", ""),
            )
            category = auto_project_category(item.get("title", ""), context or item.get("summary", ""))
            item["category"] = category
            item["category_source"] = "ai"
            auto_assignments[item["source_id"]] = category
        visible.append(item)
    if auto_assignments and str(identity.get("feishu_open_id") or "").strip():
        store.set_web_project_auto_categories(identity, auto_assignments)
    payload = visible
    payload.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    payload.sort(key=lambda item: 0 if item["status"] == "running" else 1 if item["status"] == "queued" else 2)
    payload.sort(key=lambda item: 0 if item.get("favorite") else 1)
    payload = payload[:100]
    active = next((item for item in payload if item["status"] in {"queued", "running"}), None)
    return {
        "active": active,
        "recent": payload,
        "service": store.get_service_status(),
        "categories": ["进行中", *PROJECT_CATEGORIES],
    }


def button_guide_answer() -> str:
    return (
        "这个项目台的按钮这样用：一键整理会对你发过的论文统一聚类并归类，且保留人工分类；"
        "新任务固定留在进行中，完成后我会参考标题、TL;DR 和正文开头把它搬到主题分类；"
        "分类标题可以折叠或展开类内项目；搜索框按标题或 arXiv ID 查找；分类下拉框可手动修正；"
        "星标用于收藏；重试从失败检查点继续；垃圾桶只从你的项目台移除，不会删除飞书文档。"
    )


def _progress_row(store: Store, job: dict, duration: int, workers: int) -> dict:
    status = str(job.get("status") or "")
    state = str(job.get("workflow_state") or job.get("stage") or status or "queued")
    percent, label = PROGRESS_STATES.get(state, PROGRESS_STATES.get(status, (12, state or "处理中")))
    elapsed = _elapsed_seconds(job.get("started_at") or job.get("created_at"))
    if status == "queued":
        position = max(1, int(store.queue_position(int(job["id"])) or 1))
        batches = max(1, (position - 1) // max(1, int(workers or 1)) + 1)
        remaining = batches * duration
    elif status == "running":
        remaining = max(0, duration - elapsed)
    else:
        remaining = 0
    error = _friendly_error(str(job.get("error") or ""))
    return {
        "job_id": int(job.get("id") or 0),
        "source_id": str(job.get("source_id") or ""),
        "title": str(job.get("resolved_title") or job.get("title") or ""),
        "summary": str(job.get("project_summary") or ""),
        "status": status,
        "workflow_state": state,
        "stage": str(job.get("stage") or ""),
        "label": label,
        "percent": percent,
        "remaining_seconds": remaining,
        "elapsed_seconds": elapsed,
        "overdue": status == "running" and elapsed >= duration,
        "attempts": int(job.get("attempts") or 0),
        "doc_url": str(job.get("doc_url") or ""),
        "error": error,
        "updated_at": str(job.get("updated_at") or ""),
    }


def deterministic_status_answer(progress: dict) -> str:
    active = progress.get("active")
    if active:
        remaining = int(active.get("remaining_seconds") or 0)
        if remaining > 0:
            eta = max(1, round(remaining / 60))
            return f"{active['source_id']} 现在在“{active['label']}”，整体约 {active['percent']}%。按最近任务估计还要 {eta} 分钟。"
        elapsed = max(1, round(int(active.get("elapsed_seconds") or 0) / 60))
        return f"{active['source_id']} 现在仍在“{active['label']}”，已经运行约 {elapsed} 分钟并超过近期同类任务用时。我会继续报告真实阶段，不再给一个假的倒计时。"
    recent = progress.get("recent") or []
    if not recent:
        return "现在没有论文在跑。把 arXiv 链接交给我，任务进入队列后我就能一直盯着。"
    latest = recent[0]
    if latest["status"] == "done":
        return f"最近的 {latest['source_id']} 已经完成，文档链接也在会话里。"
    if latest["status"] == "failed":
        return f"最近的 {latest['source_id']} 没有完成：{latest.get('error') or '等待重试'}。"
    return f"最近任务 {latest['source_id']} 当前状态是“{latest['label']}”。"


def project_status_answer(project: dict, question: str) -> str:
    status = str(project.get("status") or "")
    label = str(project.get("label") or "当前阶段")
    percent = int(project.get("percent") or 0)
    remaining = int(project.get("remaining_seconds") or 0)
    text = str(question or "")
    if re.search(r"阶段", text):
        state = str(project.get("workflow_state") or project.get("stage") or status)
        explanation = STAGE_EXPLANATIONS.get(state) or STAGE_EXPLANATIONS.get(str(project.get("stage") or ""))
        return f"现在是“{label}”。{explanation or '这个阶段正在推进当前项目的下一项可验证工作。'}"
    if re.search(r"多久|还要|时间", text):
        if status == "done":
            return "这个项目已经完成，不需要继续等待。"
        if status == "failed":
            return "当前已经停止计时；需要从项目卡重试后才会重新估算。"
        if remaining <= 0 and project.get("overdue"):
            elapsed = max(1, round(int(project.get("elapsed_seconds") or 0) / 60))
            return f"已经运行约 {elapsed} 分钟，超过近期同类任务用时；我不会再给一个假的倒计时，会继续根据心跳和阶段报告。"
        return f"按最近同类任务估计还要约 {max(1, round(remaining / 60))} 分钟。这个数字会随实际阶段更新。"
    if re.search(r"失败|卡住|为什么", text) and project.get("error"):
        return f"项目停在“{label}”：{project['error']}"
    if status == "done":
        return "这个项目已经完成，文档入口在项目卡上。"
    if status == "failed":
        return f"项目目前未完成，停在“{label}”。可以查看卡片里的原因并重试。"
    if remaining <= 0 and project.get("overdue"):
        elapsed = max(1, round(int(project.get("elapsed_seconds") or 0) / 60))
        return f"现在仍在“{label}”，整体约 {percent}% ，已运行约 {elapsed} 分钟并超过近期均值。"
    return f"现在在“{label}”，整体约 {percent}% ，预计还要 {max(1, round(remaining / 60))} 分钟。"


def _friendly_error(error: str) -> str:
    text = str(error or "")
    reasons = []
    if any(token in text for token in ("Feishu PDF export failed", "visual-qa:remote-error", "export-pending", "飞书 PDF 导出")):
        reasons.append("飞书 PDF 导出超时，尚未完成视觉验收")
    if "html-tag-in-formula" in text:
        reasons.append("发布后公式检查发现异常格式")
    if "missing-formula" in text or "invalid-formula" in text:
        reasons.append("页面中存在未正确渲染的公式")
    if reasons:
        return "；".join(dict.fromkeys(reasons))
    clean = re.sub(r"(?:/[^\s\]]+)+", "[内部路径]", text)
    clean = re.sub(r"\{.*", "", clean).strip(" :；")
    return clean[:260]


def _elapsed_seconds(value) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    except ValueError:
        return 0


def _parse_agent_action(raw: str) -> dict:
    text = str(raw or "").strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(text[start : end + 1])
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                pass
    return {"type": "answer", "text": text}


def _project_artifact_snapshot(settings, source_id: str) -> dict:
    if not re.fullmatch(r"\d{4}\.\d{4,5}", str(source_id or "")):
        return {"recent_files": [], "diagnostics": []}
    root = Path(getattr(settings, "workdir", "") or "") / "papers" / source_id / "pipeline_artifacts"
    if not root.is_dir():
        return {"recent_files": [], "diagnostics": []}
    files = sorted(
        (path for path in root.iterdir() if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:16]
    diagnostics = []
    for path in files:
        if path.name == "08-failure.txt":
            diagnostics.append({"file": path.name, "detail": path.read_text(encoding="utf-8", errors="replace")[:1200]})
        elif path.suffix == ".json" and any(token in path.name for token in ("quality", "visual", "attempt")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            compact = {
                key: payload.get(key)
                for key in ("passed", "errors", "warnings", "blocking_warnings", "findings", "rounds")
                if key in payload
            }
            diagnostics.append({"file": path.name, "detail": compact})
    return {
        "recent_files": [path.name for path in files],
        "diagnostics": diagnostics[:6],
    }


def chat_with_project_pet(
    settings,
    store: Store,
    identity,
    content: str,
    *,
    job_id: int = 0,
    source_id: str = "",
    history: list[dict] | None = None,
) -> dict:
    text = str(content or "").strip()
    if not text:
        raise ValueError("想问我什么？")
    if len(text) > 600:
        raise ValueError("这次先聊短一点吧")
    answer, progress = WebPetAgent(settings, store, identity).reply(
        text,
        job_id=int(job_id or 0),
        source_id=str(source_id or ""),
        history=history,
    )
    return {
        "ok": True,
        "message": {"role": "assistant", "kind": "pet_reply", "content": answer},
        "progress": progress,
    }
