from __future__ import annotations

from pathlib import Path
from typing import List, Sequence, Tuple, Union

from .models import ArticleBundle
from .render import figure_prompt_lines


ImageInsert = Union[Tuple[str, Path, str], Tuple[str, Path, str, int]]


ARTICLE_SYSTEM_PROMPT = """你是“读不动了 / MaxRead”的技术文章解读编辑。
目标读者：懂技术但没时间读完整博客/网页的人。
输出中文 Markdown，后续会转换成飞书 Docx XML。文档要结构强、段落短、图文穿插、表格摘要清晰。

硬规则：
1. 事实只来自输入网页材料。
2. 对网页/博客必须做“逐 section 保真摘要”：按原文标题顺序逐节压缩，不允许重排章节、合并远距离章节或自由组织成另一篇文章。
3. `[MaxReadFigure:...]` 是原文图片锚点，必须留在它所在 section 附近，不要移动到别的 section，不要用 A 图解释 B 图。
4. 不要编造作者没写的实验、结论、公式或背景。
5. 不要写“作为 AI”。
6. 如果网页正文抓取不足，开头说明“网页材料不足”，但材料充分时不要误报。
7. 公式使用 `<latex>...</latex>`，不要用 `$$...$$`。
"""


def build_article_user_prompt(bundle: ArticleBundle, image_inserts: Sequence[ImageInsert]) -> str:
    warnings = "\n".join(f"- {item}" for item in bundle.warnings) or "- 无"
    sections = "\n".join(f"- {item}" for item in bundle.sections[:60]) or "- 无"
    images = _image_text(bundle, image_inserts)
    tables = "\n\n".join(f"[Table {i}] {item}" for i, item in enumerate(bundle.tables[:8], start=1)) or "- 无"
    code = "\n\n".join(f"[Code {i}]\n{item}" for i, item in enumerate(bundle.code_blocks[:6], start=1)) or "- 无"
    math = "\n".join(f"- {item}" for item in bundle.math_blocks[:40]) or "- 无"
    section_material = _section_material(bundle, image_inserts)
    return f"""请根据下面网页材料生成飞书文档 Markdown。

文档结构必须是：
# {{中文标题}}：{{一句话定位}}
原文：{bundle.url}
作者/站点：{{作者或站点}}

---

**TL;DR**：2-3 句话说明这篇文章的核心问题、主要观点、读者应该记住什么。

| 维度 | 一句话 |
| --- | --- |
| 主题 | {{文章在讨论什么}} |
| 核心观点 | {{最重要主张}} |
| 关键机制 | {{核心解释/推导/流程}} |
| 适合谁读 | {{目标读者}} |

## 逐 section 保真摘要
下面必须按“原文 section 材料”的顺序逐节写。每个原文 section 用一个同名二级或三级标题，标题可翻译成中文但括号保留英文原题。

## 总结评价
只基于原文，总结文章价值、可信度、适合深读的人和局限。

写作要求：
- 篇幅目标：1200-2200 中文字。不要写成长篇论文综述。
- 每个 section 压缩为 1-3 个短段，必要时加 2-4 条 bullet。
- 不允许把原文后半段内容提前讲，也不允许把不同 section 合成“核心观点/机制/启发”这种新结构。
- 图片必须出现在原 section 中，marker 独立成行，并紧跟 1-2 句图解。不要生成独立“图片附录”。
- 正文中的 `[MaxReadFigure:...]` 必须逐字保留；不要改 marker 文本。
- marker 后面的 `**图：...**` 来自原文 figcaption/alt，必须基于这张图解释；如果 caption/alt 很短，只说明它在原文对应小节的作用。图解必须用中文转述，不要直接复制长英文 caption。
- 表格优先整理为 2-5 列中文摘要表；没有明确数值时不要编造。
- 代码块只保留关键片段，超过 40 行要摘要。

网页信息：
- URL: {bundle.url}
- Title: {bundle.title}
- Author: {bundle.author}
- Published: {bundle.published}
- Site: {bundle.site_name}

解析警告：
{warnings}

网页目录/标题：
{sections}

图片锚点（marker 必须逐字保留，独立成行）：
{images}

表格文本：
{tables}

公式片段：
{math}

代码片段：
```text
{code}
```

原文 section 材料（这是最高优先级；按顺序保真压缩）：
```text
{section_material}
```
"""


def _section_material(bundle: ArticleBundle, image_inserts: Sequence[ImageInsert]) -> str:
    if not bundle.section_blocks:
        return _clip(bundle.text, 60_000)
    text_by_source = {str(item[3]): f"{item[0]}\n**图：{item[2]}**" for item in image_inserts if len(item) >= 4}
    parts: List[str] = []
    for index, section in enumerate(bundle.section_blocks[:80], start=1):
        title = section.title or "正文"
        parts.append(f"[Section {index} | h{section.level}] {title}")
        for block in section.blocks:
            marker = _article_marker_source(block)
            if marker and marker in text_by_source:
                parts.append(text_by_source[marker])
            else:
                parts.append(block)
        parts.append("")
    return _clip("\n".join(parts).strip(), 60_000)


def _article_marker_source(block: str) -> str:
    if not block.startswith("[ArticleImage:"):
        return ""
    end = block.find("]")
    if end < 0:
        return ""
    return block[len("[ArticleImage:"):end]


def _image_text(bundle: ArticleBundle, image_inserts: Sequence[ImageInsert]) -> str:
    if not image_inserts:
        return "- 无"
    triples = [(item[0], item[1], item[2]) for item in image_inserts]
    return "\n".join(figure_prompt_lines(triples))


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[TRUNCATED]"
