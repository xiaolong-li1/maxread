from __future__ import annotations

import re


_MISSING = {"", "unknown", "top unknown", "—", "-"}
_TERM = re.compile(r"(大[一二三四][上下]?|研[一二三][上下]?)")


def normalize_academic_display(model_display: str, material_text: str) -> str:
    """Preserve every explicit academic metric without inventing a rank."""

    material = _normalize_text(material_text)
    model_parts = _model_parts(model_display)

    score = _score_from_material(material) or _model_score(model_parts)
    # The model often identifies the aggregate/current GPA while transcripts
    # contain many semester values. Prefer that aggregate, then recover GPA
    # from material only when the model omitted it entirely.
    gpa = _model_gpa(model_parts) or _gpa_from_material(material)
    model_rank = _model_rank(model_parts)
    rank = _merge_verified_model_top(_rank_from_material(material), model_rank) or model_rank

    output = [part for part in (score, gpa, rank or "排名未提供") if part]
    if not score and not gpa:
        output.insert(0, "成绩未提供")
    return " · ".join(dict.fromkeys(output))


def _normalize_text(value: str) -> str:
    return (
        str(value or "")
        .replace("，", ",")
        .replace("；", ";")
        .replace("：", ":")
    )


def _model_parts(value: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\s*[·|]\s*", _normalize_text(value))
        if part.strip().casefold() not in _MISSING
    ]


def _score_from_material(text: str) -> str:
    aggregate_patterns = (
        r"(?:学年|综合|总)[^,;\n]{0,12}(?:均分|平均分|成绩)\s*(?:为|[:])?\s*(\d{1,3}(?:\.\d+)?)",
        r"(?:均分|平均分)\s*(?:为|[:])?\s*(\d{1,3}(?:\.\d+)?)\s*(?:/\s*100)?",
    )
    for index, pattern in enumerate(aggregate_patterns):
        matches = re.findall(pattern, text, flags=re.I)
        if matches:
            value = _number(matches[-1])
            return f"均分 {value}/100"

    converted = []
    for line in text.splitlines():
        if not re.search(r"GPA|绩点", line, flags=re.I):
            continue
        converted.extend(re.findall(r"[（(]\s*(\d{1,3}(?:\.\d+)?)\s*/\s*100\s*[）)]", line))
    if converted:
        return f"百分制 {_number(converted[-1])}/100"
    weighted = re.findall(r"绩点\s*[:]?\s*(\d{2,3}(?:\.\d+)?)", text, flags=re.I)
    if weighted:
        return f"均分 {_number(weighted[-1])}/100"
    return ""


def _gpa_from_material(text: str) -> str:
    pattern = re.compile(
        r"(?:GPA|绩点)\s*[:]?\s*(\d{1,3}(?:\.\d+)?)\s*(?:/\s*(\d(?:\.\d+)?))?",
        flags=re.I,
    )
    values: list[tuple[str, str, str]] = []
    for match in pattern.finditer(text):
        if float(match.group(1)) > 5:
            continue
        value = _number(match.group(1), minimum_decimals=2)
        scale = _number(match.group(2), minimum_decimals=2) if match.group(2) else ""
        prefix = text[max(0, match.start() - 30) : match.start()]
        terms = _TERM.findall(prefix)
        term = terms[-1] if terms else ""
        item = (value, scale, term)
        if item not in values:
            values.append(item)
    if not values:
        return ""
    rendered = []
    for value, scale, term in values[:3]:
        metric = f"{value}/{scale}" if scale else value
        rendered.append(f"{metric}（{term}）" if term else metric)
    return "GPA " + "、".join(rendered)


def _rank_from_material(text: str) -> str:
    percent_rank = ""
    for line in text.splitlines():
        match = re.search(r"排名\s*[:]?\s*(\d+(?:\.\d+)?)\s*%", line, flags=re.I)
        if match:
            percent_rank = f"Top {_number(match.group(1))}%"
            break
    pattern = re.compile(
        r"(?P<label>专业|年级|综合|综测|裸绩|班级|成绩|绩点)?排名\s*(?:第|为|[:])?\s*(?P<rank>\d+)(?![\d.])\s*(?:名)?(?:\s*/\s*(?P<total>\d+))?",
        flags=re.I,
    )
    entries: list[tuple[str, int, int, str]] = []
    for match in pattern.finditer(text):
        label = str(match.group("label") or "")
        if not label:
            prefix = text[max(0, match.start() - 36) : match.start()]
            prefix = prefix.rsplit("\n", 1)[-1]
            labels = re.findall(r"专业|年级|综合|综测|裸绩|班级|成绩|绩点", prefix)
            label = labels[-1] if labels else ""
        rank = int(match.group("rank"))
        total = int(match.group("total")) if match.group("total") else 0
        prefix = f"{label}排名" if label else "排名"
        base = f"{prefix}第 {rank}/{total}" if total else f"{prefix}第 {rank} 名（总人数未提供）"
        nearby = text[match.end() : match.end() + 24]
        percent = re.search(r"[（(]?\s*(\d+(?:\.\d+)?)\s*%\s*[）)]?", nearby)
        if percent:
            base += f"（Top {_number(percent.group(1))}%）"
        entries.append((label, rank, total, base))
    rendered: list[str] = []
    for label, rank, total, base in entries:
        if total == 0 and any(other_rank == rank and other_total for _other_label, other_rank, other_total, _base in entries):
            continue
        if not label and any(other_label and other_rank == rank and other_total == total for other_label, other_rank, other_total, _base in entries):
            continue
        if base not in rendered:
            rendered.append(base)
    if rendered:
        return "、".join(rendered[:3])

    named = re.search(r"第\s*(\d+)\s*名", text)
    if named:
        return f"排名第 {int(named.group(1))} 名（总人数未提供）"

    if percent_rank:
        return percent_rank
    for line in text.splitlines():
        if not re.search(r"GPA|绩点|排名|均分|平均分|专业成绩", line, flags=re.I):
            continue
        top_match = re.search(r"(?:Top\s*|前\s*)(\d+(?:\.\d+)?)\s*%", line, flags=re.I)
        if top_match:
            return f"Top {_number(top_match.group(1))}%"
    return ""


def _merge_verified_model_top(material_rank: str, model_rank: str) -> str:
    if not material_rank or "Top " in material_rank or not model_rank:
        return material_rank
    top_match = re.search(r"Top\s*(\d+(?:\.\d+)?)\s*%", model_rank, flags=re.I)
    ratio_match = re.search(r"第\s*(\d+)\s*/\s*(\d+)", material_rank)
    if not top_match or not ratio_match:
        return material_rank
    top = float(top_match.group(1))
    calculated = 100 * int(ratio_match.group(1)) / max(1, int(ratio_match.group(2)))
    if abs(top - calculated) > 0.6:
        return material_rank
    pieces = material_rank.split("、", 1)
    pieces[0] += f"（Top {_number(top_match.group(1))}%）"
    return "、".join(pieces)


def _model_score(parts: list[str]) -> str:
    for part in parts:
        if _is_rank_part(part) or re.search(r"\bGPA\b|绩点", part, flags=re.I):
            continue
        match = re.fullmatch(r"(?:均分\s*|百分制\s*)?(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", part)
        if match:
            value, scale = _number(match.group(1)), _number(match.group(2))
            if float(match.group(2)) != 100:
                continue
            label = "百分制" if "百分制" in part else "均分"
            return f"{label} {value}/{scale}"
        if part.casefold() not in _MISSING:
            return part
    return ""


def _model_gpa(parts: list[str]) -> str:
    for part in parts:
        if re.search(r"\bGPA\b|绩点", part, flags=re.I) and "排名" not in part:
            return part.replace("绩点", "GPA", 1)
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", part)
        if match and float(match.group(2)) <= 5:
            return f"GPA {_number(match.group(1), 2)}/{_number(match.group(2), 2)}"
    return ""


def _model_rank(parts: list[str]) -> str:
    explicit = next((part for part in parts if _is_rank_part(part) and "unknown" not in part.casefold()), "")
    if explicit:
        return explicit
    for part in parts:
        match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", part)
        if match and int(match.group(2)) != 100 and int(match.group(2)) > 5:
            return f"排名第 {int(match.group(1))}/{int(match.group(2))}"
    return ""


def _is_rank_part(value: str) -> bool:
    return bool(re.search(r"Top\s*\d|前\s*\d|排名|第\s*\d+\s*名", value, flags=re.I))


def _number(value: str, minimum_decimals: int = 0) -> str:
    number = float(value)
    if minimum_decimals:
        source_decimals = len(str(value).partition(".")[2].rstrip("0"))
        precision = max(minimum_decimals, source_decimals)
        return f"{number:.{precision}f}"
    return str(int(number)) if number.is_integer() else f"{number:g}"
