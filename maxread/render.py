from __future__ import annotations

import html
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .models import PaperBundle, PaperFigure


PREFERRED_FIGURE_NAMES = [
    "introfig",
    "fig1",
    "main",
    "framework",
    "overall",
    "blade",
    "workflow",
    "asav3",
    "mask_generation",
    "teaser",
    "attn_proportion",
    "proportion",
    "flops",
    "overview",
    "pipeline",
    "method",
    "architecture",
    "attention",
    "heatmap",
    "patterns",
    "ablation",
    "scaling",
    "warmup",
]


def polish_markdown(markdown: str) -> str:
    markdown = markdown.replace("<br/>", "\n")
    markdown = _normalize_display_math(markdown)
    markdown = _normalize_inline_math(markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip() + "\n"


def remove_false_material_warning(markdown: str, bundle: PaperBundle) -> str:
    has_evidence = bool(bundle.source_tables or bundle.source_figures or bundle.source_captions)
    if not has_evidence:
        return markdown
    lines = markdown.splitlines()
    kept: List[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if "材料不足" in stripped:
            skip = True
            continue
        if skip and not stripped:
            skip = False
            continue
        if skip and (stripped.startswith("#") or stripped.startswith("**TL;DR**") or stripped.startswith("|")):
            skip = False
        if not skip:
            kept.append(line)
    return "\n".join(kept).strip() + "\n"


def figure_placeholders(figures: List[Tuple[Path, str]]) -> List[Tuple[str, Path, str]]:
    inserts: List[Tuple[str, Path, str]] = []
    for index, (path, caption) in enumerate(figures, start=1):
        inserts.append((f"[MaxReadFigure:{index}:{path.stem}]", path, caption))
    return inserts


def figure_prompt_lines(inserts: List[Tuple[str, Path, str]]) -> List[str]:
    lines = []
    for marker, path, caption in inserts:
        lines.append(f"- {marker} 文件：{path.as_posix()} caption：{caption or path.stem}")
    return lines


def markdown_to_docx_xml(markdown: str) -> str:
    blocks = _markdown_blocks(markdown)
    xml_parts: List[str] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        stripped = block.strip()
        if not stripped:
            i += 1
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            xml_parts.append("<hr/>")
        elif match := re.match(r"^(#{1,6})\s+(.+)$", stripped):
            level = min(len(match.group(1)), 6)
            if not xml_parts and level == 1:
                xml_parts.append(f"<title>{_inline_xml(match.group(2))}</title>")
            xml_parts.append(f"<h{level}>{_inline_xml(match.group(2))}</h{level}>")
        elif match := re.match(r"^(#{1,6})\s+([^\n]+)\n(.+)$", stripped, flags=re.S):
            level = min(len(match.group(1)), 6)
            if not xml_parts and level == 1:
                xml_parts.append(f"<title>{_inline_xml(match.group(2))}</title>")
            xml_parts.append(f"<h{level}>{_inline_xml(match.group(2))}</h{level}>")
            rest = re.sub(r"\n+", "<br/>", match.group(3).strip())
            if rest:
                xml_parts.append(f"<p>{_inline_xml(rest)}</p>")
        elif _is_latex_block(stripped):
            xml_parts.append(f"<p>{_inline_xml(stripped)}</p>")
        elif _is_table_block(block):
            xml_parts.append(_table_xml(block))
        elif _is_unordered_list(block):
            xml_parts.append(_list_xml(block, ordered=False))
        elif _is_ordered_list(block):
            xml_parts.append(_list_xml(block, ordered=True))
        else:
            text = re.sub(r"\n+", "<br/>", stripped)
            xml_parts.append(f"<p>{_inline_xml(text)}</p>")
        i += 1
    return "".join(xml_parts)


def append_figure_placeholders(markdown: str, figures: List[Tuple[Path, str]]) -> Tuple[str, List[Tuple[str, Path, str]]]:
    if not figures:
        return markdown, []
    inserts: List[Tuple[str, Path, str]] = []
    lines = ["", "---", "", "## 附：关键图表", ""]
    for index, (path, caption) in enumerate(figures, start=1):
        marker = f"[MaxReadFigure:{index}:{path.stem}]"
        title = path.stem.replace("_", " ")
        lines.append(f"**图 {index}：{title}**")
        lines.append(marker)
        if caption:
            lines.append(f"图解：{caption}")
        lines.append("")
        inserts.append((marker, path, caption))
    return markdown.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", inserts


def ensure_figure_markers(markdown: str, inserts: List[Tuple[str, Path, str]], max_missing_append: int = 0) -> str:
    """Keep image anchors inline when the model uses them; optionally append a few missing ones."""
    if not inserts:
        return markdown
    missing = [(marker, path, caption) for marker, path, caption in inserts if marker not in markdown]
    if not missing or max_missing_append <= 0:
        return markdown
    missing = missing[:max_missing_append]
    lines = [markdown.rstrip(), "", "## 图表补充", ""]
    for marker, path, caption in missing:
        title = path.stem.replace("_", " ")
        lines.append(f"**{title}**")
        lines.append(marker)
        if caption:
            lines.append(f"图解：{caption}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def ensure_priority_figure_markers(markdown: str, inserts: List[Tuple[str, Path, str]], max_missing: int = 2) -> str:
    """Insert missing high-value overview/method figures near method text before review."""
    if not inserts or max_missing <= 0:
        return markdown
    missing = [(marker, path, caption) for marker, path, caption in inserts if marker not in markdown and _is_priority_figure(path, caption)]
    if not missing:
        return markdown
    lines = markdown.rstrip().splitlines()
    insert_at = _priority_figure_insert_index(lines)
    block: List[str] = []
    for marker, path, caption in missing[:max_missing]:
        if block:
            block.append("")
        block.append("这张图概括了论文中最关键的流程或方法结构。")
        block.append(marker)
        if caption:
            block.append(f"**图：{_short_caption(caption)}**")
    return "\n".join(lines[:insert_at] + [""] + block + [""] + lines[insert_at:]).strip() + "\n"


def _priority_figure_insert_index(lines: List[str]) -> int:
    patterns = ("## 3.", "## 方法", "## 2.", "## 核心", "## 关键")
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if any(stripped.startswith(pattern) for pattern in patterns):
            return idx + 1
    for idx, line in enumerate(lines):
        if line.strip().startswith("## "):
            return idx + 1
    return min(len(lines), 8)


def _is_priority_figure(path: Path, caption: str = "") -> bool:
    text = f"{path.stem} {caption}".lower()
    keywords = (
        "introfig", "fig1", "main", "overview", "pipeline", "workflow",
        "framework", "architecture", "model", "method", "teaser", "overall",
    )
    return any(keyword in text for keyword in keywords)


def _short_caption(caption: str, max_chars: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(caption or "")).strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "..."


def remove_figure_markers(markdown: str, markers: Iterable[str]) -> str:
    for marker in markers:
        markdown = markdown.replace(marker, "")
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip() + "\n"



def display_caption(caption: str, image_path: Path | str | None = None, max_chars: int = 36) -> str:
    text = _strip_common_text_macros(str(caption or ""))
    text = re.sub(r"\\([A-Za-z]{3,})(?![A-Za-z])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" .。:：;-—")
    if not text and image_path:
        text = Path(image_path).stem.replace("_", " ")
    label = _caption_label(text) or text
    if _looks_english(label):
        label = _caption_keyword_title(label, image_path)
    if len(label) > max_chars:
        label = label[:max_chars].rstrip() + "..."
    return label or "图"


def _caption_label(text: str) -> str:
    match = re.match(r"(?is)^\s*(?:fig(?:ure)?\.?\s*\d*[:：.-]?\s*)?(?:\([a-z]\)[:：]?\s*)?([^.;。；]{4,80})", text)
    return match.group(1).strip() if match else ""


def _caption_keyword_title(text: str, image_path: Path | str | None = None) -> str:
    lower = text.lower()
    mapping = [
        ("architecture", "方法架构图"),
        ("overview", "整体设计图"),
        ("attention", "注意力对比图"),
        ("ablation", "消融实验图"),
        ("speed", "速度与性能对比图"),
        ("memory", "显存与性能对比图"),
        ("perplexity", "困惑度结果图"),
        ("comparison", "对比结果图"),
        ("qualitative", "定性对比图"),
    ]
    for key, title in mapping:
        if key in lower:
            return title
    if image_path:
        stem = Path(image_path).stem.replace("_", " ").replace("-", " ").strip()
        if stem:
            return stem
    return "关键图表"


def _looks_english(text: str) -> bool:
    letters = len(re.findall(r"[A-Za-z]", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return letters > max(8, cjk * 2)



def _strip_common_text_macros(text: str) -> str:
    text = re.sub(r"\\(?:textsc|textbf|textit|emph|mathrm|mathbf|mathtt)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:xspace|NB|DX|lpk|wx|qz)(?![A-Za-z])(?:\s*\{([^{}]*)\})?", lambda m: m.group(1) or "", text)
    text = re.sub(r"\\(?:citep?|ref|label|url|href)\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\([A-Za-z]{3,})(?![A-Za-z])", r"\1", text)
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _normalize_display_math(markdown: str) -> str:
    def repl(match: re.Match[str]) -> str:
        body = re.sub(r"\s+", " ", match.group(1).strip())
        body = body.replace("\\\\", r"\\")
        return f"\n<latex>{body}</latex>\n"

    markdown = re.sub(r"\$\$\s*(.*?)\s*\$\$", repl, markdown, flags=re.S)
    # Guard against model output like '# $$ ... $$', which Feishu markdown may
    # interpret as headings before converting math.
    markdown = re.sub(r"(?m)^#+\s*(<latex>.*?</latex>)\s*$", r"\1", markdown)
    return markdown


def _markdown_blocks(markdown: str) -> List[str]:
    blocks: List[str] = []
    current: List[str] = []
    for line in markdown.splitlines():
        if line.strip():
            current.append(line.rstrip())
            continue
        if current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def _is_latex_block(text: str) -> bool:
    return bool(re.fullmatch(r"<latex>.*?</latex>", text, flags=re.S))


def _is_table_block(block: str) -> bool:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    return len(lines) >= 2 and all(line.startswith("|") and line.endswith("|") for line in lines[:2]) and re.fullmatch(r"\|[\s:|\-]+\|", lines[1]) is not None


def _is_unordered_list(block: str) -> bool:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    return bool(lines) and all(re.match(r"^[-*+]\s+", line) for line in lines)


def _is_ordered_list(block: str) -> bool:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    return bool(lines) and all(re.match(r"^\d+[.)]\s+", line) for line in lines)


def _list_xml(block: str, ordered: bool) -> str:
    tag = "ol" if ordered else "ul"
    items = []
    pattern = r"^\d+[.)]\s+" if ordered else r"^[-*+]\s+"
    for line in block.splitlines():
        text = re.sub(pattern, "", line.strip())
        items.append(f"<li>{_inline_xml(text)}</li>")
    return f"<{tag}>" + "".join(items) + f"</{tag}>"


def _table_xml(block: str) -> str:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    rows = [lines[0]] + lines[2:]
    out = ["<table>"]
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        out.append("<tr>" + "".join(f"<td><p>{_inline_xml(cell)}</p></td>" for cell in cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _inline_xml(text: str) -> str:
    placeholders: List[str] = []

    def hold(value: str) -> str:
        placeholders.append(value)
        return f"@@MAXREAD_XML_{len(placeholders) - 1}@@"

    text = re.sub(r"<latex>(.*?)</latex>", lambda m: hold(f"<latex>{html.escape(m.group(1), quote=False)}</latex>"), text, flags=re.S)
    text = re.sub(r"<br\s*/?>", lambda m: hold("<br/>"), text)
    text = re.sub(r"`([^`]+)`", lambda m: hold(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)
    for index, value in enumerate(placeholders):
        text = text.replace(f"@@MAXREAD_XML_{index}@@", value)
    return text


def _normalize_inline_math(markdown: str) -> str:
    def repl(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        if not body:
            return match.group(0)
        return f"<latex>{body}</latex>"

    return re.sub(r"(?<!\$)\$([^\n$]{1,240})\$(?!\$)", repl, markdown)


def prepare_key_figures(bundle: PaperBundle, max_figures: int = 8) -> List[Tuple[Path, str]]:
    if not bundle.source_dir:
        return []
    output_dir = bundle.source_dir.parent / "rendered_figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped = _grouped_figure_items(bundle, output_dir)
    skipped_assets = {path.resolve() for path, _caption in grouped.get("skip", [])}
    candidates: List[Tuple[Tuple[int, int, str], Path, str]] = []
    for path, caption, rank in grouped.get("figures", []):
        candidates.append((rank, path, caption))

    assets = _ranked_figure_assets(bundle)
    assets = [path for path in assets if path.exists() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf"}]
    for path in assets:
        if path.resolve() in skipped_assets:
            continue
        caption = _caption_for_asset(path, bundle.source_figures, bundle.source_captions, bundle.source_dir)
        figure = _figure_for_asset(path, bundle.source_figures, bundle.source_dir)
        candidates.append((_figure_rank(path, figure), path, caption))

    candidates.sort(key=lambda item: item[0])
    figures: List[Tuple[Path, str]] = []
    seen_paths = set()
    for _rank, path, caption in candidates:
        if path.resolve() in seen_paths:
            continue
        rendered = _render_asset(path, output_dir)
        if not rendered:
            continue
        figures.append((rendered, caption))
        seen_paths.add(path.resolve())
        if len(figures) >= max_figures:
            break
    return figures



def _grouped_figure_items(bundle: PaperBundle, output_dir: Path) -> dict[str, list]:
    if not bundle.source_dir:
        return {"figures": [], "skip": []}
    by_key: dict[tuple[str, int, str, str], List[PaperFigure]] = defaultdict(list)
    for figure in bundle.source_figures:
        if not figure.label or not figure.caption:
            continue
        by_key[(figure.tex_file, figure.figure_index, figure.label, figure.caption)].append(figure)

    figures: List[Tuple[Path, str, Tuple[int, int, str]]] = []
    skip: List[Tuple[Path, str]] = []
    for (_tex_file, _figure_index, label, caption), group in by_key.items():
        if len(group) < 2:
            continue
        items: List[Tuple[Path, PaperFigure]] = []
        for figure in sorted(group, key=lambda item: (item.row, item.col, item.asset_index)):
            path = bundle.source_dir / figure.asset
            if not path.exists() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf"}:
                continue
            rendered = _render_asset(path, output_dir)
            if rendered:
                items.append((rendered, figure))
        if len(items) < 2 or len(items) > 16:
            continue
        output_path = output_dir / f"{_safe_stem(label)}.png"
        if _should_compose_as_grid(items):
            composed = _compose_grid_figure(items, output_path, caption)
        else:
            composed = _compose_horizontal_figure([path for path, _figure in items], output_path, caption)
        if composed:
            ranks = [_figure_rank(path, figure) for path, figure in items]
            rank = min(ranks) if ranks else (20, 0, str(composed))
            figures.append((composed, caption, rank))
            skip.extend((bundle.source_dir / figure.asset, caption) for _path, figure in items)
    return {"figures": figures, "skip": skip}

def _compose_horizontal_figure(paths: List[Path], output_path: Path, caption: str = "") -> Optional[Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    images = []
    try:
        for path in paths:
            image = Image.open(path).convert("RGBA")
            images.append(image)
        target_height = min(max(max(image.height for image in images), 720), 1200)
        resized = []
        for image in images:
            width = max(1, round(image.width * target_height / image.height))
            resized.append(image.resize((width, target_height), Image.LANCZOS))
        labels = _side_labels_from_caption(caption, len(resized))
        gap = max(24, target_height // 30)
        padding = gap
        label_height = 58 if labels else 0
        width = sum(image.width for image in resized) + gap * (len(resized) - 1) + padding * 2
        height = target_height + padding * 2 + label_height
        canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        draw = ImageDraw.Draw(canvas) if labels else None
        font = _figure_label_font() if labels else None
        x = padding
        for index, image in enumerate(resized):
            if labels and draw and font:
                label = labels[index]
                bbox = draw.textbbox((0, 0), label, font=font)
                text_x = x + max(0, (image.width - (bbox[2] - bbox[0])) // 2)
                draw.text((text_x, padding // 2), label, fill=(20, 20, 20, 255), font=font)
            canvas.alpha_composite(image, (x, padding + label_height))
            x += image.width + gap
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path)
        return output_path
    except Exception:
        return None




def _should_compose_as_grid(items: List[Tuple[Path, PaperFigure]]) -> bool:
    if len(items) > 4:
        return True
    rows = {figure.row for _path, figure in items}
    return len(rows) > 1


def _compose_grid_figure(items: List[Tuple[Path, PaperFigure]], output_path: Path, caption: str = "") -> Optional[Path]:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    try:
        layout = _grid_layout(items)
        if not layout:
            return None
        rows = max(row for _path, _figure, row, _col in layout) + 1
        cols = max(col for _path, _figure, _row, col in layout) + 1
        if rows <= 0 or cols <= 0 or rows * cols > 24:
            return None

        opened = []
        for path, figure, row, col in layout:
            opened.append((Image.open(path).convert("RGBA"), path, figure, row, col))
        cell_width = _grid_cell_width(cols)
        resized = []
        row_heights = [1] * rows
        for image, path, figure, row, col in opened:
            width = cell_width
            height = max(1, round(image.height * width / image.width))
            scaled = image.resize((width, height), Image.LANCZOS)
            resized.append((scaled, path, figure, row, col))
            row_heights[row] = max(row_heights[row], height)

        col_labels = _grid_column_labels([(path, figure, row, col) for _image, path, figure, row, col in opened], cols)
        row_labels = _grid_row_labels([(path, figure, row, col) for _image, path, figure, row, col in opened], rows)
        font = _figure_label_font()
        gap = 18 if cols >= 4 else 24
        padding = gap
        header_height = 48 if col_labels else 0
        row_label_width = 128 if row_labels else 0
        width = row_label_width + cols * cell_width + (cols - 1) * gap + padding * 2
        height = header_height + sum(row_heights) + (rows - 1) * gap + padding * 2
        canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        draw = ImageDraw.Draw(canvas)

        if col_labels and font:
            x = padding + row_label_width
            for label in col_labels:
                _draw_centered_text(draw, label, x, padding // 2, cell_width, header_height, font)
                x += cell_width + gap

        y = padding + header_height
        by_cell = {(row, col): image for image, _path, _figure, row, col in resized}
        for row in range(rows):
            if row_labels and font:
                _draw_centered_text(draw, row_labels[row], padding, y, row_label_width - gap, row_heights[row], font)
            x = padding + row_label_width
            for col in range(cols):
                image = by_cell.get((row, col))
                if image:
                    image_y = y + max(0, (row_heights[row] - image.height) // 2)
                    canvas.alpha_composite(image, (x, image_y))
                x += cell_width + gap
            y += row_heights[row] + gap

        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path)
        return output_path
    except Exception:
        return None


def _grid_layout(items: List[Tuple[Path, PaperFigure]]) -> List[Tuple[Path, PaperFigure, int, int]]:
    sorted_items = sorted(items, key=lambda item: (item[1].row, item[1].col, item[1].asset_index))
    coords = [(figure.row, figure.col) for _path, figure in sorted_items]
    if len(set(coords)) == len(coords) and (max(row for row, _col in coords) > 0 or max(col for _row, col in coords) > 0):
        return [(path, figure, figure.row, figure.col) for path, figure in sorted_items]
    count = len(sorted_items)
    cols = _infer_grid_columns([path for path, _figure in sorted_items], count)
    return [(path, figure, index // cols, index % cols) for index, (path, figure) in enumerate(sorted_items)]


def _infer_grid_columns(paths: List[Path], count: int) -> int:
    if count <= 4:
        return count
    if count % 4 == 0:
        return 4
    if count % 3 == 0:
        return 3
    if count % 5 == 0:
        return 5
    return min(4, count)


def _grid_cell_width(cols: int) -> int:
    if cols <= 1:
        return 1200
    if cols == 2:
        return 760
    if cols == 3:
        return 520
    return 400


def _grid_column_labels(layout: List[Tuple[Path, PaperFigure, int, int]], cols: int) -> List[str]:
    by_col = [[path for path, _figure, _row, col in layout if col == index] for index in range(cols)]
    parent_labels = [_common_parent_label(paths) for paths in by_col]
    if _usable_label_set(parent_labels, cols):
        return [_pretty_grid_label(label, parent_labels) for label in parent_labels]
    stem_labels = [_common_stem_label(paths) for paths in by_col]
    if _usable_label_set(stem_labels, cols):
        return [_pretty_grid_label(label, stem_labels) for label in stem_labels]
    return []


def _grid_row_labels(layout: List[Tuple[Path, PaperFigure, int, int]], rows: int) -> List[str]:
    by_row = [[path for path, _figure, row, _col in layout if row == index] for index in range(rows)]
    stem_labels = [_common_stem_label(paths) for paths in by_row]
    if _usable_label_set(stem_labels, rows):
        return [_pretty_grid_label(label, stem_labels) for label in stem_labels]
    parent_labels = [_common_parent_label(paths) for paths in by_row]
    if _usable_label_set(parent_labels, rows):
        return [_pretty_grid_label(label, parent_labels) for label in parent_labels]
    return []


def _common_stem_label(paths: List[Path]) -> str:
    stems = {path.stem for path in paths}
    return next(iter(stems)) if len(stems) == 1 else ""


def _common_parent_label(paths: List[Path]) -> str:
    parents = {path.parent.name for path in paths}
    if len(parents) == 1:
        return next(iter(parents))
    for depth in range(2, 5):
        names = set()
        for path in paths:
            try:
                names.add(path.parts[-depth])
            except IndexError:
                pass
        if len(names) == 1:
            return next(iter(names))
    return ""


def _usable_label_set(labels: List[str], expected: int) -> bool:
    labels = [label for label in labels if label]
    return len(labels) == expected and len(set(labels)) == expected


def _pretty_grid_label(label: str, siblings: List[str]) -> str:
    raw = label.strip()
    lower = raw.lower()
    sibling_lowers = {item.lower() for item in siblings}
    if lower == "baseline" and {"asa", "sta", "svg"}.issubset(sibling_lowers):
        return "FA2"
    aliases = {
        "asa": "ASA",
        "sta": "STA",
        "svg": "SVG",
        "fa2": "FA2",
        "gt": "GT",
    }
    if lower in aliases:
        return aliases[lower]
    cleaned = _strip_artifact_label(raw)
    text = re.sub(r"(?i)_?extracted_?", " ", cleaned)
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"(?i)frame\s*(\d+)", r"frame ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 20:
        text = text[:18].rstrip() + "..."
    return text or raw


def _strip_artifact_label(label: str) -> str:
    text = label.strip()
    text = re.sub(r"(?i)^(?:final[_-]*)?triplet[_-]*no[_-]*labels?[_-]*", "", text)
    text = re.sub(r"(?i)^no[_-]*labels?[_-]*", "", text)
    text = re.sub(r"(?i)^(?:final|figure|image|img|pic|rendered)[_-]+", "", text)
    text = re.sub(r"(?i)[_-]+(?:final|no[_-]*labels?)$", "", text)
    return text.strip(" _-") or label

def _draw_centered_text(draw, label: str, x: int, y: int, width: int, height: int, font) -> None:
    bbox = draw.textbbox((0, 0), label, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    draw.text((x + max(0, (width - text_width) // 2), y + max(0, (height - text_height) // 2)), label, fill=(25, 25, 25, 255), font=font)


def _side_labels_from_caption(caption: str, count: int) -> List[str]:
    if count != 2:
        return []
    matches = re.findall(r"(?:\((?:left|right|a|b)\)|\b(?:left|right)\b)\s*[:：]?\s*([^.;。；]+)", caption or "", flags=re.I)
    labels = []
    for item in matches:
        label = re.sub(r"\s+", " ", item).strip()
        label = re.split(r"\b(?:consists?|show(?:s|ing)?|display(?:s|ing)?)\b|[,，:：]", label, maxsplit=1, flags=re.I)[0].strip()
        if len(label) > 42:
            label = label[:39].rstrip() + "..."
        labels.append(label)
    return labels[:2] if len(labels) >= 2 else []


def _figure_label_font():
    try:
        from PIL import ImageFont
        for path in [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]:
            if Path(path).exists():
                return ImageFont.truetype(path, 32)
        return ImageFont.load_default()
    except Exception:
        return None


def _safe_stem(text: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return stem.strip("._-") or "figure_group"


def _figure_for_asset(path: Path, figures: List[PaperFigure], source_dir: Optional[Path] = None) -> Optional[PaperFigure]:
    rel = ""
    if source_dir:
        try:
            rel = path.resolve().relative_to(source_dir.resolve()).as_posix().lower()
        except ValueError:
            rel = ""
    stem = path.stem.lower()
    for figure in figures:
        if rel and Path(figure.asset).as_posix().lower() == rel:
            return figure
    for figure in figures:
        if Path(figure.asset).stem.lower() == stem:
            return figure
    return None


def _ranked_figure_assets(bundle: PaperBundle) -> List[Path]:
    assert bundle.source_dir is not None
    ranked = []
    seen = set()
    label_counts = {}
    for figure in bundle.source_figures:
        path = bundle.source_dir / figure.asset
        if not path.exists() or path in seen:
            continue
        label = figure.label or ""
        max_for_label = 1 if _is_gallery_figure(path, figure) else 2
        if label and label_counts.get(label, 0) >= max_for_label:
            continue
        ranked.append((_figure_rank(path, figure), path))
        seen.add(path)
        if label:
            label_counts[label] = label_counts.get(label, 0) + 1
    fallback = [bundle.source_dir / asset for asset in bundle.source_assets]
    fallback = [path for path in fallback if path.exists() and path not in seen]
    ranked.extend((_figure_rank(path, None), path) for path in fallback)
    ranked.sort(key=lambda item: item[0])
    return [path for _rank, path in ranked]


def _figure_rank(path: Path, figure: Optional[PaperFigure]) -> Tuple[int, int, str]:
    text = " ".join(part.lower() for part in path.parts) + " " + path.stem.lower()
    label = (figure.label or "").lower() if figure else ""
    tex_file = (figure.tex_file or "").lower() if figure else ""
    caption = (figure.caption or "").lower() if figure else ""
    rank = 60
    if _is_appendix_asset(path, figure):
        rank = 75
    elif _is_gallery_figure(path, figure):
        rank = 80
    elif "workflow" in label or "workflow" in text or "blade" in text:
        rank = 0
    elif "mask_generation" in label or "asav3" in text:
        rank = 1
    elif "model-arch" in label or "architecture" in label or "overview" in text or "architecture" in text:
        rank = 4
    elif "mask_visualization" in label:
        rank = 12
    elif "ssim_compare" in label:
        rank = 35
    elif tex_file:
        rank = 10
    if figure and not _is_appendix_asset(path, figure):
        rank = min(rank, 6 + min(max(int(figure.figure_index), 0), 20))
    for i, preferred in enumerate(PREFERRED_FIGURE_NAMES):
        if preferred in text or preferred in label or preferred in caption:
            preferred_rank = 2 + i
            if _is_appendix_asset(path, figure):
                preferred_rank += 70
            rank = min(rank, preferred_rank)
    return rank, _frame_number(path), str(path)


def _is_appendix_asset(path: Path, figure: Optional[PaperFigure]) -> bool:
    parts = {part.lower() for part in path.parts}
    label = (figure.label or "").lower() if figure else ""
    tex_file = (figure.tex_file or "").lower() if figure else ""
    return "appendix" in parts or "appendix" in label or "appendix" in tex_file


def _is_gallery_figure(path: Path, figure: Optional[PaperFigure]) -> bool:
    text = "/".join(part.lower() for part in path.parts)
    label = (figure.label or "").lower() if figure else ""
    return (
        "extracted" in text
        or "/baseline" in text
        or "/asa/" in text
        or "/sta/" in text
        or "/svg/" in text
        or re.match(r"frame_\d+$", path.stem.lower()) is not None
        or label.startswith("fig:wan_")
        or label.startswith("fig:cogvideo_")
    )


def _frame_number(path: Path) -> int:
    match = re.search(r"frame_(\d+)", path.stem.lower())
    return int(match.group(1)) if match else 0


def _render_asset(path: Path, output_dir: Path) -> Optional[Path]:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        return path
    if suffix != ".pdf":
        return None
    out_png = output_dir / f"{path.stem}.png"
    if out_png.exists() and out_png.stat().st_size > 0:
        return out_png
    qlmanage = shutil.which("qlmanage")
    if qlmanage:
        tmp_dir = output_dir / f"{path.stem}_thumb"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [qlmanage, "-t", "-s", "1400", "-o", str(tmp_dir), str(path)],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        generated = list(tmp_dir.glob("*.png")) if result.returncode == 0 else []
        if generated:
            shutil.copyfile(generated[0], out_png)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return out_png
        shutil.rmtree(tmp_dir, ignore_errors=True)

    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        prefix = output_dir / f"{path.stem}__pdftoppm"
        result = subprocess.run(
            [pdftoppm, "-png", "-r", "200", "-f", "1", "-l", "1", "-singlefile", str(path), str(prefix)],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode == 0:
            generated_png = output_dir / f"{path.stem}__pdftoppm.png"
            if generated_png.exists() and generated_png.stat().st_size > 0:
                shutil.move(str(generated_png), str(out_png))
                return out_png

    return None


def _caption_for_asset(path: Path, figures: List[PaperFigure], captions: List[str], source_dir: Optional[Path] = None) -> str:
    rel = ""
    if source_dir:
        try:
            rel = path.resolve().relative_to(source_dir.resolve()).as_posix().lower()
        except ValueError:
            rel = ""
    stem = path.stem.lower()
    for figure in figures:
        if rel and Path(figure.asset).as_posix().lower() == rel and figure.caption:
            return figure.caption[:500]
    for figure in figures:
        if Path(figure.asset).stem.lower() == stem and figure.caption:
            return figure.caption[:500]
    for caption in captions:
        lower = caption.lower()
        if stem in lower or stem.replace("_", " ") in lower:
            return caption[:500]
    return path.name
