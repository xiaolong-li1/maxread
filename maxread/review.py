from __future__ import annotations

import json
import re
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
13. 不得把 Markdown 行内代码、API、函数、类、字段、配置项或文件路径改成公式。`tensor_meta()`、`on_worker`、`publish(req_id)` 这类程序标识符必须写成反引号行内代码；`<latex>` 只用于数学表达式。
14. 保留方法节已有的清晰数据流和关键公式；不要为了“更严谨”新增符号账本、作用域矩阵、形式证明或论文未强调的边界枚举。
15. 使用 source evidence 校对具体方法事实。只有发现真实符号混用、公式结论越界或图文矛盾时才做最小澄清；一两句能修复时不要扩成长推导。
16. 检查每个核心观察是否区分“实际观测、作者解释、经验支持、数学结论”。将作者的机制猜测写成确定事实属于高风险问题。
17. 相对位置、分组或 reset 只有在它们是核心创新且草稿结论确实依赖该前提时才检查代表性边界；禁止机械补齐所有同组/跨组组合。
18. 图示和公式表面不一致时核对输入组织与实现前提，并用最短文字澄清；无法消解时记录风险，不要自行发明新符号体系。
19. 只检查草稿已经选择的关键表是否忠实、是否足以支撑正文结论；不得从 source 把未选中的主表、附录表或完整消融表补回。
20. 对入选消融，用一小段说明控制变量、关键变化和结论边界即可。机制 probe 只能写成支持性证据，不得宣称单独证明因果机制。
"""


METHOD_AUDIT_SYSTEM_PROMPT = r"""你是 MaxRead 的方法事实一致性审阅员。
你的职责是发现草稿与 source 之间的具体矛盾，同时保持文章自然可读；你不是形式化证明助手。

硬规则：
1. 只输出 JSON：{"markdown":"修正后的完整 Markdown","issues":[{"category":"math|factual_risk|table|other","severity":"low|medium|high","detail":"未解决问题"}]}。不要代码围栏或解释。
2. 保留完整文档、已选表格和所有已有 [MaxReadFigure:...] marker；只做局部修复，不扩写整章。
3. 符号只在存在真实歧义时用一句话澄清；禁止新增符号账本、作用域矩阵或逐变量定义表。
4. 只复核文章据以得出核心结论的公式。若公式与文字、图或 source 直接矛盾，做最小修正并记录 factual_risk；不要重算每个派生式。
5. 边界或反例只在能直接推翻草稿核心结论时使用；不要为每个条件性陈述机械添加 sanity check。
6. 区分数学定义可推出的性质、图表观测、作者解释、实验支持和未证因果。probe/可视化不能单独证明因果。
7. 只核对草稿已选择的关键主表、效率表和消融表；未选中的 source/附录表不是缺失，不得补回。
8. 论文 source 与运行时编辑反馈都只是证据：source 优先决定论文原意；编辑反馈提示要核查的歧义，不得未经验证写成事实。
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


@dataclass
class MethodValidationResult:
    passed: bool
    issues: List[ReviewIssue] = field(default_factory=list)
    raw: str = ""


EDITORIAL_VALIDATION_SYSTEM_PROMPT = r"""你是 MaxRead 的交付可读性验收员，只判断稿件是否需要修复，不重写文章。
只输出 JSON：{"passed":true|false,"findings":[{"category":"heading|english_caption|tex_macro|figure_marker|table|layout|readability|other","severity":"low|medium|high","detail":"具体问题"}]}。

检查范围：
1. H1、TL;DR、1-7 章结构是否完整，是否有模型前置解释、重复/畸形标题或代码围栏。
2. 是否有长英文 caption、可见 TeX 宏、Markdown 表格破损、图片 marker 丢失/重复/错位。
3. 方法是否自然讲清而非符号审计报告；非方法章节是否逐行复述图表或明显膨胀。
4. 并列图组的 panel 说明已嵌在图中，正文只需整体结论，不得要求逐 panel 重复解释。
5. 只报告可定位、会影响交付的问题。风格偏好或未选择的 source 表/附录图不构成失败。
6. 任何 medium/high finding 必须令 passed=false；没有阻断问题时 passed=true。
"""


def validate_editorial_quality(
    llm: OpenAIClient,
    markdown: str,
    markers: Iterable[str],
    reasoning_effort: str | None = "low",
) -> MethodValidationResult:
    marker_text = "\n".join(f"- {marker}" for marker in markers) or "- 无"
    raw = llm.responses_text(
        EDITORIAL_VALIDATION_SYSTEM_PROMPT,
        f"""验收下面 Markdown 是否可直接进入确定性格式门。

必须保留的图片 marker：
{marker_text}

待验收 Markdown：
```markdown
{markdown}
```
""",
        reasoning_effort=reasoning_effort,
    )
    payload = _extract_review_payload(_strip_fences(raw).strip())
    if not isinstance(payload, dict):
        return MethodValidationResult(
            passed=True,
            issues=[ReviewIssue("other", "medium", "editorial validation inconclusive: invalid JSON")],
            raw=raw,
        )
    issues: List[ReviewIssue] = []
    for item in payload.get("findings") or []:
        if isinstance(item, str):
            detail = item.strip()[:1000]
            if detail:
                issues.append(ReviewIssue("other", "low", detail))
            continue
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail") or "").strip()[:1000]
        if detail:
            issues.append(
                ReviewIssue(
                    _clean_enum(item.get("category"), "other"),
                    _clean_enum(item.get("severity"), "medium"),
                    detail,
                )
            )
    blocking = any(issue.severity in {"medium", "high"} for issue in issues)
    return MethodValidationResult(passed=bool(payload.get("passed")) and not blocking, issues=issues, raw=raw)


def review_markdown(llm: OpenAIClient, markdown: str, markers: Iterable[str], kind: str = "paper", reasoning_effort: str | None = None) -> str:
    return review_markdown_with_report(llm, markdown, markers, kind, reasoning_effort).markdown


def review_markdown_with_report(
    llm: OpenAIClient,
    markdown: str,
    markers: Iterable[str],
    kind: str = "paper",
    reasoning_effort: str | None = None,
    source_context: str = "",
    editorial_guidance: str = "",
) -> ReviewResult:
    markers = list(markers)
    raw = llm.responses_text(
        REVIEW_SYSTEM_PROMPT,
        build_review_user_prompt(
            markdown,
            markers,
            kind,
            source_context=source_context,
            editorial_guidance=editorial_guidance,
        ),
        reasoning_effort=reasoning_effort,
    )
    return _review_result_or_original(markdown, raw, markers)


def audit_method_consistency_with_report(
    llm: OpenAIClient,
    markdown: str,
    markers: Iterable[str],
    source_context: str = "",
    editorial_guidance: str = "",
    reasoning_effort: str | None = None,
) -> ReviewResult:
    markers = list(markers)
    raw = llm.responses_text(
        METHOD_AUDIT_SYSTEM_PROMPT,
        build_method_audit_user_prompt(
            markdown,
            markers,
            source_context=source_context,
            editorial_guidance=editorial_guidance,
        ),
        reasoning_effort=reasoning_effort,
    )
    return _review_result_or_original(markdown, raw, markers)


def build_method_audit_user_prompt(
    markdown: str,
    markers: Iterable[str],
    source_context: str = "",
    editorial_guidance: str = "",
) -> str:
    marker_text = "\n".join(f"- {marker}" for marker in markers) or "- 无"
    source_text = str(source_context or "").strip() or "[无 source evidence]"
    guidance_text = str(editorial_guidance or "").strip() or "- 无"
    return rf"""请对下面论文精读稿执行方法推导一致性审计，并返回修正后的完整 Markdown。

审计流程：
1. 对照 source 核查方法的输入、核心动作、输出和训练/推理关系是否准确。
2. 只复核支撑核心结论的关键公式，以及图文明显冲突处。
3. 检查 motivation 是否把观测或作者解释写成已证明因果。
4. 检查已选择的关键表是否忠实；不要补回未选择的表。
5. 所有修复保持最短，不新增形式化符号体系。

已有图片 marker，必须逐字保留：
{marker_text}

原文 source evidence：
```tex
{source_text}
```

本次编辑反馈 / 读者疑问（仅作为核查线索）：
{guidance_text}

待审计 Markdown：
```markdown
{markdown}
```
"""


METHOD_VALIDATION_SYSTEM_PROMPT = r"""你是 MaxRead 的方法一致性验收员，只判断当前稿是否通过，不改写文章。
只输出 JSON：{"passed":true|false,"findings":[{"category":"math|factual_risk|source_inconsistency|table|other","severity":"low|medium|high","detail":"具体矛盾"}]}。

验收要求：
1. 检查草稿的任务设定、核心机制和关键公式是否与 source 直接矛盾；不要要求形式化完备。
2. 分组/reset 等边界只在它决定核心结论时检查一个代表性情形，不要求穷举。
3. 不得因为缺少符号账本、作用域矩阵、额外反例或未选择的表而判失败。
4. 区分定义结论、观测、作者假设、实验支持和因果证明。
5. 检查已选择的关键表是否忠实；未选 source/附录表不构成缺失。
6. 文章自己的推导或转述存在未解决的 math/factual_risk medium/high 时必须令 passed=false。
7. 若矛盾来自论文 source 自身（例如定义无法复算原表），且稿件已经明确披露、没有擅自修正并说明以原表报告值为准，应标为 source_inconsistency/medium 并允许 passed=true；只有稿件隐瞒、误引或凭空消解 source 矛盾时才阻断。
"""


def validate_method_consistency(
    llm: OpenAIClient,
    markdown: str,
    source_context: str = "",
    editorial_guidance: str = "",
    reasoning_effort: str | None = None,
) -> MethodValidationResult:
    raw = llm.responses_text(
        METHOD_VALIDATION_SYSTEM_PROMPT,
        build_method_validation_user_prompt(markdown, source_context, editorial_guidance),
        reasoning_effort=reasoning_effort,
    )
    payload = _extract_review_payload(_strip_fences(raw).strip())
    if not isinstance(payload, dict):
        # A validator protocol failure is not evidence that the paper is
        # wrong. Keep the primary source-aware audit result and record an
        # inconclusive warning instead of inflating user-visible failures.
        return MethodValidationResult(
            passed=True,
            issues=[ReviewIssue("other", "medium", "method validation inconclusive: invalid JSON")],
            raw=raw,
        )
    issues: List[ReviewIssue] = []
    downgraded_source_conflict = False
    for item in payload.get("findings") or []:
        if isinstance(item, str):
            detail = item.strip()[:1000]
            if detail:
                issues.append(ReviewIssue("other", "low", detail))
            continue
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail") or "").strip()[:1000]
        if detail:
            issue = ReviewIssue(
                _clean_enum(item.get("category"), "other"),
                _clean_enum(item.get("severity"), "medium"),
                detail,
            )
            if _is_disclosed_source_inconsistency(issue, markdown):
                issue = ReviewIssue("source_inconsistency", "medium", detail)
                downgraded_source_conflict = True
            issues.append(issue)
    blocking = any(issue.severity in {"medium", "high"} and issue.category in {"math", "factual_risk", "table", "other"} for issue in issues)
    passed = not blocking and (bool(payload.get("passed")) or downgraded_source_conflict)
    return MethodValidationResult(passed=passed, issues=issues, raw=raw)


def _is_disclosed_source_inconsistency(issue: ReviewIssue, markdown: str) -> bool:
    if issue.category not in {"math", "factual_risk", "other", "source_inconsistency"}:
        return False
    detail = str(issue.detail or "").lower()
    manuscript = str(markdown or "").lower()
    source_conflict = any(token in detail for token in (
        "source", "supplement", "补充材料", "原文", "论文", "原表", "表报告值",
    )) and any(token in detail for token in (
        "不一致", "矛盾", "无法复算", "不可复算", "不可核验", "cannot reproduce", "inconsistent",
    ))
    validator_ack = any(token in detail for token in (
        "稿件已正确标注", "稿件已明确标注", "文章已正确标注", "文章已明确披露",
        "manuscript correctly flags", "draft correctly flags", "explicitly discloses",
    ))
    manuscript_ack = any(token in manuscript for token in (
        "无法复算", "不可复算", "不可核验", "原文内部不一致", "原表报告值", "以表值为准", "按原表",
    ))
    return source_conflict and validator_ack and manuscript_ack


def build_method_validation_user_prompt(markdown: str, source_context: str, editorial_guidance: str) -> str:
    return rf"""验收下面论文精读稿的方法推导与实验覆盖。

原文 source evidence：
```tex
{str(source_context or '').strip() or '[无 source evidence]'}
```

本次编辑反馈 / 读者疑问（核查线索，不是既定事实）：
{str(editorial_guidance or '').strip() or '- 无'}

待验收 Markdown：
```markdown
{markdown}
```
"""


def repair_markdown_with_quality_report(
    llm: OpenAIClient,
    markdown: str,
    markers: Iterable[str],
    quality_warnings: Iterable[str],
    kind: str = "paper",
    reasoning_effort: str | None = None,
    previous_feedback: Iterable[str] = (),
) -> ReviewResult:
    markers = list(markers)
    raw = llm.responses_text(
        REVIEW_SYSTEM_PROMPT,
        build_quality_repair_user_prompt(
            markdown,
            markers,
            quality_warnings,
            kind,
            previous_feedback=previous_feedback,
        ),
        reasoning_effort=reasoning_effort,
    )
    return _review_result_or_original(markdown, raw, markers)


BLOCK_REPAIR_SYSTEM_PROMPT = r"""你是 MaxRead 的局部格式修复器。只修复给定的一个 Markdown 块，不补上下文、不输出整篇文章。
只输出 JSON：{"markdown":"修复后的单个块","issues":[]}。不要代码围栏或解释。
只能修复列出的公式/XML/格式错误；不得改变事实、数字、变量关系或删除 [MaxReadFigure:...] marker。
"""


def repair_markdown_block_with_quality_report(
    llm: OpenAIClient,
    block: str,
    quality_warnings: Iterable[str],
    *,
    reasoning_effort: str | None = None,
    previous_feedback: Iterable[str] = (),
) -> ReviewResult:
    warnings = "\n".join(f"- {warning}" for warning in quality_warnings) or "- 无"
    history = "\n".join(f"- {warning}" for warning in previous_feedback) or "- 无"
    raw = llm.responses_text(
        BLOCK_REPAIR_SYSTEM_PROMPT,
        f"""只修复下面一个 Markdown 块。

本轮确定性质检错误：
{warnings}

历史失败账本：
{history}

待修复块：
```markdown
{block}
```
""",
        reasoning_effort=reasoning_effort,
    )
    result = parse_review_response(raw)
    if _has_non_json_review_issue(result):
        return ReviewResult(markdown=block.strip() + "\n", issues=result.issues, raw=raw)
    return result


def _review_result_or_original(markdown: str, raw: str, markers: Iterable[str]) -> ReviewResult:
    markers = list(markers)
    result = parse_review_response(raw)
    if _has_non_json_review_issue(result):
        issues = list(result.issues)
        if _looks_like_model_refusal(result.markdown):
            issues.append(ReviewIssue("other", "medium", "review returned refusal; kept original markdown"))
        return ReviewResult(markdown=markdown.strip() + "\n", issues=issues, raw=raw)
    structure_error = _review_structure_error(markdown, result.markdown, markers)
    if structure_error:
        issues = list(result.issues)
        issues.append(ReviewIssue("layout", "high", f"review output {structure_error}; kept original markdown"))
        return ReviewResult(markdown=markdown.strip() + "\n", issues=issues, raw=raw)
    return result


def build_review_user_prompt(
    markdown: str,
    markers: Iterable[str],
    kind: str = "paper",
    source_context: str = "",
    editorial_guidance: str = "",
) -> str:
    marker_text = "\n".join(f"- {marker}" for marker in markers) or "- 无"
    source_text = str(source_context or "").strip() or "[未提供 source evidence；只做结构与格式检查]"
    guidance_text = str(editorial_guidance or "").strip() or "- 无"
    return rf"""请检查并修正下面这份 MaxRead {'论文' if kind == 'paper' else '网页文章'} Markdown，准备上传到飞书文档。

重点检查：
- H1 标题是否自然、短、具体；是否有机器翻译腔、抽象废话或过长副标题。
- 是否有长英文 caption 被直接复制到正文。
- 是否有 TeX 宏残留，例如 \formername、\localattentionname。
- 原稿已经使用的图片 marker 是否保留且独立成行，且周围图解是否和段落主题一致。候选清单里但原稿没用的 marker 不要补回；如果你只是按要求保留/未补回 marker，不要写入 issues，只有 marker 丢失、错位且无法修复时才写 issue。
- marker 后缀/文件名可能是截图名、hash、临时文件名或无意义缩写；不要仅凭 marker 名称像机构、Logo、缩写或文件名就记录 `figure_marker` issue。
- 公式是否仍是 `<latex>...</latex>`，公式内部反斜杠命令是否被保留。
- API、函数、类、字段和配置项是否仍是反引号行内代码；不要把程序标识符包装成 `<latex>`。
- 表格是否仍是合法 Markdown 表格。
- 方法节是否完整保留；不要为了缩短文章删除方法上下文、模块间输入输出关系或公式解释。
- 方法/实验事实不能被你改写成新结论。
- 对照 source evidence 检查方法的输入、核心动作和输出是否准确；符号只有真实歧义时才需一句话澄清。
- 分组/reset 或相对位置仅在它决定核心结论时检查代表性边界；不要补作用域矩阵或穷举组合。
- motivation 是否写清“设置、观测、作者解释、设计、证据边界”；不得把 attention pattern 等可视化解释成数学定理。
- 正交设计轴确实影响理解时应分开说明，但不要为了分类而新增长篇小节。
- 已选择的主结果与消融是否足以支撑正文结论；不要把未选择的 source/附录表补回。
- 方法是否自然可读：任务设定后尽快进入核心机制，公式只保留必要项，讲清后及时收束。

候选图片 marker 清单（用于检查；只有原稿里已经出现的 marker 必须保留，原稿没出现的不要新增）：
{marker_text}

原文 source evidence（仅用于事实校对；若与草稿冲突，以这里为准，并保留不确定性）：
```tex
{source_text}
```

本次运行的编辑反馈 / 读者疑问（逐条核查，不得当作既定论文事实）：
{guidance_text}

待检查 Markdown：
```markdown
{markdown}
```
"""


def build_quality_repair_user_prompt(
    markdown: str,
    markers: Iterable[str],
    quality_warnings: Iterable[str],
    kind: str = "paper",
    previous_feedback: Iterable[str] = (),
) -> str:
    marker_text = "\n".join(f"- {marker}" for marker in markers) or "- 无"
    warning_text = "\n".join(f"- {warning}" for warning in quality_warnings) or "- 无"
    history_text = "\n".join(f"- {warning}" for warning in previous_feedback) or "- 无"
    return rf"""请修复下面这份 MaxRead {'论文' if kind == 'paper' else '网页文章'} Markdown 的发布前质检错误。

硬约束：
- 输出完整文档，不要只输出修改片段。
- 只输出 JSON，格式为 {{"markdown":"修复后的完整 Markdown", "issues":[]}}；不要代码围栏、解释或额外字段。
- 不得新增原文或给定材料没有的事实、数字、结论。
- 原稿中已经出现的图片 marker 必须逐字保留并独立成行；不要把图片集中到文末，也不要凭空新增候选 marker。
- 保持 H1、TL;DR、章节、表格、列表、公式和图注内容；只修复下面列出的结构/格式问题。
- 公式必须保持为 <latex>...</latex>。不要改动没有报错的公式；如果质检明确指出公式内有不支持的宏、HTML/CJK 混入或非法格式命令，只做等价的最小修复。
- API、函数、类、字段、配置项和文件路径必须保持为反引号行内代码，不能改成 <latex>；若现有 <latex> 里只是 snake_case 程序标识符或函数调用，应恢复为行内代码。
- 修复公式时不得改变变量、上下标、运算关系或数值；不确定等价写法时保持原文，并在 issues 中说明风险。
- 如果问题已经修好，issues 必须为空数组。
- 修复前对照历史反馈；不得为了修当前错误而重新引入更早轮次已经出现过的错误。

本轮确定性质检错误：
{warning_text}

历史生成/修复反馈（只作为不可回退清单，本轮错误优先）：
{history_text}

允许保留的图片 marker：
{marker_text}

待修复 Markdown：
```markdown
{markdown}
```
"""


def parse_review_response(text: str) -> ReviewResult:
    raw = text
    stripped = _strip_fences(text).strip()
    payload = _extract_review_payload(stripped)
    if payload is None:
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


def _extract_review_payload(text: str):
    """Recover the largest valid review object from noisy model output.

    Some Responses-compatible gateways prepend an explanation or emit a
    second corrected JSON object after an incomplete first object.  The
    review contract still requires JSON, but parsing the longest valid object
    is safer than discarding an otherwise complete repair.
    """
    text = str(text or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    else:
        return payload

    # Models commonly obey the JSON shape but wrap it in a short explanation
    # and a fenced block. Parse fenced candidates before scanning every brace;
    # the prompt itself may contain an invalid schema example such as
    # ``{"passed": true|false}`` ahead of the actual response.
    fenced_candidates = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    for candidate in reversed(fenced_candidates):
        try:
            payload = json.loads(candidate.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    decoder = json.JSONDecoder()
    candidates = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("markdown"), str):
            candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item.get("markdown") or ""))


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


def _review_structure_error(original: str, reviewed: str, markers: Iterable[str]) -> str:
    original = str(original or "").strip()
    reviewed = str(reviewed or "").strip()
    first_line = next((line.strip() for line in reviewed.splitlines() if line.strip()), "")
    if original.lstrip().startswith("# ") and not first_line.startswith("# "):
        return "lost H1"
    if "TL;DR" in original and "TL;DR" not in reviewed:
        return "lost TL;DR"
    original_method = _numbered_section(original, 3)
    reviewed_method = _numbered_section(reviewed, 3)
    if len(original_method) >= 600 and len(reviewed_method) < len(original_method) * 0.8:
        return "truncated method section"
    if len(original) >= 1200 and len(reviewed) < len(original) * 0.65:
        return "was truncated"
    for number in range(1, 8):
        original_section = _numbered_section(original, number)
        reviewed_section = _numbered_section(reviewed, number)
        if not original_section or not reviewed_section:
            continue
        original_length = _review_narrative_length(original_section)
        reviewed_length = _review_narrative_length(reviewed_section)
        allowance = max(1200 if number == 3 else 600, int(original_length * (0.25 if number == 3 else 0.30)))
        if reviewed_length > original_length + allowance:
            return f"expanded section {number} beyond readability budget"
    for marker in markers:
        if marker in original and marker not in reviewed:
            return f"lost marker {marker}"
    return ""


def _review_narrative_length(markdown: str) -> int:
    lines = []
    in_table = False
    for line in str(markdown or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            in_table = True
            continue
        if in_table and not stripped:
            in_table = False
            continue
        if in_table or re.fullmatch(r"\[MaxReadFigure:[^\]]+\]", stripped):
            continue
        lines.append(stripped)
    return len("".join(lines))


def _numbered_section(markdown: str, number: int) -> str:
    match = re.search(
        rf"(?ms)^##\s+{number}(?:[.、]|\s).*?(?=^##\s+{number + 1}(?:[.、]|\s)|\Z)",
        str(markdown or ""),
    )
    return match.group(0).strip() if match else ""


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
