from __future__ import annotations

from pathlib import Path

from .workflow import workflow_spec


STATE_PRESENTATION = {
    "queued": ("等待调度", "intake", "等待 worker 原子认领，尚未产生外部副作用。"),
    "claimed": ("已认领", "intake", "worker_id 持有任务租约，心跳开始更新。"),
    "fetching": ("获取原文", "intake", "下载 arXiv 元数据、PDF、源码与可渲染图片。"),
    "source_ready": ("材料就绪", "intake", "原文材料满足生成前置条件。"),
    "generating": ("生成初稿", "generation", "模型根据正文、公式和关键图片生成完整 Markdown。"),
    "generation_checking": ("生成检查", "generation", "检查 H1、章节、提示泄漏、长度与图片标记。"),
    "generation_repairing": ("修复生成", "generation", "先做确定性修复，必要时携带原稿和错误让模型重写。"),
    "reviewing": ("内容审阅", "review", "AI review 校对事实、结构、图片说明和表达。"),
    "quality_checking": ("发布前质检", "review", "同时检查 Markdown 与渲染后的 Docx XML。"),
    "quality_repairing": ("格式修复", "review", "基于精确告警修复公式、表格与格式化残留。"),
    "publishing": ("写入飞书", "delivery", "创建文档、写入 XML、插图并保存发布检查点。"),
    "post_publish_checking": ("发布后检查", "delivery", "回读真实飞书文档，核对标题、图、公式和表格数量。"),
    "visual_checking": ("视觉检查", "delivery", "无头浏览器检查真实渲染页面和章节截图。"),
    "visual_repairing": ("视觉修复", "delivery", "按 block_id 修复可定位问题，然后重新截图验证。"),
    "completed": ("完成", "terminal", "文档通过全部检查，结果可交付。"),
    "needs_source": ("缺少源码", "terminal", "源材料不足且策略要求源码，可补齐后重试。"),
    "generation_incomplete": ("生成不完整", "terminal", "有界生成修复耗尽，未发布文档，可重新排队。"),
    "quality_failed": ("质量未通过", "terminal", "格式或视觉问题超过修复预算，保留产物供审计。"),
    "failed": ("执行失败", "terminal", "网络、模型、文件或飞书调用发生未恢复异常。"),
    "cancelled": ("已取消", "terminal", "任务被显式取消，不再继续产生副作用。"),
}


SCENARIOS = [
    {
        "id": "happy",
        "label": "正常交付",
        "summary": "一次生成通过，各质量门顺序完成。",
        "states": [
            "queued", "claimed", "fetching", "source_ready", "generating",
            "generation_checking", "reviewing", "quality_checking", "publishing",
            "post_publish_checking", "visual_checking", "completed",
        ],
        "events": [
            "claim", "fetch_started", "source_ready", "generation_started",
            "generation_check_started", "draft_ready", "review_completed", "quality_passed",
            "publish_succeeded", "visual_qa_started", "complete",
        ],
    },
    {
        "id": "generation-repair",
        "label": "生成修复",
        "summary": "初稿不完整，确定性修复或带原稿重写后再次检查。",
        "states": [
            "generating", "generation_checking", "generation_repairing",
            "generation_checking", "reviewing",
        ],
        "events": [
            "generation_check_started", "generation_repair_required",
            "generation_recheck", "draft_ready",
        ],
    },
    {
        "id": "quality-repair",
        "label": "格式修复",
        "summary": "Markdown/XML 质检阻断发布，修复后重新渲染复检。",
        "states": ["reviewing", "quality_checking", "quality_repairing", "quality_checking", "publishing"],
        "events": ["review_completed", "quality_repair_required", "quality_recheck", "quality_passed"],
    },
    {
        "id": "visual-repair",
        "label": "视觉修复",
        "summary": "飞书真实页面出现无效公式或缺图，按页面 block 修复再截图。",
        "states": [
            "post_publish_checking", "visual_checking", "visual_repairing",
            "visual_checking", "completed",
        ],
        "events": ["visual_qa_started", "visual_repair_required", "visual_recheck", "complete"],
    },
    {
        "id": "worker-recovery",
        "label": "Worker 恢复",
        "summary": "租约失效或心跳过期，原子回到队列并由新 worker 认领。",
        "states": ["claimed", "fetching", "queued", "claimed", "fetching"],
        "events": ["fetch_started", "recover", "claim", "fetch_started"],
    },
    {
        "id": "terminal-retry",
        "label": "失败重试",
        "summary": "有界修复耗尽后进入可重试终态，由明确操作重新排队。",
        "states": [
            "generation_checking", "generation_repairing", "generation_checking",
            "generation_incomplete", "queued",
        ],
        "events": [
            "generation_repair_required", "generation_recheck",
            "generation_incomplete", "retry",
        ],
    },
]


LAYERS = [
    {"name": "入口层", "modules": ["cli.py", "sources.py"], "detail": "解析飞书消息、arXiv ID 与网页输入；只负责入队，不执行长任务。"},
    {"name": "耐久队列", "modules": ["job_queue.py", "db.py"], "detail": "原子认领、租约心跳、去重、状态版本与事件审计。"},
    {"name": "论文流水线", "modules": ["pipeline.py", "arxiv.py", "render.py"], "detail": "材料提取、关键图选择、生成、规范化与 Docx XML 编译。"},
    {"name": "质量门", "modules": ["quality.py", "quality_repair.py", "visual_qa.py"], "detail": "确定性规则先行，模型修复有界，真实页面渲染是最终依据。"},
    {"name": "交付层", "modules": ["publishing.py", "feishu.py"], "detail": "文档写入、图片锚点替换、发布检查点与结果通知。"},
]


INVARIANTS = [
    {"title": "单一状态来源", "detail": "业务状态只由 workflow.py 的 transition() 推进；stage 仅用于进度展示。"},
    {"title": "副作用有边界", "detail": "发布成功先持久化 checkpoint；崩溃恢复只复检原文档，不重复创建。"},
    {"title": "修复必须有界", "detail": "生成、格式和视觉修复都有独立预算；耗尽后进入可审计终态。"},
    {"title": "租约隔离旧 Worker", "detail": "所有状态写入校验 worker_id，失效 worker 不能覆盖新 worker 的结果。"},
    {"title": "真实渲染优先", "detail": "Markdown/XML 只是中间表示，最终以飞书回读和浏览器截图判定交付。"},
]


def architecture_spec() -> dict:
    spec = workflow_spec()
    missing = {state["id"] for state in spec["states"]} - set(STATE_PRESENTATION)
    if missing:
        raise RuntimeError(f"missing architecture metadata: {', '.join(sorted(missing))}")
    for state in spec["states"]:
        label, phase, detail = STATE_PRESENTATION[state["id"]]
        state.update({"label": label, "phase": phase, "detail": detail})
    spec.update(
        {
            "scenarios": SCENARIOS,
            "layers": LAYERS,
            "invariants": INVARIANTS,
            "metrics": {
                "states": len(spec["states"]),
                "transitions": len(spec["transitions"]),
                "repair_loops": 3,
                "retryable_terminals": sum(1 for state in spec["states"] if state["terminal"] and state["retryable"]),
            },
        }
    )
    return spec


def architecture_html() -> str:
    path = Path(__file__).with_name("static") / "architecture.html"
    return path.read_text(encoding="utf-8")
