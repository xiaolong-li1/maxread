from __future__ import annotations

from pathlib import Path
from typing import List, Sequence, Tuple, Union

from .models import ArticleBundle
from .render import figure_prompt_lines


ImageInsert = Union[Tuple[str, Path, str], Tuple[str, Path, str, int]]


ARTICLE_SYSTEM_PROMPT = """你是“读不动了 / MaxRead”的技术文章解读编辑。
目标读者：懂技术但没时间读完整博客/网页的人。
输出中文 Markdown，后续会转换成飞书 Docx XML。对 blog / 网页文章，默认目标是“汉化后的原文导读”，不是论文综述，也不是重新组织一篇新文章。

硬规则：
1. 事实只来自输入网页材料。
2. 对网页/博客必须做“逐 section 汉化重述”：按原文标题顺序推进，不允许重排章节、合并远距离章节或自由组织成另一篇文章。
3. `[MaxReadFigure:...]` 是原文图片锚点，必须留在它所在 section 附近，不要移动到别的 section，不要用 A 图解释 B 图。
4. 不要编造作者没写的实验、结论、公式或背景。
5. 不要写“作为 AI”。
6. 如果网页正文抓取不足，开头说明“网页材料不足”，但材料充分时不要误报。
7. 公式使用 `<latex>...</latex>`，不要用 `$$...$$`。
8. 除开头总结外，不要增加“核心机制/实验表格/总结评价”等原文没有的重排版块；用中文把原文意思讲清楚即可。
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
# {{中文标题}}
原文：{bundle.url}
站点：{{作者或站点；没有就写网页域名}}

---

## 总结
用 4-6 条中文 bullet 先说明：这篇文章想解决什么问题、核心概念是什么、最重要发现是什么、为什么值得读、读者需要注意什么边界。

## 正文汉化
下面必须按“原文 section 材料”的顺序逐节写。每个原文 section 用同级标题，标题翻译成中文并在括号中保留英文原题。正文用中文重述原文含义，保持原文推进顺序。

## 阅读提示
只写 2-3 条：哪些读者适合深读原文、哪些部分可以跳读、哪些概念需要背景知识。

写作要求：
- 篇幅目标：2500-6000 中文字；长文可以更长，但不要逐句硬翻译。
- 不要把 blog 改造成论文阅读报告；开头总结之后，主要任务是按原文顺序汉化重述。
- 每个 section 通常写 1-4 个短段；原文是列举时才用 bullet，原文不是列表就不要强行列表化。
- 不允许把原文后半段内容提前讲，也不允许把不同 section 合成“核心观点/机制/启发”这种新结构。
- 图片必须出现在原 section 中，marker 独立成行，并紧跟 1-2 句图解。不要生成独立“图片附录”。
- 正文中的 `[MaxReadFigure:...]` 必须逐字保留；不要改 marker 文本。
- marker 后面的 `**图：...**` 来自原文 figcaption/alt，必须基于这张图解释；如果 caption/alt 很短，只说明它在原文对应小节的作用。图解必须用中文转述，不要直接复制长英文 caption。
- 如果图片说明是“原网页标题区和可视目录”，把它放在正文汉化开头，作为原网页版式导览；不要拆成多张目录小图。
- 如果图片来自原网页渲染截图，按它在 section 材料里的位置解释：它代表原网页可见的 figure/table/canvas/svg 区块，不要当成附录图，也不要移动到其他 section。
- 原文表格可以翻译成中文表格；不要新增总结表格。
- 代码块只保留关键片段，超过 40 行要摘要。
- 不要输出 `??`、`[TRUNCATED]` 或 `rep`/`measu` 这类明显截断的英文半词；遇到引用编号缺失时直接删掉引用占位，保留中文含义。

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
