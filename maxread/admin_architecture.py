from __future__ import annotations

from pathlib import Path

from .workflow import transition, workflow_spec


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


EVENT_PRESENTATION = {
    "claim": ("认领成功", "Worker 通过原子租约成功认领排队任务。"),
    "fetch_started": ("开始取材", "Worker 开始下载并解析论文原文与 TeX source。"),
    "source_ready": ("材料就绪", "PDF、TeX source 和结构化材料满足生成约束。"),
    "source_missing": ("源码缺失", "require_source=true，但没有取得可用的 TeX source。"),
    "generation_started": ("开始生成", "材料检查通过，进入初稿生成。"),
    "generation_check_started": ("检查初稿", "模型返回候选稿，进入完整文档契约检查。"),
    "generation_repair_required": ("修复初稿", "完整性检查失败，且生成修复预算尚未耗尽。"),
    "generation_recheck": ("重检初稿", "修复稿生成完成，重新执行完整性检查。"),
    "draft_ready": ("初稿通过", "候选稿满足完整文档契约。"),
    "generation_incomplete": ("生成耗尽", "生成修复预算耗尽，文档仍不完整。"),
    "review_completed": ("审阅完成", "内容审阅完成；辅助审阅失败允许带 warning 降级继续。"),
    "quality_repair_required": ("修复格式", "发布前质检失败，且格式修复预算尚未耗尽。"),
    "quality_recheck": ("重检格式", "格式修复完成，重新规范化、编译并复检。"),
    "quality_passed": ("质检通过", "Markdown/XML 中不存在阻断发布的高严重度问题。"),
    "quality_rejected": ("质量拒绝", "修复预算耗尽，或发布后检查仍存在阻断问题。"),
    "publish_succeeded": ("发布完成", "飞书文档写入成功，并已持久化发布检查点。"),
    "resume_published": ("恢复已发布", "任务带有可复用的 doc_url 和发布检查点。"),
    "visual_qa_started": ("开始实页检查", "发布后回读通过，进入浏览器真实渲染检查。"),
    "visual_repair_required": ("修复实页", "视觉检查发现可定位问题，且视觉修复预算尚未耗尽。"),
    "visual_recheck": ("重检实页", "视觉修复完成，重新打开文档并截图复检。"),
    "complete": ("交付完成", "发布或视觉检查确认文档达到可交付条件。"),
    "fail": ("执行异常", "任意非终态发生未处理异常。"),
    "recover": ("租约恢复", "活动状态的 Worker 失联或租约超时，queued 除外。"),
    "retry": ("显式重试", "任务处于可重试终态，并收到 retry-job。"),
    "cancel": ("取消任务", "任意非终态收到取消请求。"),
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
    {
        "id": "source-blocked",
        "label": "源码阻断",
        "summary": "材料不足时停止在生成之前；补齐源码后由人工重试回到队列。",
        "states": ["fetching", "needs_source", "queued", "claimed"],
        "events": ["source_missing", "retry", "claim"],
    },
    {
        "id": "execution-failed",
        "label": "执行异常",
        "summary": "网络、模型或文件异常进入通用失败终态，确认根因后显式重试。",
        "states": ["generating", "failed", "queued", "claimed"],
        "events": ["fail", "retry", "claim"],
    },
    {
        "id": "published-recovery",
        "label": "发布后恢复",
        "summary": "已有发布检查点时，重试复用原飞书文档并直接回到发布后检查，不重复生成。",
        "states": [
            "publishing", "post_publish_checking", "quality_failed",
            "queued", "claimed", "post_publish_checking",
        ],
        "events": ["publish_succeeded", "quality_rejected", "retry", "claim", "resume_published"],
    },
]


HANDLING_TYPES = [
    {
        "id": "automatic",
        "label": "自动恢复",
        "summary": "系统能证明重放安全，自动回队列或复用已有检查点。",
    },
    {
        "id": "bounded",
        "label": "有界修复",
        "summary": "在固定预算内修复并复检，耗尽后进入可审计终态。",
    },
    {
        "id": "degraded",
        "label": "降级继续",
        "summary": "辅助步骤失败只记录告警，后续确定性质量门仍然执行。",
    },
    {
        "id": "manual",
        "label": "人工介入",
        "summary": "系统无法安全猜测下一步，保留证据并等待明确操作。",
    },
]


FAILURE_MODES = [
    {
        "id": "source-asset-render",
        "title": "源码有图，但图形格式无法渲染",
        "stage": "生成前材料",
        "trigger": "TeX 引用了 EPS/PDF/SVG 等资产，但对应转换器缺失、文件损坏或所有候选转换失败。",
        "handling": "manual",
        "outcome_state": "failed",
        "automatic": "停止在模型调用之前，并在错误中保存实际资产格式，例如 formats=.eps。",
        "next_action": "补齐受支持的渲染后端后显式重试；不得把“有图但转不出”降级成无图文档。",
        "side_effect": "未创建飞书文档",
    },
    {
        "id": "source-unavailable",
        "title": "原文或 TeX source 不可用",
        "stage": "获取原文",
        "trigger": "arXiv 限流、网络失败、源码包缺失或无法解压，且 require_source=true。",
        "handling": "manual",
        "outcome_state": "needs_source",
        "automatic": "停止在生成之前，保存 source summary 与解析告警，不创建飞书文档。",
        "next_action": "等待限流恢复，或用 import-source 补齐源码后在控制台重试。",
        "side_effect": "无外部文档副作用",
    },
    {
        "id": "generation-no-output",
        "title": "生成模型没有可用输出",
        "stage": "生成初稿",
        "trigger": "密钥、超时、网关或模型调用在所有 generation attempts 中持续失败。",
        "handling": "bounded",
        "outcome_state": "failed",
        "automatic": "按 generation_repair_rounds + 1 次调用预算重试，并保存每次异常。",
        "next_action": "检查模型配置与网关状态；恢复后 retry-job，任务重新进入队列。",
        "side_effect": "未创建飞书文档",
    },
    {
        "id": "generation-contract",
        "title": "初稿违反完整文档契约",
        "stage": "生成检查",
        "trigger": "缺 H1、章节不全、输出过短、提示词泄漏、代码围栏或关键图标记缺失。",
        "handling": "bounded",
        "outcome_state": "generation_incomplete",
        "automatic": "先做确定性修复，再携带原稿和精确错误让模型重写；所有原始输出落盘。",
        "next_action": "查看 generation attempt artifact；修正提示或模型后显式重试。",
        "side_effect": "未创建飞书文档",
    },
    {
        "id": "auxiliary-review",
        "title": "AI review 或关键图读图失败",
        "stage": "内容审阅",
        "trigger": "reviewer 或图像理解模型超时、拒答或返回不可解析内容。",
        "handling": "degraded",
        "outcome_state": "quality_checking",
        "automatic": "保留原 Markdown，把异常记为 warning，继续执行 Markdown/XML 确定性质检。",
        "next_action": "通常无需处理；若最终质量异常，可从 review warning 追溯辅助模型。",
        "side_effect": "无外部文档副作用",
    },
    {
        "id": "prepublish-quality",
        "title": "发布前公式、表格或结构质检失败",
        "stage": "发布前质检",
        "trigger": "Markdown/XML 出现高严重度公式、格式字符、表格或完整性问题。",
        "handling": "bounded",
        "outcome_state": "quality_failed",
        "automatic": "按 quality_repair_rounds 循环模型修复、重新规范化、重新编译并复检。",
        "next_action": "检查 05/06/07 quality artifacts；修正规则或模型后 retry-job。",
        "side_effect": "未创建飞书文档",
    },
    {
        "id": "image-publication",
        "title": "图片锚点、上传或移动失败",
        "stage": "写入飞书",
        "trigger": "marker 找不到、媒体上传失败、block_id 缺失、移动失败或 marker 删除失败。",
        "handling": "bounded",
        "outcome_state": "quality_failed",
        "automatic": "图片步骤尝试回滚；发布后计数和真实页面质检把残留问题升级为阻断告警。",
        "next_action": "重试会复用已保存的文档检查点；优先修复原文档，不重新生成正文。",
        "side_effect": "可能已有未交付的飞书文档",
    },
    {
        "id": "feishu-write",
        "title": "飞书创建或写入过程异常",
        "stage": "写入飞书",
        "trigger": "create/update/publish 命令持续失败，或媒体写入发生不可安全盲重试的错误。",
        "handling": "manual",
        "outcome_state": "failed",
        "automatic": "幂等写操作按错误类型退避重试；图片 append 避免盲重试并尽量回滚。",
        "next_action": "先检查是否留下部分文档；没有 publish checkpoint 时，确认清理后再重试。",
        "side_effect": "检查点前存在部分文档风险",
    },
    {
        "id": "postpublish-quality",
        "title": "发布后回读或真实页面质量失败",
        "stage": "发布后检查",
        "trigger": "标题、图片、公式、表格数量不足，或截图发现无效公式、裸格式字符和错位图片。",
        "handling": "bounded",
        "outcome_state": "quality_failed",
        "automatic": "按 block_id 做确定性修复，必要时调用模型修公式，并在每轮后重新截图。",
        "next_action": "预算耗尽后查看 visual QA 截图；retry-job 从原文档复检，不重跑正文生成。",
        "side_effect": "文档已存在但暂不交付",
    },
    {
        "id": "visual-runner",
        "title": "无头浏览器或视觉检查基础设施失败",
        "stage": "视觉检查",
        "trigger": "SSH、浏览器、登录态、runner 超时、无 JSON 或没有生成截图。",
        "handling": "manual",
        "outcome_state": "quality_failed",
        "automatic": "remote-error 被视为阻断，不会把未经真实渲染验证的文档交付。",
        "next_action": "恢复 runner、网络和登录态后重试；已有 checkpoint 时仍复用原文档。",
        "side_effect": "文档已存在但暂不交付",
    },
    {
        "id": "worker-lease",
        "title": "Worker 崩溃、重启或租约过期",
        "stage": "任意活动阶段",
        "trigger": "本机 PID 消失，或 heartbeat 超过 queue_stale_minutes。",
        "handling": "automatic",
        "outcome_state": "queued",
        "automatic": "原子 recover 回队列；新 worker 认领后，旧 worker 的迟到写入会因 worker_id 不匹配被拒绝。",
        "next_action": "通常无需人工操作；通过 job-events 检查 recover_dead_worker/recover_stale。",
        "side_effect": "有 checkpoint 时复用原文档",
    },
    {
        "id": "notification",
        "title": "结果已落库但飞书通知失败",
        "stage": "终态通知",
        "trigger": "回复消息失败、原消息不可回复、飞书网络异常或权限变化。",
        "handling": "manual",
        "outcome_state": "",
        "automatic": "任务终态保持不变，watcher 不标记 notified，并记录 notify_error 事件。",
        "next_action": "在控制台查看真实任务结果并人工补发；当前没有独立通知重试 worker。",
        "side_effect": "文档和任务结果不受影响",
    },
]


RECOVERY_RULES = [
    {
        "title": "发布前失败",
        "condition": "没有 publish checkpoint",
        "decision": "只有显式 retry 才回到 queued，重新执行材料和生成流程。",
        "guard": "未创建文档时可安全重跑；若写入阶段异常需先排查部分文档。",
    },
    {
        "title": "发布后失败",
        "condition": "job 保存 doc_url + checkpoint_json",
        "decision": "claimed 通过 resume_published 直接进入发布后检查，跳过模型与源码下载。",
        "guard": "复用同一文档，避免重复创建和重复消耗模型调用。",
    },
    {
        "title": "Worker 失联",
        "condition": "PID 消失或 heartbeat 过期",
        "decision": "自动 recover 到 queued，再由新 worker 原子认领。",
        "guard": "worker_id 租约隔离迟到结果，终态写入不会被旧进程覆盖。",
    },
    {
        "title": "辅助步骤失败",
        "condition": "review 或图像理解失败但核心材料仍完整",
        "decision": "记录 warning 后继续，让后续确定性质量门决定是否可交付。",
        "guard": "降级不绕过发布前质检和真实页面检查。",
    },
    {
        "title": "终态重试",
        "condition": "needs_source / generation_incomplete / quality_failed / failed",
        "decision": "retry 清空错误、重置 watcher 通知并回到 queued；cancelled 不允许重试。",
        "guard": "所有转移和原因写入 job_events，便于审计每次尝试。",
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
    for edge in spec["transitions"]:
        label, condition = EVENT_PRESENTATION[edge["event"]]
        edge.update({"label": label, "condition": condition})
    for policy in spec["policies"]:
        label, condition = EVENT_PRESENTATION[policy["event"]]
        policy.update(
            {
                "label": label,
                "condition": condition,
                "sources": [
                    state["id"]
                    for state in spec["states"]
                    if _transition_matches(state["id"], policy["event"], policy["to"])
                ],
            }
        )
    spec.update(
        {
            "scenarios": SCENARIOS,
            "handling_types": HANDLING_TYPES,
            "failure_modes": FAILURE_MODES,
            "recovery_rules": RECOVERY_RULES,
            "layers": LAYERS,
            "invariants": INVARIANTS,
            "metrics": {
                "states": len(spec["states"]),
                "transitions": len(spec["transitions"]),
                "repair_loops": 3,
                "retryable_terminals": sum(1 for state in spec["states"] if state["terminal"] and state["retryable"]),
                "failure_modes": len(FAILURE_MODES),
            },
        }
    )
    _validate_architecture_spec(spec)
    return spec


def _validate_architecture_spec(spec: dict) -> None:
    state_ids = {state["id"] for state in spec["states"]}
    missing = state_ids - set(STATE_PRESENTATION)
    if missing:
        raise RuntimeError(f"missing architecture metadata: {', '.join(sorted(missing))}")

    for scenario in spec["scenarios"]:
        states = scenario["states"]
        events = scenario["events"]
        if len(events) != len(states) - 1:
            raise RuntimeError(f"invalid scenario length: {scenario['id']}")
        unknown = set(states) - state_ids
        if unknown:
            raise RuntimeError(f"unknown scenario states in {scenario['id']}: {', '.join(sorted(unknown))}")
        for source, event, target in zip(states, events, states[1:]):
            actual = transition(source, event).to_state.value
            if actual != target:
                raise RuntimeError(f"invalid scenario edge: {source} + {event} -> {target}, actual={actual}")

    for edge in spec["transitions"]:
        if not edge.get("label") or not edge.get("condition"):
            raise RuntimeError(f"missing transition presentation: {edge['from']} + {edge['event']}")
    for policy in spec["policies"]:
        if not policy.get("label") or not policy.get("condition") or not policy.get("sources"):
            raise RuntimeError(f"missing policy presentation: {policy['event']}")
        for source in policy["sources"]:
            actual = transition(source, policy["event"]).to_state.value
            if actual != policy["to"]:
                raise RuntimeError(f"invalid policy edge: {source} + {policy['event']} -> {actual}")

    handling_ids = {item["id"] for item in spec["handling_types"]}
    failure_ids = [item["id"] for item in spec["failure_modes"]]
    if len(failure_ids) != len(set(failure_ids)):
        raise RuntimeError("duplicate failure mode id")
    for failure in spec["failure_modes"]:
        if failure["handling"] not in handling_ids:
            raise RuntimeError(f"unknown failure handling: {failure['id']}")
        outcome_state = failure.get("outcome_state")
        if outcome_state and outcome_state not in state_ids:
            raise RuntimeError(f"unknown failure outcome state: {failure['id']} -> {outcome_state}")


def _transition_matches(source: str, event: str, target: str) -> bool:
    try:
        return transition(source, event).to_state.value == target
    except ValueError:
        return False


def architecture_html() -> str:
    path = Path(__file__).with_name("static") / "architecture.html"
    return path.read_text(encoding="utf-8")
