from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Store
from .openai_client import OpenAIClient


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
    "explain_stage": "解释一个 MaxRead 工作流阶段。",
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

    def reply(
        self,
        text: str,
        job_id: int = 0,
        source_id: str = "",
        history: list[dict] | None = None,
    ) -> tuple[str, dict]:
        progress = progress_payload(self.settings, self.store, self.identity)
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
        if tool == "explain_stage":
            stage = str(args.get("stage") or "")
            return {"stage": stage, "explanation": STAGE_EXPLANATIONS.get(stage, "这是内部工作流阶段，当前没有更细说明。")}
        return {"error": "工具不在允许范围"}

    def _model_call(self, transcript: list[dict]) -> str:
        system = (
            "你叫 Max，是 MaxRead 网页里的绿色小狗任务伙伴，也是一个受限、只读、类似 Codex 的小型 agent。"
            "你固定陪在一张论文项目卡旁，只讨论用户当前点开的这一篇论文项目，也可以回答轻松的普通问题。"
            "你只能使用两个工具：get_project、explain_stage。"
            "你不能访问原始 SQL、文件系统、密钥、全局队列、其他用户、任意网络，也不能提交、重试、删除或修改任务。"
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
    jobs = store.list_web_identity_jobs(identity, 50)
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
    payload.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    payload.sort(key=lambda item: 0 if item["status"] == "running" else 1 if item["status"] == "queued" else 2)
    payload = payload[:30]
    active = next((item for item in payload if item["status"] in {"queued", "running"}), None)
    return {"active": active, "recent": payload, "service": store.get_service_status()}


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
