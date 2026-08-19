from __future__ import annotations

import json
import re

from .feishu import progress_help_lines


HELP_PATTERNS = [
    r"^\s*/?help\s*$",
    r"帮助",
    r"使用说明",
    r"怎么用",
    r"如何使用",
    r"自我介绍",
    r"你是谁",
    r"读不动了",
    r"maxread",
]


def plain_message_text(content: str) -> str:
    text = str(content or "")
    try:
        payload = json.loads(text)
    except Exception:
        return text
    if isinstance(payload, dict):
        for key in ("text", "content"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(payload, ensure_ascii=False)
    return text


def should_send_intro(content: str) -> bool:
    text = plain_message_text(content).strip()
    if not text:
        return False
    return any(re.search(pattern, text, flags=re.I) for pattern in HELP_PATTERNS)


def intro_message(feedback_url: str = "") -> str:
    return (
        "📖 我是读不动了 / MaxRead，读不动了就来找我。\n\n"
        "目前支持：\n"
        "- arXiv ID：2604.12946\n"
        "- arXiv abs 链接：https://arxiv.org/abs/2604.12946\n"
        "- arXiv PDF 链接：https://arxiv.org/pdf/2506.13585.pdf\n"
        "- HuggingFace Papers 链接：https://huggingface.co/papers/2605.18739\n"
        "- 部分普通网页文章链接\n\n"
        "一条消息可以放多篇，我会并行处理并提示排队顺序。\n"
        "私聊我可以直接发链接；群聊里必须 @ 我，我才会处理。\n\n"
        "处理过程中，我会在你的原消息下添加飞书表情反应，不单独刷屏：\n"
        + "\n".join(progress_help_lines())
        + "\n\n"
        "完成后我会在原消息话题里回复飞书文档链接；失败时会回复原因。失败后直接在该话题回复“重试”即可再跑一次。\n\n"
        "意见反馈：请尽量回复对应文档链接那条消息，或在反馈里带上 arXiv ID / 飞书文档链接，比如“反馈 2604.12946：图太少”或“建议：支持 PDF URL”。这样我能定位是哪篇出了问题。\n\n"
        "暂不稳定/暂不支持：微信链接、任意 PDF URL、纯文本问答；SVG 网页图会跳过，避免飞书里出现裁剪或大白边。"
    )

def group_intro_message() -> str:
    return (
        "📖 我是读不动了 / MaxRead，你的论文阅读小助手。\n\n"
        "在群里用法：\n"
        "- 直接 @ 我并带链接：@读不动了 2604.12946\n"
        "- 或者在一条 arXiv 链接消息下开话题 @ 我：@读不动了 看看这个\n"
        "- 一条消息可以放多篇，我会分别排队生成文档。\n\n"
        "目前支持：\n"
        "- arXiv ID / abs / PDF 链接\n"
        "- HuggingFace Papers 链接\n"
        "- 部分普通网页文章\n\n"
        "我会在原消息下添加飞书表情反应，最终文档链接回复到原消息话题里：\n"
        + "\n".join(progress_help_lines())
        + "\n\n"
        "能力边界：\n"
        "- 群里不 @ 我不会处理。\n"
        "- 任务失败后，在对应话题直接回复“重试”即可，不需要重新贴链接或再次 @。\n"
        "- 话题里 @ 我时，只读取这个话题里的支持链接，不回溯整个群聊。\n"
        "- 暂不稳定/暂不支持：微信链接、任意 PDF URL、纯文本问答；SVG 网页图会跳过。\n"
        "- 需要稳定图片和公式时，arXiv source 优先。"
    )
