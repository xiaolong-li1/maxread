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
UNCLASSIFIED_CATEGORY = "已完成未分类"


def is_placeholder_project_title(title: str, source_id: str = "") -> bool:
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    paper_id = str(source_id or "").strip()
    if not value:
        return True
    if "标题尚未取得" in value or "title unavailable" in value.lower():
        return True
    normalized = re.sub(r"[\s:：\[\]()]+", " ", value.lower()).strip()
    if paper_id and normalized in {
        paper_id.lower(),
        f"arxiv {paper_id}".lower(),
        f"arxiv id {paper_id}".lower(),
    }:
        return True
    return bool(re.fullmatch(r"(?i)arxiv(?:\s+id)?\s*[:：]?\s*\d{4}\.\d{4,5}", value))

_CATEGORY_KEYWORDS = (
    ("机器人", ("robot", "robotics", "manipulation", "locomotion", "embodied", "navigation", "motion planning", "机器人", "具身", "导航", "操控")),
    ("3D 与世界模型", ("world model", "3d", "three-dimensional", "nerf", "gaussian splat", "point cloud", "monocular 3d", "scene reconstruction", "geometry", "世界模型", "三维", "点云", "场景", "几何")),
    ("Agent 与检索", ("agent", "agentic", "rag", "retrieval", "tool use", "reasoning agent", "智能体", "检索", "工具调用")),
    ("生成模型", ("diffusion", "flow matching", "video generation", "image generation", "autoregressive generation", "扩散模型", "流匹配", "视频生成", "图像生成")),
    ("视觉与多模态", ("vision", "visual", "multimodal", "video", "image", "vlm", "perception", "object detection", "segmentation", "视觉", "多模态", "视频", "图像", "感知", "检测", "分割")),
    ("训练与优化", ("training", "optimizer", "optimization", "fine-tuning", "alignment", "distillation", "rlhf", "self-supervised", "representation learning", "训练", "优化器", "微调", "对齐", "蒸馏", "自监督")),
    ("推理与系统", ("inference", "serving", "attention", "transformer", "language model", "llm", "neural network", "kv cache", "compression", "routing", "moe", "tensor", "推理", "系统", "注意力", "缓存", "压缩", "路由", "语言模型", "神经网络")),
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


def extract_project_context(markdown: str, fallback: str = "", limit: int = 1200) -> str:
    """Collect high-level opening evidence for project classification."""
    text = str(markdown or "")
    evidence = []
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if heading:
        title = re.sub(r"^\[[^]]+\]\s*", "", heading.group(1)).strip()
        if title:
            evidence.append(title)
    tldr = re.search(
        r"(?ms)^\*\*TL;DR\*\*\s*[：:]\s*(.+?)(?:\n\s*\n|\n#{1,6}\s|\Z)",
        text,
    )
    if tldr:
        evidence.append(re.sub(r"\s+", " ", tldr.group(1)).strip())
    opening = re.search(
        r"(?ms)^##\s+1(?:[.、]|\s).*?\n\s*\n(.+?)(?=\n\s*\n|^#{2,6}\s|\Z)",
        text,
    )
    if opening:
        paragraph = re.sub(r"\[MaxRead(?:Figure|Table):[^]]+\]", " ", opening.group(1))
        paragraph = re.sub(r"<latex>.*?</latex>|\|.*?\|", " ", paragraph, flags=re.S)
        paragraph = re.sub(r"[*_`#>]", "", paragraph)
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if paragraph:
            evidence.append(paragraph)
    fallback_text = one_sentence_summary(fallback, limit=400)
    if fallback_text:
        evidence.append(fallback_text)
    output = []
    seen = set()
    for item in evidence:
        clean = str(item or "").strip()
        if clean and clean not in seen:
            output.append(clean)
            seen.add(clean)
    return "\n".join(output)[: max(200, int(limit))]


def auto_project_category(title: str, summary: str = "") -> str:
    text = f"{title} {summary}".lower()
    scores = []
    for category, keywords in _CATEGORY_KEYWORDS:
        score = sum(2 if " " in keyword else 1 for keyword in keywords if keyword in text)
        scores.append((score, category))
    best_score, best_category = max(scores, default=(0, "其他"), key=lambda item: item[0])
    if best_score:
        return best_category
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


def load_generated_project_context(workdir: Path, source_id: str, fallback: str = "") -> str:
    if not re.fullmatch(r"\d{4}\.\d{4,5}", str(source_id or "")):
        return one_sentence_summary(fallback, limit=400)
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
            context = extract_project_context(
                path.read_text(encoding="utf-8", errors="replace"),
                fallback=fallback,
            )
        except OSError:
            continue
        if context:
            return context
    return one_sentence_summary(fallback, limit=400)
