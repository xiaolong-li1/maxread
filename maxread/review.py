from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, List

from .openai_client import OpenAIClient


REVIEW_SYSTEM_PROMPT = r"""你是 MaxRead 的发布前质量检查员。
你的任务不是重写文章，而是在不改变事实内容的前提下，修正即将上传到飞书文档的 Markdown，并记录你发现的问题。

硬规则：
1. 只输出 JSON，不要解释、不要加代码围栏。
2. JSON 格式必须是：{"markdown":"修正后的 Markdown", "issues":[{"category":"tex_macro|english_caption|figure_marker|math|table|heading|layout|factual_risk|other", "severity":"low|medium|high", "detail":"简短中文说明"}]}。
   `issues` 只记录修订后仍未解决、需要人工注意的问题；不要把“已修正/已清理/已保留/已改为”的修改记录放进 issues。
3. 不允许新增论文/文章里没有的事实、指标、结论、数据集或评价。
4. 必须保留原稿中已经出现的 [MaxReadFigure:...] marker，逐字一致，独立成行；严禁把原稿没有使用的候选 marker 补回文末或补到任意段落。
5. 图解 `**图：...**` 必须用中文转述，不能直接复制长英文 caption。
6. 清理明显 TeX 残留：例如 \formername、\localattentionname、\xspace、\textsc{}、\cite{}、\ref{}。如果能从上下文看出真实名称就展开；否则改成自然中文描述。
7. 检查标题层级、表格、公式 `<latex>...</latex>`、列表和段落换行，修复明显格式错误。
   H1 标题必须自然、短、具体；如果像机器翻译、摘要压缩，或出现“深网/复刻函数/模型压缩训练/函数逼近”这类抽象废话/抽象空话，应改成读者熟悉的技术表达。
8. 中文文档中不要保留整段英文解释；方法名、模型名、数据集名、指标名可以保留英文。
9. 不要把图片集中移动到文末；只做局部格式修复。
10. 检查图文是否错位：如果 `**图：...**` 的解释和 marker 周围段落主题明显不一致，移动到更合适的相邻段落；无法判断时记录 `figure_marker` issue，不要臆造图意。
11. 不要修改 `<latex>...</latex>` 内部的反斜杠命令，例如 `\left`、`\right`、`\cdot`、`\lambda`、`\nabla`、`\partial`。
12. 如果问题都已经修好，issues 输出空数组；不要为了说明你做了什么而写 issue。
"""


@dataclass
class ReviewIssue:
    category: str
    severity: str
    detail: str


@dataclass
class ReviewResult:
    markdown: str
    issues: List[ReviewIssue] = field(default_factory=list)
    raw: str = ""


def review_markdown(llm: OpenAIClient, markdown: str, markers: Iterable[str], kind: str = "paper") -> str:
    return review_markdown_with_report(llm, markdown, markers, kind).markdown


def review_markdown_with_report(llm: OpenAIClient, markdown: str, markers: Iterable[str], kind: str = "paper") -> ReviewResult:
    raw = llm.responses_text(REVIEW_SYSTEM_PROMPT, build_review_user_prompt(markdown, markers, kind))
    result = parse_review_response(raw)
    if _has_non_json_review_issue(result):
        issues = list(result.issues)
        if _looks_like_model_refusal(result.markdown):
            issues.append(ReviewIssue("other", "medium", "review returned refusal; kept original markdown"))
        return ReviewResult(markdown=markdown.strip() + "\n", issues=issues, raw=raw)
    return result


def build_review_user_prompt(markdown: str, markers: Iterable[str], kind: str = "paper") -> str:
    marker_text = "\n".join(f"- {marker}" for marker in markers) or "- 无"
    return rf"""请检查并修正下面这份 MaxRead {'论文' if kind == 'paper' else '网页文章'} Markdown，准备上传到飞书文档。

重点检查：
- H1 标题是否自然、短、具体；是否有机器翻译腔、抽象废话或过长副标题。
- 是否有长英文 caption 被直接复制到正文。
- 是否有 TeX 宏残留，例如 \formername、\localattentionname。
- 原稿已经使用的图片 marker 是否保留且独立成行，且周围图解是否和段落主题一致。候选清单里但原稿没用的 marker 不要补回；如果你只是按要求保留/未补回 marker，不要写入 issues，只有 marker 丢失、错位且无法修复时才写 issue。
- marker 后缀/文件名可能是截图名、hash、临时文件名或无意义缩写；不要仅凭 marker 名称像机构、Logo、缩写或文件名就记录 `figure_marker` issue。
- 公式是否仍是 `<latex>...</latex>`，公式内部反斜杠命令是否被保留。
- 表格是否仍是合法 Markdown 表格。
- 方法/实验事实不能被你改写成新结论。

候选图片 marker 清单（用于检查；只有原稿里已经出现的 marker 必须保留，原稿没出现的不要新增）：
{marker_text}

待检查 Markdown：
```markdown
{markdown}
```
"""


def parse_review_response(text: str) -> ReviewResult:
    raw = text
    stripped = _strip_fences(text).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return ReviewResult(markdown=_strip_fences(text).strip() + "\n", issues=[ReviewIssue("other", "low", "review returned non-json markdown")], raw=raw)
    if not isinstance(payload, dict):
        return ReviewResult(markdown=stripped + "\n", issues=[ReviewIssue("other", "medium", "review returned non-object json")], raw=raw)
    markdown = str(payload.get("markdown") or "").strip()
    if not markdown:
        markdown = stripped
    issues = []
    for item in payload.get("issues") or []:
        if not isinstance(item, dict):
            continue
        category = _clean_enum(item.get("category"), "other")
        severity = _clean_enum(item.get("severity"), "low")
        detail = str(item.get("detail") or "").strip()[:1000]
        issue = ReviewIssue(category, severity, detail)
        if detail and is_unresolved_review_issue(issue):
            issues.append(issue)
    return ReviewResult(markdown=_strip_fences(markdown).strip() + "\n", issues=issues, raw=raw)


def _has_non_json_review_issue(result: ReviewResult) -> bool:
    return any(issue.category == "other" and "non-json" in issue.detail for issue in result.issues)


def _looks_like_model_refusal(markdown: str) -> bool:
    text = " ".join(str(markdown or "").lower().split())
    refusals = (
        "i'm sorry, but i cannot assist",
        "i’m sorry, but i cannot assist",
        "i cannot assist with that request",
        "i can't assist with that request",
        "sorry, i can’t help",
        "抱歉，我无法",
        "不能协助",
    )
    return any(item in text for item in refusals)


def visible_review_issues(rows):
    return [row for row in rows if is_unresolved_review_issue(row)]


def is_unresolved_review_issue(issue) -> bool:
    detail = _issue_detail(issue)
    if not detail:
        return False
    if _is_low_value_marker_name_issue(issue, detail):
        return False
    unresolved = (
        "仍有", "仍然", "仍存在", "未解决", "未修复", "尚未",
        "无法判断", "无法确认", "无法修复", "需要人工", "需要检查",
        "可能错误", "可能不", "疑似", "存在风险", "事实风险",
    )
    if any(token in detail for token in unresolved):
        return True
    resolved = (
        "已修", "修正", "清理", "已清", "已改", "改为", "调整为", "中文化",
        "已保留", "均已保留", "未新增", "未补回", "按要求", "避免",
        "保持", "未改变", "更自然", "已简", "已补", "已统一",
    )
    if any(token in detail for token in resolved):
        return False
    return True


def _is_low_value_marker_name_issue(issue, detail: str) -> bool:
    category = issue.get("category", "") if isinstance(issue, dict) else getattr(issue, "category", "")
    if str(category) != "figure_marker":
        return False
    name_only = ("marker 名称" in detail or "marker名" in detail or "文件名" in detail) and ("像机构" in detail or "Logo" in detail or "缩写" in detail)
    has_real_misalignment = any(token in detail for token in ("错位", "周围段落", "丢失"))
    return name_only and not has_real_misalignment


def _issue_detail(issue) -> str:
    if isinstance(issue, dict):
        return str(issue.get("detail", "") or "").strip()
    return str(getattr(issue, "detail", "") or "").strip()


def _clean_enum(value, default: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    return text or default


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped[len("```json"): -3].strip()
    if stripped.startswith("```markdown") and stripped.endswith("```"):
        return stripped[len("```markdown"): -3].strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped[3:-3].strip()
    return text
