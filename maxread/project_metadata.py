from __future__ import annotations

import re
from pathlib import Path


PROJECT_CATEGORIES = (
    "推理与系统",
    "训练与优化",
    "Agent 与检索",
    "视觉与多模态",
    "生成模型",
    "3D 与世界模型",
    "机器人",
    "其他",
)

_CATEGORY_KEYWORDS = (
    ("机器人", ("robot", "robotics", "manipulation", "locomotion", "embodied", "navigation", "机器人", "具身", "导航", "操控")),
    ("3D 与世界模型", ("world model", "3d", "three-dimensional", "nerf", "gaussian splat", "scene", "geometry", "世界模型", "三维", "场景", "几何")),
    ("Agent 与检索", ("agent", "agentic", "rag", "retrieval", "tool use", "reasoning agent", "智能体", "检索", "工具调用")),
    ("生成模型", ("diffusion", "flow matching", "video generation", "image generation", "autoregressive generation", "扩散模型", "流匹配", "视频生成", "图像生成")),
    ("视觉与多模态", ("vision", "visual", "multimodal", "video", "image", "vlm", "视觉", "多模态", "视频", "图像")),
    ("训练与优化", ("training", "optimizer", "optimization", "fine-tuning", "alignment", "distillation", "rlhf", "训练", "优化器", "微调", "对齐", "蒸馏")),
    ("推理与系统", ("inference", "serving", "attention", "transformer", "kv cache", "compression", "routing", "moe", "推理", "系统", "注意力", "缓存", "压缩", "路由")),
)


def one_sentence_summary(text: str, limit: int = 140) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    clean = re.sub(r"[*_`#]", "", clean)
    if not clean:
        return ""
    match = re.search(r"^(.+?[。！？.!?])(?:\s|$)", clean)
    sentence = match.group(1) if match else clean
    return sentence[:limit].rstrip("，,;；:： ")


def extract_project_summary(markdown: str, fallback: str = "") -> str:
    text = str(markdown or "")
    heading = re.search(r"(?m)^#\s+.+?[：:]\s*(.+?)\s*$", text)
    if heading:
        summary = one_sentence_summary(heading.group(1))
        if summary:
            return summary
    tldr = re.search(
        r"(?ms)^\*\*TL;DR\*\*\s*[：:]\s*(.+?)(?:\n\s*\n|\n#{1,6}\s|\Z)",
        text,
    )
    if tldr:
        summary = one_sentence_summary(tldr.group(1))
        if summary:
            return summary
    return one_sentence_summary(fallback)


def auto_project_category(title: str, summary: str = "") -> str:
    text = f"{title} {summary}".lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return category
    return "其他"


def load_generated_project_summary(workdir: Path, source_id: str) -> str:
    if not re.fullmatch(r"\d{4}\.\d{4,5}", str(source_id or "")):
        return ""
    root = Path(workdir) / "papers" / source_id / "pipeline_artifacts"
    candidates = [root / "05-final.md"]
    if root.is_dir():
        candidates.extend(sorted(root.glob("05-quality-*.md"), reverse=True))
        candidates.extend(sorted(root.glob("04-reviewed.md"), reverse=True))
        candidates.extend(sorted(root.glob("02-polished.md"), reverse=True))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            summary = extract_project_summary(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if summary:
            return summary
    return ""
