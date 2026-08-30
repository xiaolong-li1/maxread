from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import PaperBundle, PaperFigure
from .formula_compiler import compile_formula_markup


PREFERRED_FIGURE_NAMES = [
    "introfig",
    "fig1",
    "main",
    "framework",
    "overall",
    "teacher_training",
    "inference",
    "world_state",
    "sparse_attention",
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


def polish_markdown(
    markdown: str,
    custom_macros: Optional[Dict[str, str]] = None,
    latex_macros: Optional[Dict[str, str]] = None,
    latex_arg_macros: Optional[Dict[str, str]] = None,
) -> str:
    markdown = compile_formula_markup(markdown).text
    markdown = _flatten_nested_latex_wrappers(markdown)
    markdown = _restore_code_like_latex(markdown)
    markdown = _normalize_escaped_currency(markdown)
    markdown = _normalize_backticked_math(markdown, latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
    markdown = _normalize_display_math(markdown, latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
    markdown = _normalize_inline_math(markdown, latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
    markdown = _split_adjacent_markdown_tables(markdown)
    markdown = _normalize_table_math(markdown, latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
    markdown = _sanitize_latex_blocks(markdown, latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
    markdown = _replace_breaks_outside_latex(markdown)
    markdown = _expand_text_macros_outside_latex(markdown, custom_macros or {})
    markdown = _sanitize_visible_text_macros(markdown)
    markdown = compile_formula_markup(markdown).text
    markdown = _flatten_nested_latex_wrappers(markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip() + "\n"


def _split_adjacent_markdown_tables(markdown: str) -> str:
    """Insert a blank line when a second table starts without a separator."""
    output: List[str] = []
    seen_separator = False
    for line in str(markdown or "").splitlines():
        stripped = line.strip()
        table_row = _looks_like_table_row(line)
        separator = bool(re.fullmatch(r"\|?[\s:|\-]+\|?", stripped)) if table_row else False
        if separator and seen_separator and output and _looks_like_table_row(output[-1]):
            header = output.pop()
            if output and output[-1].strip():
                output.append("")
            output.extend([header, line])
            seen_separator = True
            continue
        output.append(line)
        if separator:
            seen_separator = True
        elif not table_row:
            seen_separator = False
    return "\n".join(output)


def _flatten_nested_latex_wrappers(markdown: str) -> str:
    """Remove presentation wrappers that would create nested Feishu formulas."""
    text = re.sub(r"`\s*(<latex>.*?</latex>)\s*`", r"\1", str(markdown or ""), flags=re.S)
    for _ in range(3):
        updated = re.sub(r"<latex>\s*<latex>", "<latex>", text, flags=re.I)
        updated = re.sub(r"</latex>\s*</latex>", "</latex>", updated, flags=re.I)
        if updated == text:
            break
        text = updated
    return text


def _normalize_backticked_math(
    markdown: str,
    latex_macros: Optional[Dict[str, str]] = None,
    latex_arg_macros: Optional[Dict[str, str]] = None,
) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if _looks_like_currency_code(raw):
            return match.group(0)
        if _looks_like_code_identifier(raw):
            return match.group(0)
        if not _looks_like_math_code(raw):
            return match.group(0)
        body = _normalize_latex_body(raw, latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
        if not _is_valid_latex_body(body):
            # Models sometimes put a readable set definition in code ticks
            # while mixing TeX styling commands with Chinese prose. Keeping
            # that raw string makes it render as code and triggers three
            # separate blocking checks. Strip only presentation commands and
            # retain the content as readable text; structural LaTeX errors
            # still stay visible for the quality gate.
            if re.search(r"[\u4e00-\u9fff]", raw) and re.search(
                r"\\(?:mathcal|mathbf|mathrm|mathbb|mathsf|mathit)\b", raw
            ):
                cleaned = _strip_math_code_for_text(raw)
                if cleaned and cleaned != raw:
                    return f"`{cleaned}`"
            return match.group(0)
        return f"<latex>{body}</latex>"

    return re.sub(r"`([^`\n]{1,240})`", repl, markdown)


_MATH_FUNCTION_NAMES = {
    "argmax", "argmin", "cos", "det", "exp", "f", "g", "h", "log",
    "max", "mean", "min", "p", "q", "relu", "sigmoid", "sin", "softmax",
    "sqrt", "sum", "tanh", "var",
}
_MATH_SNAKE_PREFIXES = {
    "alpha", "beta", "chi", "delta", "eta", "gamma", "kappa", "lambda",
    "mu", "nu", "omega", "phi", "psi", "rho", "sigma", "tau", "theta",
}


def _restore_code_like_latex(markdown: str) -> str:
    """Undo reviewer mistakes that turn program identifiers into formulas."""

    def repl(match: re.Match[str]) -> str:
        body = html.unescape(match.group(1)).strip()
        if _looks_like_code_identifier(body):
            return f"`{body}`"
        return match.group(0)

    return re.sub(r"<latex>(.*?)</latex>", repl, str(markdown or ""), flags=re.S | re.I)


def _looks_like_code_identifier(text: str) -> bool:
    value = html.unescape(str(text or "")).strip()
    if not value or "\n" in value or re.search(r"\\|[{}=+*/^<>≤≥]", value):
        return False
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.]*)(?:\(([A-Za-z0-9_.,='\"\s-]*)\))?", value)
    if not match:
        return False
    name = match.group(1)
    arguments = match.group(2)
    root = name.split("_", 1)[0].lower()
    if root in _MATH_SNAKE_PREFIXES or name.lower() in _MATH_FUNCTION_NAMES:
        return False
    segments = name.split("_")
    descriptive_segments = sum(len(segment) >= 2 for segment in segments)
    descriptive_snake_case = (
        len(segments) >= 2
        and descriptive_segments >= 2
        and all(segment.isalnum() for segment in segments)
    )
    call_with_code_argument = arguments is not None and "_" in arguments and len(name) >= 3
    return descriptive_snake_case or call_with_code_argument


def _looks_like_math_code(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"\\[A-Za-z]+|[_^]", value):
        return True
    if re.search(r"[α-ωΑ-Ωϑϕϵℓ≤≥×]", value):
        return True
    if re.fullmatch(r"[A-Za-z]", value):
        return True
    if re.fullmatch(r"\(?[A-Za-z](?:\s*,\s*[A-Za-z])+\)?", value):
        return True
    if re.fullmatch(r"[A-Za-z0-9.{}()[\],\s]+(?:[=<>+*/-][A-Za-z0-9.{}()[\],\s]*)+", value):
        return True
    return bool(re.fullmatch(r"\[[0-9.,\s]+\]", value))


def _strip_math_code_for_text(text: str) -> str:
    value = html.unescape(str(text or "")).strip()
    replacements = {
        r"\leq": "<=",
        r"\le": "<=",
        r"\geq": ">=",
        r"\ge": ">=",
        r"\pm": "+/-",
        r"\cup": " union ",
        r"\cap": " intersection ",
        r"\in": " in ",
        r"\notin": " not in ",
        r"\to": " -> ",
    }
    for command, replacement in replacements.items():
        value = re.sub(re.escape(command) + r"(?![A-Za-z])", replacement, value)
    value = re.sub(
        r"\\(?:mathcal|mathbf|mathrm|mathbb|mathsf|mathit)\s*",
        "",
        value,
    )
    value = re.sub(r"\\([{}\[\]()])", r"\1", value)
    value = re.sub(r"\\+", "+", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


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


def figure_prompt_lines(inserts: List[Tuple[str, Path, str]], visual_descriptions: Optional[Dict[str, str]] = None) -> List[str]:
    lines = []
    visual_descriptions = visual_descriptions or {}
    for marker, path, caption in inserts:
        visual = visual_descriptions.get(marker, "").strip()
        suffix = f" visual：{visual}" if visual else ""
        if str(caption or "").startswith("并列图组"):
            suffix += " layout：panel 说明已内嵌，正文只写图组共同结论"
        lines.append(f"- {marker} 文件：{path.as_posix()} caption：{caption or path.stem}{suffix}")
    return lines


def compose_related_figure_groups(
    inserts: List[Tuple[str, Path, str]],
    visual_descriptions: Optional[Dict[str, str]] = None,
) -> Tuple[List[Tuple[str, Path, str]], Dict[str, str]]:
    """Combine adjacent, semantically related figures into readable pairs."""
    visuals = dict(visual_descriptions or {})
    output: List[Tuple[str, Path, str]] = []
    grouped_visuals: Dict[str, str] = {}
    index = 0
    while index < len(inserts):
        current = inserts[index]
        if index + 1 >= len(inserts):
            output.append(current)
            if current[0] in visuals:
                grouped_visuals[current[0]] = visuals[current[0]]
            break
        following = inserts[index + 1]
        if not _should_group_related_figures(current, following, visuals):
            output.append(current)
            if current[0] in visuals:
                grouped_visuals[current[0]] = visuals[current[0]]
            index += 1
            continue
        marker_a, path_a, caption_a = current
        marker_b, path_b, caption_b = following
        group_dir = Path(path_a).parent / "related_groups"
        group_path = group_dir / f"{_safe_stem(Path(path_a).stem)}--{_safe_stem(Path(path_b).stem)}.png"
        labels = [
            _related_panel_label(visuals.get(marker_a, ""), caption_a, Path(path_a), 0),
            _related_panel_label(visuals.get(marker_b, ""), caption_b, Path(path_b), 1),
        ]
        composed = _compose_related_pair(Path(path_a), Path(path_b), group_path, labels)
        if composed is None:
            output.append(current)
            if marker_a in visuals:
                grouped_visuals[marker_a] = visuals[marker_a]
            index += 1
            continue
        marker_index = re.search(r"\[MaxReadFigure:(\d+):", marker_a)
        ordinal = marker_index.group(1) if marker_index else str(len(output) + 1)
        marker = f"[MaxReadFigure:{ordinal}:related-{_safe_stem(Path(path_a).stem)}-{_safe_stem(Path(path_b).stem)}]"
        caption = f"并列图组：(a) {caption_a[:260]}；(b) {caption_b[:260]}"
        output.append((marker, composed, caption))
        grouped_visuals[marker] = (
            f"(a) {visuals.get(marker_a, caption_a)}；(b) {visuals.get(marker_b, caption_b)}"
        )[:480]
        index += 2
    return output, grouped_visuals


_RELATED_FIGURE_STOPWORDS = {
    "figure", "fig", "shows", "showing", "results", "result", "comparison", "different",
    "proposed", "method", "model", "models", "visual", "image", "images", "plot", "plots",
    "performance", "example", "examples", "using", "with", "from", "under", "across", "our",
}


def _should_group_related_figures(a, b, visuals: Dict[str, str]) -> bool:
    marker_a, path_a, caption_a = a
    marker_b, path_b, caption_b = b
    if _is_reconstructed_latex_figure(Path(path_a)) or _is_reconstructed_latex_figure(Path(path_b)):
        return False
    target_a = _figure_section_target(Path(path_a), caption_a, visuals.get(marker_a, ""))
    target_b = _figure_section_target(Path(path_b), caption_b, visuals.get(marker_b, ""))
    if target_a and target_b and target_a != target_b:
        return False
    tokens_a = _figure_relation_tokens(Path(path_a), caption_a, visuals.get(marker_a, ""))
    tokens_b = _figure_relation_tokens(Path(path_b), caption_b, visuals.get(marker_b, ""))
    common = tokens_a & tokens_b
    required = 2 if target_a or target_b else 3
    return len(common) >= required


def _is_reconstructed_latex_figure(path: Path) -> bool:
    """A source-level multi-asset Figure is already a complete visual unit."""
    return path.parent.name == "rendered_figures" and path.stem.lower().startswith(("fig_", "fig-"))


def _figure_relation_tokens(path: Path, caption: str, visual: str) -> set[str]:
    text = f"{path.stem} {caption} {visual}".lower().replace("_", " ").replace("-", " ")
    return {
        token for token in re.findall(r"[a-z0-9]{3,}", text)
        if token not in _RELATED_FIGURE_STOPWORDS and not token.isdigit()
    }


def _related_panel_label(visual: str, caption: str, path: Path, index: int) -> str:
    has_cjk_font = bool(_cjk_figure_font_path())
    if has_cjk_font:
        text = re.sub(r"\s+", " ", str(visual or "")).strip()
        if not text:
            text = display_caption(caption, path, max_chars=30)
    else:
        text = _plain_english_panel_label(caption, path)
    text = re.sub(r"^(?:图中|该图|这张图)(?:展示|显示|给出|对比)?", "", text).strip(" ：:。")
    text = re.split(r"[。；;]", text, maxsplit=1)[0].strip()
    max_chars = 30 if has_cjk_font else 48
    if len(text) > max_chars:
        text = text[: max_chars - 2].rstrip() + "..."
    letter = chr(ord("a") + index)
    return f"({letter}) {text or display_caption(caption, path, max_chars=26)}"


def _plain_english_panel_label(caption: str, path: Path) -> str:
    text = re.sub(r"\\[A-Za-z]+\s*(?:\{([^{}]*)\})?", lambda match: match.group(1) or "", str(caption or ""))
    text = re.sub(r"[^\x20-\x7E]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .:;")
    text = re.split(r"[.;]", text, maxsplit=1)[0].strip()
    if not text:
        text = re.sub(r"[_-]+", " ", path.stem).strip()
    return text[:44].rstrip() + ("..." if len(text) > 44 else "")


def _compose_related_pair(path_a: Path, path_b: Path, output_path: Path, labels: List[str]) -> Optional[Path]:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    try:
        opened = [Image.open(path).convert("RGBA") for path in (path_a, path_b)]
        ratios = [image.width / max(1, image.height) for image in opened]
        horizontal = max(ratios) < 1.8
        gap = 28
        padding = 24
        label_height = 78
        font = _figure_label_font()
        if horizontal:
            cell_width = 1080
            rendered = []
            heights = []
            for image in opened:
                scale = min(cell_width / image.width, 900 / image.height)
                resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
                rendered.append(resized)
                heights.append(resized.height)
            card_height = max(heights) + label_height + padding * 2
            width = cell_width * 2 + gap + padding * 2
            height = card_height
            canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
            draw = ImageDraw.Draw(canvas)
            for idx, image in enumerate(rendered):
                x0 = padding + idx * (cell_width + gap)
                draw.rounded_rectangle((x0, 0, x0 + cell_width, card_height - 1), radius=10, fill=(247, 247, 246, 255))
                image_x = x0 + (cell_width - image.width) // 2
                image_y = padding + (max(heights) - image.height) // 2
                canvas.alpha_composite(image, (image_x, image_y))
                if font:
                    _draw_centered_text(draw, labels[idx], x0 + 12, padding + max(heights), cell_width - 24, label_height, font)
        else:
            cell_width = 1800
            rendered = []
            for image in opened:
                scale = min(cell_width / image.width, 900 / image.height)
                rendered.append(image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS))
            card_heights = [image.height + label_height + padding * 2 for image in rendered]
            width = cell_width + padding * 2
            height = sum(card_heights) + gap
            canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
            draw = ImageDraw.Draw(canvas)
            y0 = 0
            for idx, image in enumerate(rendered):
                card_height = card_heights[idx]
                draw.rounded_rectangle((padding, y0, padding + cell_width, y0 + card_height - 1), radius=10, fill=(247, 247, 246, 255))
                image_x = padding + (cell_width - image.width) // 2
                canvas.alpha_composite(image, (image_x, y0 + padding))
                if font:
                    _draw_centered_text(draw, labels[idx], padding + 12, y0 + padding + image.height, cell_width - 24, label_height, font)
                y0 += card_height + gap
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path, format="PNG", optimize=True)
        return output_path
    except Exception:
        return None


def markdown_to_docx_xml(
    markdown: str,
    latex_macros: Optional[Dict[str, str]] = None,
    latex_arg_macros: Optional[Dict[str, str]] = None,
) -> str:
    # Keep this boundary defensive: repair scripts and older artifacts can call
    # the XML renderer directly without going through polish_markdown().
    markdown = compile_formula_markup(markdown).text
    markdown = _restore_code_like_latex(markdown)
    markdown = _normalize_table_math(markdown, latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
    markdown = _sanitize_latex_blocks(markdown, latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
    markdown = _sanitize_visible_text_macros(markdown)
    markdown = re.sub(
        r"(?m)^[ \t]*(\[MaxReadFigure:[^\]\n]+\])[ \t]*$",
        r"\n\1\n",
        markdown,
    )
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
            else:
                xml_parts.append(f"<h{level}>{_inline_xml(match.group(2))}</h{level}>")
        elif match := re.match(r"^(#{1,6})\s+([^\n]+)\n(.+)$", stripped, flags=re.S):
            level = min(len(match.group(1)), 6)
            if not xml_parts and level == 1:
                xml_parts.append(f"<title>{_inline_xml(match.group(2))}</title>")
            else:
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
        elif _is_compiled_figure_caption(stripped):
            # The publisher attaches this text to the image block using
            # Feishu's native caption style. Keeping a second paragraph here
            # would create the duplicate body-style caption users reported.
            pass
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
        lines.append(marker)
        lines.append(f"图题：{caption or title}")
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
        lines.append(marker)
        lines.append(f"图题：{caption or title}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def ensure_priority_figure_markers(
    markdown: str,
    inserts: List[Tuple[str, Path, str]],
    max_missing: int = 2,
    visual_descriptions: Optional[Dict[str, str]] = None,
) -> str:
    """Insert missing high-value overview/method figures near method text before review."""
    if not inserts or max_missing <= 0:
        return markdown
    visual_descriptions = visual_descriptions or {}
    missing = [
        (marker, path, caption)
        for marker, path, caption in inserts
        if marker not in markdown and _is_priority_figure(path, caption, visual_descriptions.get(marker, ""))
    ]
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
            block.append(f"图题：{_short_caption(caption)}")
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


def _is_priority_figure(path: Path, caption: str = "", visual_description: str = "") -> bool:
    path_text = path.stem.lower()
    caption_text = str(caption or "").lower()
    visual_text = str(visual_description or "").lower()
    evidence_text = f"{caption_text} {visual_text}".strip()
    path_tokens = set(re.split(r"[^a-z0-9]+", path_text.replace("_", "-")))
    priority_path_tokens = {
        "introfig", "fig1", "arch", "architecture", "overview", "pipeline",
        "workflow", "framework", "teaser", "overall", "method", "inference",
        "teacher", "student", "world", "state",
    }
    priority_path_phrases = ("model_arch", "model-arch", "method_overview", "method-overview")
    has_priority_path = bool(path_tokens & priority_path_tokens) or any(phrase in path_text for phrase in priority_path_phrases)
    evidence_keywords = (
        "architecture", "overview", "pipeline", "workflow", "framework",
        "model architecture", "method overview", "overall design", "block diagram",
        "schematic", "sparse attention", "world state bank", "inference pipeline",
        "teacher backbone", "student generator", "coarse-to-fine",
        "流程", "架构", "框架", "模块", "箭头", "输入", "输出",
    )
    if any(keyword in evidence_text for keyword in evidence_keywords):
        return True
    metric_words = ("pretrain loss", "training loss", "perplexity", "ppl", "benchmark", "accuracy")
    if any(word in evidence_text for word in metric_words):
        return False
    return has_priority_path


def _short_caption(caption: str, max_chars: int = 180) -> str:
    text = _sanitize_visible_text_macros(str(caption or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "..."


def remove_figure_markers(markdown: str, markers: Iterable[str]) -> str:
    for marker in markers:
        markdown = markdown.replace(marker, "")
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip() + "\n"


def normalize_figure_captions(
    markdown: str,
    inserts: Iterable[Tuple[str, Path, str]],
    visual_descriptions: Optional[Dict[str, str]] = None,
) -> str:
    """Compile model-written figure notes into numbered, plain caption paragraphs."""
    source_captions = {marker: caption for marker, _path, caption in inserts}
    visuals = visual_descriptions or {}
    marker_pattern = re.compile(r"^\s*(\[MaxReadFigure:[^\]\n]+\])\s*$")
    lines = str(markdown or "").splitlines()
    output: List[str] = []
    figure_number = 0
    index = 0
    while index < len(lines):
        match = marker_pattern.match(lines[index])
        if not match:
            output.append(lines[index])
            index += 1
            continue
        marker = match.group(1)
        figure_number += 1
        output.append(marker)
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1

        caption_lines: List[str] = []
        if index < len(lines) and _is_figure_caption_line(lines[index]):
            while index < len(lines) and lines[index].strip():
                caption_lines.append(lines[index].strip())
                index += 1
        raw_caption = " ".join(caption_lines)
        caption = _clean_figure_caption_text(raw_caption)
        if not caption:
            caption = _clean_figure_caption_text(source_captions.get(marker, ""))
        visual = _clean_figure_caption_text(visuals.get(marker, ""))
        if visual and _looks_english(caption) and not _looks_english(visual):
            caption = visual
        if not caption:
            caption = "论文关键图。"
        if caption[-1:] not in "。！？.!?":
            caption += "。"
        output.extend(["", f"图 {figure_number}\u3000{caption}", ""])
    return re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip() + "\n"


def _is_figure_caption_line(line: str) -> bool:
    text = re.sub(r"^\s*>\s*", "", str(line or "")).strip()
    text = text.strip("*_ ")
    return bool(re.match(r"(?i)^(?:图解|图题|图(?:\s*\d+)?|fig(?:ure)?\.?\s*\d*)\s*[：:.\-—]?", text))


def _clean_figure_caption_text(text: str) -> str:
    value = re.sub(r"(?:^|\s)>\s*", " ", str(text or "")).strip()
    value = value.strip("*_ ")
    value = re.sub(
        r"(?i)^(?:图解|图题|图(?:\s*\d+)?|fig(?:ure)?\.?\s*\d*)\s*[：:.\-—]?\s*",
        "",
        value,
    )
    value = value.strip("*_ ")
    return re.sub(r"\s+", " ", value).strip()


def compiled_figure_captions(markdown: str) -> Dict[str, str]:
    """Return marker -> native image caption after deterministic compilation."""
    lines = str(markdown or "").splitlines()
    captions: Dict[str, str] = {}
    for index, line in enumerate(lines):
        marker = line.strip()
        if not re.fullmatch(r"\[MaxReadFigure:[^\]\n]+\]", marker):
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor < len(lines) and _is_compiled_figure_caption(lines[cursor].strip()):
            captions[marker] = lines[cursor].strip()
    return captions


def _is_compiled_figure_caption(text: str) -> bool:
    return bool(re.match(r"^图\s+\d+\u3000\S", str(text or "").strip()))



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
        ("token reduction", "视觉 Token 削减性能图"),
        ("visual-token reduction", "视觉 Token 削减性能图"),
        ("different visual-token", "跨预算性能图"),
        ("accuracy", "精度对比图"),
        ("performance", "性能对比图"),
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
    text = _sanitize_visible_text_macros(text)
    text = re.sub(r"\\(?:xspace|NB|DX|lpk|wx|qz)(?![A-Za-z])(?:\s*\{([^{}]*)\})?", lambda m: m.group(1) or "", text)
    text = re.sub(r"\\(?:citep?|ref|label|url|href)\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\([A-Za-z]{3,})(?![A-Za-z])", r"\1", text)
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _normalize_display_math(
    markdown: str,
    latex_macros: Optional[Dict[str, str]] = None,
    latex_arg_macros: Optional[Dict[str, str]] = None,
) -> str:
    def repl(match: re.Match[str]) -> str:
        body = _normalize_latex_body(
            re.sub(r"\s+", " ", match.group(1).strip()),
            latex_macros=latex_macros,
            latex_arg_macros=latex_arg_macros,
        )
        body = body.replace("\\\\", r"\\")
        return f"\n<latex>{body}</latex>\n"

    markdown = re.sub(r"\$\$\s*(.*?)\s*\$\$", repl, markdown, flags=re.S)
    markdown = re.sub(r"\\\[\s*(.*?)\s*\\\]", repl, markdown, flags=re.S)
    # Guard against model output like '# $$ ... $$', which Feishu markdown may
    # interpret as headings before converting math.
    markdown = re.sub(r"(?m)^#+\s*(<latex>.*?</latex>)\s*$", r"\1", markdown)
    return markdown


def _markdown_blocks(markdown: str) -> List[str]:
    blocks: List[str] = []
    current: List[str] = []
    latex_depth = 0

    def flush() -> None:
        if current:
            blocks.append("\n".join(current))
            current.clear()

    def block_kind(lines: List[str]) -> str:
        nonempty = [line.strip() for line in lines if line.strip()]
        if nonempty and all(_looks_like_table_row(line) for line in nonempty):
            return "table"
        if nonempty and all(re.match(r"^[-*+]\s+", line) for line in nonempty):
            return "unordered-list"
        if nonempty and all(re.match(r"^\d+[.)]\s+", line) for line in nonempty):
            return "ordered-list"
        return "paragraph"

    for line in markdown.splitlines():
        stripped = line.strip()
        if latex_depth > 0:
            current.append(line.rstrip())
            latex_depth += _latex_tag_delta(stripped)
            latex_depth = max(0, latex_depth)
            continue
        if not stripped:
            flush()
            continue
        if re.match(r"^<latex\b", stripped, flags=re.I):
            current.append(line.rstrip())
            latex_depth = max(0, _latex_tag_delta(stripped))
            continue
        # Markdown producers often omit the blank line before a heading, table,
        # or list. Split those structural boundaries before XML conversion.
        if re.match(r"^#{1,6}\s+\S", stripped):
            flush()
            blocks.append(stripped)
            continue
        kind = "table" if _looks_like_table_row(stripped) else ""
        if re.match(r"^[-*+]\s+", stripped):
            kind = "unordered-list"
        elif re.match(r"^\d+[.)]\s+", stripped):
            kind = "ordered-list"
        if current and kind and block_kind(current) != kind:
            flush()
        if current and not kind and block_kind(current) in {"table", "unordered-list", "ordered-list"}:
            flush()
        current.append(line.rstrip())
    flush()
    return blocks


def _latex_tag_delta(text: str) -> int:
    openings = len(re.findall(r"<latex\b[^>]*>", text or "", flags=re.I))
    closings = len(re.findall(r"</latex\s*>", text or "", flags=re.I))
    return openings - closings


def _is_latex_block(text: str) -> bool:
    return bool(re.fullmatch(r"<latex>.*?</latex>", text, flags=re.S))


def _is_table_block(block: str) -> bool:
    lines = [_normalize_table_line(line) for line in block.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    if not all(_looks_like_table_row(line) for line in lines[:2]):
        return False
    return re.fullmatch(r"\|?[\s:|\-]+\|?", lines[1]) is not None


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
    lines = [_normalize_table_line(line) for line in block.splitlines() if line.strip()]
    rows = [lines[0]] + lines[2:]
    column_count = max(
        (len([cell for cell in row.strip("|").split("|")]) for row in rows),
        default=1,
    )
    widths = _table_column_widths(column_count)
    out = ["<table><colgroup>"]
    out.extend(f'<col width="{width}"/>' for width in widths)
    out.append("</colgroup><tbody>")
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        out.append("<tr>" + "".join(f"<td><p>{_inline_xml(cell)}</p></td>" for cell in cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _table_column_widths(column_count: int, target_width: int = 1200) -> List[int]:
    count = max(1, int(column_count))
    target = max(720, int(target_width))
    if count == 1:
        return [target]
    if count == 2:
        return [round(target * 0.32), target - round(target * 0.32)]
    if count <= 6:
        first = max(180, round(target * 0.22))
        remaining = target - first
        base = remaining // (count - 1)
        widths = [first] + [base] * (count - 1)
        widths[-1] += target - sum(widths)
        return widths
    # Wide result tables should remain readable and use horizontal scrolling;
    # squeezing 10-20 metric columns into the viewport makes every cell wrap.
    return [max(120, target // count)] * count


def _normalize_table_line(line: str) -> str:
    return line.strip().rstrip()


def _looks_like_table_row(line: str) -> bool:
    return line.count("|") >= 2 and bool(line.strip("|").strip())


def _inline_xml(text: str) -> str:
    placeholders: List[str] = []

    def hold(value: str) -> str:
        placeholders.append(value)
        return f"@@MAXREAD_XML_{len(placeholders) - 1}@@"

    text = re.sub(r"<latex>(.*?)</latex>", lambda m: hold(f"<latex>{html.escape(m.group(1), quote=False)}</latex>"), text, flags=re.S)
    text = re.sub(r"<br\s*/?>", lambda m: hold("<br/>"), text)
    text = re.sub(r"`([^`]+)`", lambda m: hold(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = _sanitize_visible_text_macros(text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)
    for index, value in enumerate(placeholders):
        text = text.replace(f"@@MAXREAD_XML_{index}@@", value)
    # Feishu can canonicalize a break immediately after an inline formula as a
    # child of <latex>, which makes the persisted formula invalid.
    text = re.sub(r"(</latex>)<br/>", r"\1 ", text)
    return text


def _sanitize_latex_blocks(
    markdown: str,
    latex_macros: Optional[Dict[str, str]] = None,
    latex_arg_macros: Optional[Dict[str, str]] = None,
) -> str:
    def repl(match: re.Match[str]) -> str:
        body = _normalize_latex_body(match.group(1).strip(), latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
        if not _is_valid_latex_body(body):
            return f"`{_strip_latex_for_text(body)}`"
        return f"<latex>{body}</latex>"

    return re.sub(r"<latex>(.*?)</latex>", repl, markdown, flags=re.S)


def _normalize_inline_math(
    markdown: str,
    latex_macros: Optional[Dict[str, str]] = None,
    latex_arg_macros: Optional[Dict[str, str]] = None,
) -> str:
    def repl(match: re.Match[str]) -> str:
        body = _normalize_latex_body(match.group(1).strip(), latex_macros=latex_macros, latex_arg_macros=latex_arg_macros)
        if not body:
            return match.group(0)
        return f"<latex>{body}</latex>"

    parts = re.split(r"(<latex>.*?</latex>|`[^`\n]*`)", str(markdown or ""), flags=re.S | re.I)
    for index in range(0, len(parts), 2):
        segment = re.sub(r"\\\((.{1,240}?)\\\)", repl, parts[index])
        parts[index] = re.sub(r"(?<![\\$])\$([^\n$]{1,240})(?<!\\)\$(?!\$)", repl, segment)
    return "".join(parts)


def _looks_like_currency_code(value: str) -> bool:
    text = str(value or "").strip()
    return bool(
        re.fullmatch(r"\$(?:[A-Za-z][A-Za-z0-9._/-]*|\d+(?:\.\d+)?(?:/[A-Za-z0-9._/-]+)?)", text)
        or re.fullmatch(r"(?i)Perf/\$", text)
    )


def _normalize_escaped_currency(markdown: str) -> str:
    r"""Keep Markdown currency escapes out of the inline-math lexer.

    Models correctly use ``\$`` for labels such as ``$Total`` and ``Perf/$``.
    Treating two escaped currency signs as math delimiters can consume an
    entire sentence or merge two table columns into one invalid formula.
    """
    parts = re.split(r"(<latex>.*?</latex>|`[^`\n]*`)", str(markdown or ""), flags=re.S | re.I)
    for index in range(0, len(parts), 2):
        segment = re.sub(r"(?i)\bPerf/\\\$", "`Perf/$`", parts[index])
        segment = re.sub(
            r"\\\$([A-Za-z0-9][A-Za-z0-9._/\-–]*)",
            lambda match: f"`{match.group(0)[1:]}`",
            segment,
        )
        parts[index] = segment.replace(r"\$", "`$`")
    return "".join(parts)


_NUMBER_TOKEN = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_RAW_TABLE_UNCERTAINTY_RE = re.compile(
    rf"(?<![\w.]){_NUMBER_TOKEN}\s*(?:"
    rf"\^\s*\{{\s*{_NUMBER_TOKEN}\s*\}}\s*_\s*\{{\s*{_NUMBER_TOKEN}\s*\}}"
    rf"|_\s*\{{\s*{_NUMBER_TOKEN}\s*\}}\s*\^\s*\{{\s*{_NUMBER_TOKEN}\s*\}}"
    rf")(?![\w.])"
)


def _normalize_table_math(
    markdown: str,
    latex_macros: Optional[Dict[str, str]] = None,
    latex_arg_macros: Optional[Dict[str, str]] = None,
) -> str:
    """Compile un-delimited numeric uncertainty notation inside Markdown tables."""

    def normalize_formula_pipes(line: str) -> str:
        def repl(match: re.Match[str]) -> str:
            body = match.group(1).replace(r"\|", r"\vert ")
            body = body.replace("|", r"\vert ")
            return f"<latex>{body}</latex>"

        return re.sub(r"<latex>(.*?)</latex>", repl, line, flags=re.S | re.I)

    def normalize_cell(cell: str) -> str:
        parts = re.split(r"(<latex>.*?</latex>|`[^`\n]*`)", cell, flags=re.S | re.I)
        for index in range(0, len(parts), 2):
            parts[index] = _RAW_TABLE_UNCERTAINTY_RE.sub(
                lambda match: (
                    "<latex>"
                    + _normalize_latex_body(
                        match.group(0),
                        latex_macros=latex_macros,
                        latex_arg_macros=latex_arg_macros,
                    )
                    + "</latex>"
                ),
                parts[index],
            )
        return "".join(parts)

    lines: List[str] = []
    for line in str(markdown or "").splitlines():
        if not _looks_like_table_row(line) or re.fullmatch(r"\|?[\s:|\-]+\|?", line.strip()):
            lines.append(line)
            continue
        # Raw absolute-value/set-builder bars inside a formula are valid TeX
        # but our Markdown table parser would treat them as cell delimiters.
        # Compile them before splitting the row so the formula stays atomic.
        line = normalize_formula_pipes(line)
        cells = line.split("|")
        lines.append("|".join(normalize_cell(cell) for cell in cells))
    return "\n".join(lines)


def _normalize_latex_body(
    body: str,
    latex_macros: Optional[Dict[str, str]] = None,
    latex_arg_macros: Optional[Dict[str, str]] = None,
) -> str:
    body = _decode_latex_html(body)
    body = _normalize_unicode_math_symbols(body)
    body = body.replace(r"\_", "_")
    body = _expand_latex_custom_macros(body, latex_macros or {}, latex_arg_macros or {})
    body = _flatten_substack(body)
    body = _repair_split_latex_commands(body)
    body = _strip_latex_publish_metadata(body)
    body = _normalize_paper_math_macros(body)
    body = _repair_latex_delimiter_corruption(body)
    body = _normalize_fused_latex_accents(body)
    body = _normalize_latex_text_macros(body)
    body = _normalize_boldsymbol(body)
    body = _normalize_latex_control_spaces(body)
    body = _repair_fused_greek_commands(body)
    body = _normalize_latex_delimiter_sizing(body)
    body = _repair_internal_display_delimiters(body)
    body = _repair_cases_missing_row_breaks(body)
    body = re.sub(r"\\bm\s*\{([^{}]+)\}", r"\\mathbf{\1}", body)
    body = re.sub(r"\\bm([A-Za-z])(?=[^A-Za-z]|$)", r"\\mathbf{\1}", body)
    body = re.sub(r"\\(?:m|v)([A-Z])(?=\b|_)", r"\\mathbf{\1}", body)
    body = re.sub(r"\\g([A-Z])(?=\b|_)", r"\\mathcal{\1}", body)
    body = re.sub(r"\\mathcal([A-Za-z])\b", r"\\mathcal{\1}", body)
    body = re.sub(r"\\(?:Ls|mathcalL)(?=\b|_)", r"\\mathcal{L}", body)
    body = re.sub(r"\\R\b", r"\\mathbb{R}", body)
    body = re.sub(r"\\rm\s+([A-Za-z][A-Za-z0-9]*)", r"\\mathrm{\1}", body)
    body = re.sub(r"\\rm\b", r"\\mathrm", body)
    body = re.sub(r"\\sg\b", r"\\mathrm{sg}", body)
    body = re.sub(r"\\softmax\b", r"\\mathrm{softmax}", body)
    body = re.sub(r"(?<!\\)(?<![A-Za-z])softmax(?=\s*(?:\\left|\())", r"\\mathrm{softmax}", body)
    body = re.sub(r"(?<!\\)(?<![A-Za-z])stopgrad(?=\s*(?:\\left|\())", r"\\mathrm{stopgrad}", body)
    body = re.sub(r"\\KL\b", r"\\mathrm{KL}", body)
    body = re.sub(r"\\TopK\b", r"\\mathrm{TopK}", body)
    # Feishu removes ordinary spaces after spacing commands when persisting
    # formulas. An empty group keeps the command boundary stable on round-trip.
    body = re.sub(r"\\(qquad|quad)(?:\s+)?([A-Za-z])", r"\\\1{}\2", body)
    body = re.sub(r"\\(le|leq|ge|geq|approx|to|in|notin)([A-Z\\])", r"\\\1 \2", body)
    body = re.sub(r"(\d+)\\mathrm\{e\}\{(-?\d+)\}", r"\1\\times10^{\2}", body)
    body = re.sub(r"(\d+)\\mathrm\{e\}\s*([+-]\d+)", r"\1\\times10^{\2}", body)
    return body


def _repair_fused_greek_commands(body: str) -> str:
    commands = (
        "Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Theta", "Lambda",
        "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega",
    )
    pattern = r"\\(" + "|".join(commands) + r")([A-Z])(?=[^A-Za-z]|$)"
    return re.sub(pattern, r"\\\1 \2", str(body or ""))


def _normalize_latex_delimiter_sizing(body: str) -> str:
    """Downgrade delimiter sizing commands unsupported by Feishu formulas."""
    text = str(body or "")
    text = re.sub(r"\\middle\s*(?:\\vert|\\\||\|)", r"\\mid ", text)
    text = re.sub(r"\\(?:big|Big|bigg|Bigg)\s*(?=[()\[\]{}|]|\\[{}|])", "", text)
    return text


def _decode_latex_html(body: str) -> str:
    """Decode model-escaped HTML before removing tags from a formula body."""
    body = str(body or "")
    for _ in range(3):
        decoded = html.unescape(body)
        if decoded == body:
            break
        body = decoded
    body = re.sub(r"<\s*br\s*/?\s*>", " ", body, flags=re.I)
    body = re.sub(r"<\s*/?\s*(?:p|div)\b[^>]*>", " ", body, flags=re.I)
    return body


def _normalize_unicode_math_symbols(body: str) -> str:
    replacements = {
        "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
        "ε": r"\epsilon", "ϵ": r"\varepsilon", "ζ": r"\zeta", "η": r"\eta",
        "θ": r"\theta", "ϑ": r"\vartheta", "ι": r"\iota", "κ": r"\kappa",
        "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi",
        "π": r"\pi", "ρ": r"\rho", "σ": r"\sigma", "τ": r"\tau",
        "φ": r"\phi", "ϕ": r"\varphi", "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
        "Δ": r"\Delta", "Σ": r"\Sigma", "Π": r"\Pi", "Λ": r"\Lambda",
        "ℓ": r"\ell", "≤": r"\le ", "≥": r"\ge ", "×": r"\times ",
    }
    return "".join(replacements.get(char, char) for char in str(body or ""))


def _expand_text_macros_outside_latex(text: str, macros: Dict[str, str]) -> str:
    macros = _safe_text_macros(macros)
    if not macros:
        return text
    parts = re.split(r"(<latex>.*?</latex>)", text, flags=re.S)
    for index in range(0, len(parts), 2):
        parts[index] = _expand_text_macros(parts[index], macros)
    return "".join(parts)


_VISIBLE_TEXT_COMMANDS = (
    "textnormal", "operatorname", "textbf", "textit", "textsc", "textrm",
    "emph", "mathrm", "mathbf", "mathtt",
)


def _sanitize_visible_text_macros(text: str) -> str:
    """Remove TeX presentation commands that leaked into ordinary prose."""
    parts = re.split(r"(<latex>.*?</latex>|<code>.*?</code>)", text or "", flags=re.S)
    commands = sorted(_VISIBLE_TEXT_COMMANDS, key=len, reverse=True)
    command_pattern = r"\\(?:" + "|".join(map(re.escape, commands)) + r")"
    for index in range(0, len(parts), 2):
        segment = parts[index]
        for command in commands:
            segment = _replace_simple_latex_command(segment, command, _plain_latex_text)
        # Review output can lose the braces and leave text such as \textbfTitle.
        segment = re.sub(command_pattern + r"(?=[A-Za-z])", "", segment)
        segment = re.sub(r"\\(?:xspace|NB|DX|lpk|wx|qz)(?![A-Za-z])", "", segment)
        segment = re.sub(r"\\(?:citep?|ref|label|url|href)\s*\{[^{}]*\}", "", segment)
        parts[index] = segment
    return "".join(parts)


def _replace_breaks_outside_latex(text: str) -> str:
    parts = re.split(r"(<latex>.*?</latex>|<code>.*?</code>)", text or "", flags=re.S)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"<br\s*/?>", "\n", parts[index], flags=re.I)
    return "".join(parts)


def _safe_text_macros(macros: Dict[str, str]) -> Dict[str, str]:
    unsafe_names = {
        "R", "N", "Z", "C", "E", "P", "Q", "KL", "Var", "Cov", "argmax", "argmin",
        "max", "min", "sin", "cos", "tan", "log", "exp", "sqrt", "frac",
    }
    out: Dict[str, str] = {}
    for name, value in (macros or {}).items():
        key = str(name or "").strip()
        text = str(value or "").strip()
        if not key or key in unsafe_names:
            continue
        if len(text) < 2 or re.fullmatch(r"[A-Za-z]", text):
            continue
        if re.search(r"[\\{}_^]", text):
            continue
        out[key] = text
    return out


def _expand_text_macros(text: str, macros: Dict[str, str]) -> str:
    for name, value in sorted(macros.items(), key=lambda item: -len(item[0])):
        pattern = rf"\\{re.escape(name)}(?![A-Za-z])(?P<space>\s*)"
        text = re.sub(pattern, lambda m, v=value: v + (" " if m.group("space") else ""), text)
    return text


def _expand_latex_custom_macros(body: str, zero_arg: Dict[str, str], one_arg: Dict[str, str]) -> str:
    for _ in range(2):
        before = body
        for name, value in sorted((zero_arg or {}).items(), key=lambda item: -len(item[0])):
            body = re.sub(rf"\\{re.escape(name)}(?![A-Za-z])", lambda _m, v=value: v, body)
        for name, template in sorted((one_arg or {}).items(), key=lambda item: -len(item[0])):
            body = _replace_simple_latex_command(body, name, lambda content, t=template: t.replace("#1", content.strip()))
        if body == before:
            break
    return body


def _normalize_latex_text_macros(body: str) -> str:
    body = _replace_simple_latex_command(body, "mbox", _plain_latex_text)
    body = re.sub(r"\\xspace\b", "", body)
    for command in ("text", "textnormal", "textsc", "textbf", "textit", "emph", "mathtt", "operatorname"):
        body = _replace_simple_latex_command(body, command, lambda content: rf"\mathrm{{{_plain_latex_text(content)}}}")
    body = _replace_simple_latex_command(body, "mathrm", lambda content: rf"\mathrm{{{_normalize_mathrm_content(content)}}}")
    return body


def _normalize_fused_latex_accents(body: str) -> str:
    commands = "overline|underline|hat|widehat|tilde|widetilde|bar|vec|dot|ddot|check|breve|acute|grave"
    # A whitespace-delimited argument is unambiguous. Without whitespace, only
    # repair the uppercase matrix/vector shorthand commonly emitted by models;
    # accepting lowercase here corrupts valid commands such as \dots,
    # \doteq, \vector, and \checkmark.
    body = re.sub(rf"\\({commands})\s+([A-Za-z])", r"\\\1{\2}", body)
    return re.sub(rf"\\({commands})([A-Z])", r"\\\1{\2}", body)


def _normalize_mathrm_content(content: str) -> str:
    text = str(content or "").strip()
    text = re.sub(r"\\\s+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _repair_cases_missing_row_breaks(body: str) -> str:
    def repl(match: re.Match[str]) -> str:
        inside = match.group(1).strip()
        if r"\\" in inside:
            return match.group(0)
        parts = re.split(r",\s*&\s*", inside, maxsplit=2)
        if len(parts) != 3:
            return match.group(0)
        split = re.match(r"(.+?)\s{2,}(\\[A-Za-z].*)\Z", parts[1].strip(), flags=re.S)
        if not split:
            return match.group(0)
        first_expr = parts[0].strip()
        first_condition = split.group(1).strip()
        second_expr = split.group(2).strip()
        second_condition = parts[2].strip()
        return rf"\begin{{cases}}{first_expr} & {first_condition} \\ {second_expr} & {second_condition}\end{{cases}}"

    return re.sub(r"\\begin\{cases\}(.*?)\\end\{cases\}", repl, body, flags=re.S)


def _repair_internal_display_delimiters(body: str) -> str:
    """Turn a model's ``\\[4pt]`` row separator into valid LaTeX.

    Display delimiters are not valid inside a Feishu formula body. Models
    sometimes drop one backslash from the usual ``\\\\[4pt]`` cases row break,
    leaving the visually similar but invalid ``\\[4pt]`` sequence.
    """

    def row_break(match: re.Match[str]) -> str:
        spacing = re.sub(r"\s+", "", match.group(1))
        return rf"\\[{spacing}]"

    repaired = re.sub(
        r"(?<!\\)\\\[\s*([0-9]+(?:\.[0-9]+)?\s*(?:pt|em|ex|mm|cm|in|bp|dd|cc|sp))\s*\]",
        row_break,
        str(body or ""),
    )
    # A bare display delimiter can only be a wrapper leak once the content is
    # already inside <latex>; remove the delimiters while preserving the math.
    repaired = re.sub(r"(?<!\\)\\\]", "", repaired)
    return repaired


def _normalize_latex_control_spaces(body: str) -> str:
    return re.sub(r"(?<!\\)\\(?!\\)\s+", " ", body)


def _normalize_boldsymbol(body: str) -> str:
    body = _replace_simple_latex_command(body, "boldsymbol", _boldsymbol_replacement)
    return _replace_simple_latex_command(body, "bm", _boldsymbol_replacement)


def _boldsymbol_replacement(content: str) -> str:
    content = content.strip()
    if re.fullmatch(r"\\(?:mathcal|mathrm|mathbb|mathbf)\{[^{}]+\}", content):
        return content
    if re.fullmatch(r"\\[A-Za-z]+", content):
        return content
    if re.fullmatch(r"[A-Za-z](?:[_^].*)?", content):
        return rf"\mathbf{{{content}}}"
    return content


def _plain_latex_text(content: str) -> str:
    text = str(content or "").strip()
    text = re.sub(r"\\[A-Za-z]+\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\[A-Za-z]+", "", text)
    return text


def _replace_simple_latex_command(body: str, command: str, repl) -> str:
    marker = "\\" + command
    out: List[str] = []
    i = 0
    while i < len(body):
        start = body.find(marker, i)
        if start < 0:
            out.append(body[i:])
            break
        end_marker = start + len(marker)
        if end_marker < len(body) and body[end_marker].isalpha():
            out.append(body[i:end_marker])
            i = end_marker
            continue
        out.append(body[i:start])
        j = end_marker
        while j < len(body) and body[j].isspace():
            j += 1
        if j >= len(body) or body[j] != "{":
            out.append(marker)
            i = end_marker
            continue
        end = _matching_brace_index(body, j)
        if end is None:
            out.append(body[start:])
            break
        out.append(repl(body[j + 1:end]))
        i = end + 1
    return "".join(out)


def _normalize_paper_math_macros(body: str) -> str:
    # ``\cal`` is a TeX declaration rather than a portable command.  Paper
    # sources commonly emit ``{\cal T}``/``{\cal S}``; Feishu's formula
    # renderer accepts the explicit braced form but marks the declaration as
    # an invalid formula in the real document.
    body = re.sub(r"(?<![_^])\{\s*\\cal\s*\{([^{}]+)\}\s*\}", r"\\mathcal{\1}", body)
    body = re.sub(r"(?<![_^])\{\s*\\cal\s+([A-Za-z])\s*\}", r"\\mathcal{\1}", body)
    body = re.sub(r"\\cal\s*\{([^{}]+)\}", r"\\mathcal{\1}", body)
    body = re.sub(r"\\cal\s+([A-Za-z])", r"\\mathcal{\1}", body)
    body = re.sub(r"\\cal([A-Za-z])", r"\\mathcal{\1}", body)
    body = re.sub(r"\\mathsfit\s*\{([^{}]+)\}", r"\\mathbf{\1}", body)
    body = re.sub(r"\\(?:tens|etens)\s*\{([^{}]+)\}", r"\\mathbf{\1}", body)
    for command in ("matrix", "vector"):
        body = _replace_simple_latex_command(body, command, lambda content: rf"\mathbf{{{content.strip()}}}")
    body = _replace_simple_latex_command(body, "tr", _transpose_latex_content)
    body = re.sub(r"\\rv(?!ert\b)([A-Za-z]+)(?=[^A-Za-z]|$)", lambda m: _styled_macro(m.group(1), "mathbf"), body)
    body = re.sub(r"\\rm([A-Z])(?=[^A-Za-z]|$)", r"\\mathbf{\1}", body)
    body = re.sub(r"\\erv([A-Za-z])(?=[^A-Za-z]|$)", r"\\mathrm{\1}", body)
    body = re.sub(r"\\erm([A-Za-z])(?=[^A-Za-z]|$)", r"\\mathrm{\1}", body)
    body = re.sub(r"\\et([A-Z][A-Za-z]*)(?=[^A-Za-z]|$)", lambda m: _styled_macro(m.group(1), "mathbf"), body)
    body = re.sub(r"\\t([A-Z][A-Za-z]*)(?=[^A-Za-z]|$)", lambda m: _styled_macro(m.group(1), "mathbf"), body)
    return body


def _repair_latex_delimiter_corruption(body: str) -> str:
    r"""Recover ``\rvert`` when a paper-vector macro consumed its prefix.

    The legacy ``\rvName`` normalizer used to read ``\rvert`` as ``\rv`` +
    ``ert`` and emit ``\mathbf{ert}``.  Only reverse that artifact when an
    unmatched left delimiter proves the intended role.
    """
    text = str(body or "")
    missing = text.count(r"\lvert") - text.count(r"\rvert")
    for _ in range(max(0, missing)):
        if r"\mathbf{ert}" not in text:
            break
        text = text.replace(r"\mathbf{ert}", r"\rvert", 1)
    return text


def _transpose_latex_content(content: str) -> str:
    content = str(content or "").strip()
    if not content:
        return ""
    return rf"{{{content}}}^\top"


def _styled_macro(name: str, command: str) -> str:
    greek = {
        "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta",
        "theta", "vartheta", "iota", "kappa", "lambda", "Lambda", "mu", "nu", "xi",
        "pi", "rho", "sigma", "tau", "upsilon", "phi", "varphi", "chi", "psi", "omega",
    }
    if name in greek:
        return f"\\{command}{{\\{name}}}"
    return f"\\{command}{{{name}}}"


def _strip_latex_publish_metadata(body: str) -> str:
    body = re.sub(r"\\(?:label|ref|eqref|tag)\s*\{[^{}]*\}", "", body)
    body = re.sub(r"\\nonumber\b", "", body)
    return body


def _repair_split_latex_commands(body: str) -> str:
    repairs = {
        r"\\le\s+ft\b": r"\\left",
        r"\\ri\s+ght\b": r"\\right",
        # A split transpose only makes sense in superscript position. Matching
        # every ``\to p`` corrupts ordinary arrows such as ``q\to p``.
        r"(?<=\^)\\to\s+p\b": r"\\top",
        r"\\in\s+fty\b": r"\\infty",
        r"\\ap\s+prox\b": r"\\approx",
        r"\\ge\s+q\b": r"\\geq",
        r"\\le\s+q\b": r"\\leq",
    }
    for pattern, replacement in repairs.items():
        body = re.sub(pattern, replacement, body)
    return body


def _flatten_substack(body: str) -> str:
    marker = r"\substack"
    out: List[str] = []
    i = 0
    while i < len(body):
        start = body.find(marker, i)
        if start < 0:
            out.append(body[i:])
            break
        out.append(body[i:start])
        j = start + len(marker)
        while j < len(body) and body[j].isspace():
            j += 1
        if j >= len(body) or body[j] != "{":
            out.append(marker)
            i = start + len(marker)
            continue
        end = _matching_brace_index(body, j)
        if end is None:
            out.append(body[start:])
            break
        content = body[j + 1:end]
        content = content.replace(r"\\", ", ")
        content = re.sub(r"\s+", " ", content).strip()
        out.append(content)
        i = end + 1
    return "".join(out)


def _matching_brace_index(text: str, open_index: int) -> Optional[int]:
    depth = 0
    escaped = False
    for idx in range(open_index, len(text)):
        ch = text[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    return None


def _is_valid_latex_body(body: str) -> bool:
    text = str(body or "").strip()
    if not text:
        return False
    if any(ch in text for ch in "\uFFFD"):
        return False
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    if not _balanced_latex_braces(text):
        return False
    if _latex_command_count(text, "left") != _latex_command_count(text, "right"):
        return False
    if _latex_command_count(text, "lvert") != _latex_command_count(text, "rvert"):
        return False
    if re.search(r"\\(?:def|newcommand|renewcommand|usepackage|RequirePackage)\b", text):
        return False
    if re.search(r"\\(?:begin|end)\s*\{(?!aligned|align|array|matrix|pmatrix|bmatrix|cases)[^{}]+\}", text):
        return False
    if re.search(r"\\(?:qquad|quad)[A-Za-z]", text):
        return False
    if re.search(r"(?<!\\)\\(?:\[|\])", text):
        return False
    if re.search(r"\\rm\b", text):
        return False
    if re.search(r"\\[A-Za-z]+(?:\s+[A-Za-z]{3,}){3,}", text):
        return False
    return True


def _latex_command_count(text: str, command: str) -> int:
    return len(re.findall(rf"\\{re.escape(command)}(?![A-Za-z])", text or ""))


def _balanced_latex_braces(text: str) -> bool:
    depth = 0
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _strip_latex_for_text(body: str) -> str:
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    return text.replace("`", "'")


def prepare_key_figures(bundle: PaperBundle, max_figures: Optional[int] = None) -> List[Tuple[Path, str]]:
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
    assets = [
        path
        for path in assets
        if path.exists() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".eps", ".ps"}
    ]
    for path in assets:
        if path.resolve() in skipped_assets:
            continue
        caption = _caption_for_asset(path, bundle.source_figures, bundle.source_captions, bundle.source_dir)
        figure = _figure_for_asset(path, bundle.source_figures, bundle.source_dir)
        if not caption or _is_non_content_asset(path, caption, figure):
            continue
        candidates.append((_figure_rank(path, figure), path, caption))

    candidates.sort(key=lambda item: item[0])
    unique_candidates: List[Tuple[Path, str]] = []
    seen_paths = set()
    for _rank, path, caption in candidates:
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        unique_candidates.append((path, caption))
    limit = len(unique_candidates) if max_figures is None else max(0, int(max_figures))
    return _render_key_figure_candidates(unique_candidates, output_dir, limit)


def _render_key_figure_candidates(candidates: List[Tuple[Path, str]], output_dir: Path, max_figures: int) -> List[Tuple[Path, str]]:
    if not candidates or max_figures <= 0:
        return []
    workers = _figure_render_workers()
    if workers <= 1 or len(candidates) == 1:
        figures: List[Tuple[Path, str]] = []
        for path, caption in candidates:
            rendered = _render_asset(path, output_dir)
            if rendered:
                figures.append((rendered, caption))
            if len(figures) >= max_figures:
                break
        return figures

    rendered_by_index: Dict[int, Optional[Path]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as executor:
        future_to_index = {
            executor.submit(_render_asset, path, output_dir): index
            for index, (path, _caption) in enumerate(candidates)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                rendered_by_index[index] = future.result()
            except Exception:
                rendered_by_index[index] = None

    figures: List[Tuple[Path, str]] = []
    for index, (_path, caption) in enumerate(candidates):
        rendered = rendered_by_index.get(index)
        if rendered:
            figures.append((rendered, caption))
        if len(figures) >= max_figures:
            break
    return figures


def _figure_render_workers() -> int:
    try:
        value = int(os.environ.get("MAXREAD_FIGURE_RENDER_WORKERS", "4"))
    except ValueError:
        value = 4
    return max(1, value)


def ensure_referenced_figure_markers(
    markdown: str,
    inserts: List[Tuple[str, Path, str]],
    max_missing: int = 1,
    visual_descriptions: Optional[Dict[str, str]] = None,
) -> str:
    """Only rescue one missing method overview figure; avoid unreadable figure piles."""
    if not inserts or max_missing <= 0:
        return markdown
    visual_descriptions = visual_descriptions or {}
    if _marker_count(markdown) >= 3:
        return markdown
    missing = [
        (marker, path, caption)
        for marker, path, caption in inserts
        if marker not in markdown and _is_priority_figure(path, caption, visual_descriptions.get(marker, ""))
    ]
    if not missing:
        return markdown
    lines = markdown.rstrip().splitlines()
    inserted = 0
    for marker, path, caption in missing[:max_missing]:
        insert_at = _section_insert_index(lines, "method")
        if insert_at is None:
            continue
        block = [
            "",
            "原文的方法总览图如下。",
            marker,
            f"图题：{_short_caption(caption or path.stem)}",
            "",
        ]
        lines = lines[:insert_at] + block + lines[insert_at:]
        inserted += 1
    if inserted == 0:
        return markdown
    return "\n".join(lines).strip() + "\n"


def _marker_count(markdown: str) -> int:
    return len(set(re.findall(r"\[MaxReadFigure:[^\]]+\]", markdown or "")))


def _is_referenced_or_result_figure(path: Path, caption: str = "", visual_description: str = "") -> bool:
    text = f"{path.stem} {caption or ''} {visual_description or ''}".lower()
    skip_words = ("logo", "icon")
    if any(word in text for word in skip_words):
        return False
    keep_words = (
        "overview", "workflow", "framework", "architecture", "mechanism",
        "comparison", "ablation", "rank", "loss", "variance", "attention",
        "time", "profile", "breakdown", "quality", "validation", "demo",
        "机制", "对比", "实验", "消融", "结果", "流程", "架构",
    )
    return any(word in text for word in keep_words)


def _figure_section_target(path: Path, caption: str = "", visual_description: str = "") -> str:
    path_text = path.stem.lower()
    context = f"{caption or ''} {visual_description or ''}".lower()
    text = f"{path_text} {context}"
    if any(word in text for word in (
        "ablation", "sensitivity", "appendix", "supplement", "failure", "scaling",
        "learnable sink", "with and without", "pilot", "training signal", "warmup",
        "detach", "sliding-window", "probe", "heatmap", "attention map", "visualization",
        "消融", "敏感", "附录", "失败", "探针", "热力图", "可视化",
    )):
        return "analysis"
    method_words = (
        "overview", "workflow", "framework", "architecture", "mechanism", "pipeline",
        "data construction", "model design", "流程", "架构", "框架", "机制", "数据构建",
    )
    experiment_words = (
        "experiment", "benchmark", "comparison", "result", "validation", "accuracy",
        "aggregate performance", "performance under", "qualitative", "quantitative",
        "latency", "throughput", "memory", "acc_vs", "acc-vs", "loss", "rank",
        "实验", "结果", "对比", "指标", "性能", "延迟", "吞吐", "显存",
    )
    method_score = 3 * sum(word in path_text for word in method_words) + sum(word in context for word in method_words)
    experiment_score = 3 * sum(word in path_text for word in experiment_words) + sum(word in context for word in experiment_words)
    if method_score and method_score >= experiment_score:
        return "method"
    if experiment_score:
        return "experiments"
    if "attention" in text and not any(word in text for word in ("compare", "comparison", "map", "pattern")):
        return "method"
    return ""


def _figure_lead_sentence(target: str) -> str:
    if target == "analysis":
        return "下面这张图补充了消融或扩展分析中的关键证据。"
    if target == "experiments":
        return "下面这张图对应实验结论中的关键对比或结果。"
    return "下面这张图对应方法描述中的关键机制或结构。"


def _section_insert_index(lines: List[str], target: str) -> Optional[int]:
    if not target:
        return None
    patterns_by_target = {
        "method": ("## 3.", "## 方法", "## 核心方法", "## 模型", "## 框架"),
        "experiments": ("## 4.", "## 实验", "## 结果", "## Evaluation", "## Experiments"),
        "analysis": ("## 5.", "## 消融", "## 补充", "## 分析", "## Ablation", "## Analysis"),
    }
    patterns = patterns_by_target.get(target, ())
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if any(stripped.startswith(pattern) for pattern in patterns):
            next_heading = _next_top_level_heading_index(lines, idx + 1)
            return next_heading if next_heading is not None else len(lines)
    return None


def _next_top_level_heading_index(lines: List[str], start: int) -> Optional[int]:
    for idx in range(start, len(lines)):
        if re.match(r"^##\s+", lines[idx].strip()):
            return idx
    return None



def _grouped_figure_items(bundle: PaperBundle, output_dir: Path) -> dict[str, list]:
    if not bundle.source_dir:
        return {"figures": [], "skip": []}
    by_key: dict[tuple[str, int, str, str], List[PaperFigure]] = defaultdict(list)
    for figure in bundle.source_figures:
        if _is_appendix_asset(bundle.source_dir / figure.asset, figure):
            continue
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
            if not path.exists() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf", ".eps", ".ps"}:
                continue
            rendered = _render_asset(path, output_dir)
            if rendered:
                items.append((rendered, figure))
        if len(items) < 2 or len(items) > 36:
            continue
        output_path = output_dir / f"{_safe_stem(label)}.png"
        if _should_compose_as_grid(items):
            composed = _compose_grid_figure(items, output_path, caption)
        else:
            composed = _compose_horizontal_figure(
                [path for path, _figure in items],
                output_path,
                caption,
                figures=[figure for _path, figure in items],
            )
        if composed:
            ranks = [_figure_rank(path, figure) for path, figure in items]
            rank = min(ranks) if ranks else (20, 0, str(composed))
            figures.append((composed, caption, rank))
            skip.extend((bundle.source_dir / figure.asset, caption) for _path, figure in items)
    return {"figures": figures, "skip": skip}

def _compose_horizontal_figure(
    paths: List[Path],
    output_path: Path,
    caption: str = "",
    figures: Optional[List[PaperFigure]] = None,
) -> Optional[Path]:
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
        labels = _panel_labels(figures or [])
        if len(labels) != len(resized):
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
                draw.text((text_x, padding + target_height + 8), label, fill=(20, 20, 20, 255), font=font)
            canvas.alpha_composite(image, (x, padding))
            x += image.width + gap
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path, format="PNG", optimize=True)
        return constrain_rendered_image(output_path)
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

        col_labels = [] if cols == 1 else _grid_column_labels(
            [(path, figure, row, col) for _image, path, figure, row, col in opened], cols
        )
        row_labels = [] if cols == 1 else _grid_row_labels(
            [(path, figure, row, col) for _image, path, figure, row, col in opened], rows
        )
        panel_labels = {
            (row, col): label
            for (_image, _path, figure, row, col), label in zip(resized, _panel_labels([item[2] for item in resized]))
            if label
        }
        font = _figure_label_font()
        measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1), "white"))
        panel_label_lines = {
            cell: _wrap_figure_label(measure_draw, label, font, cell_width - 20)
            for cell, label in panel_labels.items()
        } if font else {}
        line_height = _figure_text_line_height(measure_draw, font) if font else 0
        gap = 18 if cols >= 4 else 24
        padding = gap
        header_height = 48 if col_labels else 0
        row_label_width = 128 if row_labels else 0
        width = row_label_width + cols * cell_width + (cols - 1) * gap + padding * 2
        panel_label_heights = [
            max(
                [len(panel_label_lines.get((row, col), [])) * line_height + 16 for col in range(cols)]
                or [0]
            )
            for row in range(rows)
        ]
        height = header_height + sum(row_heights) + sum(panel_label_heights) + (rows - 1) * gap + padding * 2
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
                lines = panel_label_lines.get((row, col), [])
                if lines and font:
                    _draw_centered_multiline_text(
                        draw,
                        lines,
                        x,
                        y + row_heights[row],
                        cell_width,
                        panel_label_heights[row],
                        font,
                        line_height,
                    )
                x += cell_width + gap
            y += row_heights[row] + panel_label_heights[row] + gap

        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path, format="PNG", optimize=True)
        return constrain_rendered_image(output_path)
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


def _wrap_figure_label(draw, label: str, font, max_width: int, max_lines: int = 3) -> List[str]:
    words = re.sub(r"\s+", " ", str(label or "")).strip().split(" ")
    if not words:
        return []
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        suffix = "..."
        while lines[-1] and draw.textbbox((0, 0), lines[-1] + suffix, font=font)[2] > max_width:
            lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] += suffix
    return lines


def _figure_text_line_height(draw, font) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return max(1, bbox[3] - bbox[1] + 8)


def _draw_centered_multiline_text(draw, lines: List[str], x: int, y: int, width: int, height: int, font, line_height: int) -> None:
    block_height = len(lines) * line_height
    line_y = y + max(0, (height - block_height) // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        draw.text((x + max(0, (width - text_width) // 2), line_y), line, fill=(25, 25, 25, 255), font=font)
        line_y += line_height


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


def _panel_labels(figures: List[PaperFigure]) -> List[str]:
    if not figures or not any(str(figure.panel_caption or "").strip() for figure in figures):
        return []
    labels: List[str] = []
    for index, figure in enumerate(figures):
        caption = re.sub(r"\s+", " ", str(figure.panel_caption or "")).strip()
        if not caption:
            labels.append("")
            continue
        if re.match(r"^\s*\([a-z]\)", caption, re.I):
            labels.append(caption)
        else:
            letter = chr(ord("a") + index) if index < 26 else str(index + 1)
            labels.append(f"({letter}) {caption}")
    return labels


def _figure_label_font():
    try:
        from PIL import ImageFont
        cjk = _cjk_figure_font_path()
        for path in [
            cjk,
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]:
            if path and Path(path).exists():
                return ImageFont.truetype(path, 32)
        return ImageFont.load_default()
    except Exception:
        return None


def _cjk_figure_font_path() -> str:
    for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ):
        if Path(path).exists():
            return path
    return ""


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
        if _is_appendix_asset(path, figure):
            continue
        if _is_non_content_asset(path, figure.caption, figure):
            continue
        label = figure.label or ""
        max_for_label = 1 if _is_gallery_figure(path, figure) else 2
        if label and label_counts.get(label, 0) >= max_for_label:
            continue
        ranked.append((_figure_rank(path, figure), path))
        seen.add(path)
        if label:
            label_counts[label] = label_counts.get(label, 0) + 1
    fallback = []
    if not bundle.source_figures:
        fallback = [bundle.source_dir / asset for asset in bundle.source_assets]
        fallback = [
            path for path in fallback
            if path.exists()
            and path not in seen
            and not _is_appendix_asset(path, None)
            and not _is_non_content_asset(path, "", None)
        ]
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
    if _is_priority_figure(path, caption):
        rank = min(rank, 3)
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
    return bool(getattr(figure, "is_appendix", False)) or "appendix" in parts or "appendix" in label or "appendix" in tex_file


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


def _is_non_content_asset(path: Path, caption: str = "", figure: Optional[PaperFigure] = None) -> bool:
    text = "/".join(part.lower() for part in path.parts)
    stem = path.stem.lower()
    label = (figure.label or "").lower() if figure else ""
    caption_text = (caption or "").strip().lower()
    # A paper may keep real figures under e.g. ``presentation/assets``.
    # Only unreferenced assets are treated as decorative resources; a figure
    # declaration from TeX is stronger evidence than the directory name.
    if figure is None and any(part in text for part in ("/assets/", "/logo/", "/logos/", "/brand/", "/icon/", "/icons/")):
        return True
    if stem in {"logo", "mm", "minimax", "brand", "icon", "favicon"}:
        return True
    if any(word in stem for word in ("logo", "favicon", "brandmark")):
        return True
    if any(word in label for word in ("logo", "icon", "brand")):
        return True
    if caption_text in {"", path.name.lower(), stem}:
        return figure is None
    if re.fullmatch(r"(?:logo|icon|brand|mm|minimax)(?:\.\w+)?", caption_text):
        return True
    return False


def _frame_number(path: Path) -> int:
    match = re.search(r"frame_(\d+)", path.stem.lower())
    return int(match.group(1)) if match else 0


def _render_asset(path: Path, output_dir: Path) -> Optional[Path]:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        return path
    if suffix not in {".pdf", ".eps", ".ps"}:
        return None
    out_png = output_dir / f"{path.stem}.png"
    if out_png.exists() and out_png.stat().st_size > 0 and _has_sufficient_pdf_resolution(out_png):
        return constrain_rendered_image(out_png)
    if suffix in {".eps", ".ps"}:
        return _render_postscript_with_ghostscript(path, out_png)
    qlmanage = shutil.which("qlmanage")
    if qlmanage:
        tmp_dir = output_dir / f"{path.stem}_thumb"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [qlmanage, "-t", "-s", "1400", "-o", str(tmp_dir), str(path)],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            result = None
        generated = list(tmp_dir.glob("*.png")) if result is not None and result.returncode == 0 else []
        if generated:
            shutil.copyfile(generated[0], out_png)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return constrain_rendered_image(out_png)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        prefix = output_dir / f"{path.stem}__pdftoppm"
        try:
            result = subprocess.run(
                [pdftoppm, "-png", "-r", str(_render_dpi()), "-f", "1", "-l", "1", "-singlefile", str(path), str(prefix)],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            generated_png = output_dir / f"{path.stem}__pdftoppm.png"
            if generated_png.exists() and generated_png.stat().st_size > 0:
                shutil.move(str(generated_png), str(out_png))
                return constrain_rendered_image(out_png)

    rendered = _render_pdf_with_pymupdf(path, out_png)
    if rendered:
        return rendered

    return None


def _has_sufficient_pdf_resolution(path: Path) -> bool:
    """Invalidate old low-resolution PDF renders after the renderer improves."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            return max(image.size) >= 1000
    except Exception:
        return True


def _render_postscript_with_ghostscript(path: Path, out_png: Path) -> Optional[Path]:
    ghostscript, runtime_env = _ghostscript_runtime()
    if not ghostscript:
        return None
    out_png.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ghostscript,
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-dEPSCrop",
            "-sDEVICE=png16m",
            f"-r{_render_dpi()}",
            f"-sOutputFile={out_png}",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
        env=runtime_env,
    )
    if result.returncode == 0 and out_png.exists() and out_png.stat().st_size > 0:
        return constrain_rendered_image(out_png)
    out_png.unlink(missing_ok=True)
    return None


def _ghostscript_runtime() -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    system_binary = shutil.which("gs")
    if system_binary:
        return system_binary, None

    configured = str(os.environ.get("MAXREAD_GHOSTSCRIPT_ROOT", "") or "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".local/share/maxread-tools/ghostscript"
    binary = root / "usr/bin/gs"
    if not binary.is_file():
        return None, None

    env = dict(os.environ)
    library_dirs = [
        root / "usr/lib/x86_64-linux-gnu",
        root / "lib/x86_64-linux-gnu",
        root / "usr/lib",
    ]
    existing_library_path = str(env.get("LD_LIBRARY_PATH", "") or "").strip()
    library_path = [str(path) for path in library_dirs if path.is_dir()]
    if existing_library_path:
        library_path.append(existing_library_path)
    if library_path:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(library_path)

    resource_dirs: List[Path] = []
    for pattern in (
        "usr/share/ghostscript/*/Resource/Init",
        "usr/share/ghostscript/*/lib",
        "usr/share/ghostscript/*/Resource/Font",
        "usr/share/ghostscript/fonts",
        "usr/share/color/icc/ghostscript",
        "usr/share/fonts/X11/Type1",
    ):
        resource_dirs.extend(sorted(path for path in root.glob(pattern) if path.is_dir()))
    if resource_dirs:
        env["GS_LIB"] = os.pathsep.join(str(path) for path in resource_dirs)
    return str(binary), env


def _render_pdf_with_pymupdf(path: Path, out_png: Path) -> Optional[Path]:
    try:
        import pymupdf as fitz  # type: ignore
    except Exception:
        try:
            import fitz  # type: ignore
        except Exception:
            return None
    try:
        doc = fitz.open(str(path))
        if len(doc) < 1:
            doc.close()
            return None
        page = doc.load_page(0)
        # Keep PDF conversion around print-quality 200 DPI; the shared raster
        # bound below prevents large pages from producing hundred-MB PNGs.
        scale = _render_dpi() / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out_png))
        doc.close()
        return constrain_rendered_image(out_png) if out_png.exists() and out_png.stat().st_size > 0 else None
    except Exception:
        return None


def _render_dpi() -> int:
    try:
        value = int(os.environ.get("MAXREAD_FIGURE_RENDER_DPI", "200"))
    except ValueError:
        value = 200
    return min(300, max(120, value))


def constrain_rendered_image(
    path: Path,
    *,
    max_bytes: Optional[int] = None,
    max_side: Optional[int] = None,
    max_pixels: Optional[int] = None,
) -> Path:
    """Rewrite a generated raster at bounded resolution and encoded size."""
    try:
        from PIL import Image
    except Exception:
        return path
    if not path.exists() or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return path
    try:
        byte_limit = max_bytes or int(os.environ.get("MAXREAD_MAX_RENDERED_IMAGE_BYTES", str(10 * 1024 * 1024)))
        side_limit = max_side or int(os.environ.get("MAXREAD_MAX_RENDERED_IMAGE_SIDE", "3200"))
        pixel_limit = max_pixels or int(os.environ.get("MAXREAD_MAX_RENDERED_IMAGE_PIXELS", "16000000"))
    except ValueError:
        byte_limit, side_limit, pixel_limit = 10 * 1024 * 1024, 3200, 16_000_000
    byte_limit = max(256 * 1024, byte_limit)
    side_limit = max(640, side_limit)
    pixel_limit = max(1_000_000, pixel_limit)
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        scale = min(1.0, side_limit / max(image.size), (pixel_limit / max(1, image.width * image.height)) ** 0.5)
        if scale < 1.0:
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.LANCZOS,
            )
        temporary = path.with_name(f".{path.stem}.bounded.png")
        for _attempt in range(10):
            image.save(temporary, format="PNG", optimize=True, compress_level=9)
            if temporary.stat().st_size <= byte_limit:
                temporary.replace(path)
                return path
            ratio = (byte_limit / max(1, temporary.stat().st_size)) ** 0.5 * 0.92
            ratio = min(0.88, max(0.55, ratio))
            image = image.resize(
                (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
                Image.LANCZOS,
            )
        temporary.replace(path)
        return path
    except Exception:
        return path


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
    return ""
