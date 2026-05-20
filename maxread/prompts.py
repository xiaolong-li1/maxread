from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

from .models import PaperBundle
from .render import figure_prompt_lines


FINAL_SYSTEM_PROMPT = """你是“读不动了 / MaxRead”的论文解读编辑。
目标读者：懂技术但没时间读全文的人。
输出中文 Markdown，后续会转换成飞书 Docx XML。风格参考高质量中文论文精读飞书文档：TL;DR 先行、结构强、段落短、公式和图表嵌在对应论述处，最后给整体评价。

规则：
1. 论文事实只来自输入材料。
2. 不确定就写“不确定”。
3. 不要写“作为 AI”。
4. 不要虚构指标、数据集、结论。
5. 方法部分保真优先：必须根据 TeX/source 中的 Method/Approach/Algorithm/Model/Training/Inference 小节写，不允许只凭摘要改写。
6. 公式必须解释直觉和作用。
7. 目前没有接入知乎资料，不要编造“中文社区/知乎解读”。
8. 如果输入材料不足以写出方法、实验、图表和评价，开头明确写“材料不足”，不要输出伪完整精读。
9. 不要只翻译摘要；必须结合 TeX/source 内容组织方法、公式、实验和图表。
"""


def build_final_user_prompt(bundle: PaperBundle, figure_inserts: List[Tuple[str, Path, str]] | None = None) -> str:
    metadata = bundle.metadata
    authors = ", ".join(metadata.authors[:20])
    warnings = "\n".join(f"- {item}" for item in bundle.parse_warnings) or "- 无"
    source_text = bundle.source_text or "[TeX source unavailable]"
    pdf_text = bundle.pdf_text or "[PDF text unavailable]"
    source_dir = str(bundle.source_dir) if bundle.source_dir else "[source dir unavailable]"
    source_tree = bundle.source_tree or "[source tree unavailable]"
    source_assets = "\n".join(f"- {item}" for item in bundle.source_assets) or "- 无"
    source_captions = "\n".join(f"- {item}" for item in bundle.source_captions) or "- 无"
    source_tables = "\n\n".join(f"[Table {i}]\n{item}" for i, item in enumerate(bundle.source_tables, start=1)) or "- 无"
    figure_markers = _figure_marker_text(figure_inserts or [])
    figure_pairs = _figure_pair_text(bundle)
    figure_refs = _figure_reference_text(bundle)
    return f"""请根据下面材料生成最终飞书文档 Markdown。

文档结构必须尽量贴近这个形态：
# [{metadata.paper_id}] {{中文标题}}：{{一句话定位}}
**{{英文标题}}**  
{{作者列表}} — {{机构/会议，如材料中可见}}

标题规则：
- H1 必须像中文技术精读标题，读者一眼知道“研究问题 + 方法抓手”，不要像机器翻译或论文摘要压缩。
- 中文标题部分控制在 8-18 个汉字；一句话定位控制在 12-28 个汉字。
- 优先使用读者熟悉、完整的术语：深层网络、深层神经网络、模型蒸馏、浅层网络、注意力迁移、量化训练、稀疏注意力、视频生成等；不要写“深网”“复刻函数”“函数逼近”“模型压缩训练”这类抽象空话，除非原文标题/方法名就是这样。
- H1 不要重复英文题名的逐词翻译；中文标题可以是问题式或结论式，但副标题必须具体说明方法或核心发现。
- 好例子：`# [1312.6184] 深层网络一定要很深吗？：用深层模型蒸馏浅层网络`
- 坏例子：`# [1312.6184] 深网真的需要很深吗：用模型压缩训练浅层网络复刻深层函数`

---

**TL;DR**：用 2-3 句话说明这篇论文解决什么问题、核心方法是什么、最值得记住的结论是什么。

| 维度 | 一句话 |
| --- | --- |
| 问题 | {{这篇论文要解决的瓶颈}} |
| 方法 | {{核心机制，不超过 25 字}} |
| 证据 | {{最重要实验/图表结论}} |
| 适用 | {{适用场景或边界}} |

## 1. 这篇论文要解决什么问题
解释背景、核心痛点、已有路线和本文切入点。只写读者理解后文必需的信息。

## 2. 核心观察 / 关键直觉
提炼论文中真正驱动方法设计的 2-4 个观察。能用图说明的，在本节就插图，不要放到单独图表章节。

## 3. 方法框架
方法部分必须是全文最准确的一节。按论文 Method/Approach/Algorithm/Model/Training/Inference 的原始小节顺序写，不要重排成泛泛的“核心思想”。整体框架图必须放在这里。

本节必须覆盖：
- 输入是什么、输出是什么、每个核心模块做什么。
- 关键公式逐条解释：变量含义、计算顺序、它解决哪个问题。
- 如果有算法/训练流程/推理流程，按原文步骤复述，不要省略条件、阈值、采样策略、损失项。
- 如果方法依赖图，图必须贴在正文引用它的位置附近：优先放在出现 `Fig./Figure/图` 引用、`\ref{{label}}` 或对应模块描述之后，而不是按 TeX figure 环境出现顺序放。
- 图解必须使用同一 figure pair 的 caption；不要用 A 图解释 B 图。
- 如果某个细节 source 里没有，写“原文未展开”，不要自行补全。

## 4. 实验结果
还原实验设置、baseline、指标、主表数据。能写 Markdown 表格就写表格。实验图必须放在对应结论附近。

## 5. 消融与补充分析
只保留最能解释方法有效性的消融、效率、scaling、失败案例或敏感性分析。

## 6. 局限性与开放问题
只写材料支持的局限；推断必须标明。

## 7. 整体评价
给出对这篇论文贡献、可信度、适用场景和阅读价值的判断。

写作要求：
- 篇幅目标：2200-3200 中文字。宁可结构清楚，也不要堆长段。
- 每段尽量不超过 160 个中文字符；连续纯文字段落不要超过 3 段，之后必须用列表、表格、公式或图承接。
- 顶层章节最多 7 个；不要生成“图表解读”“图表补充”“附：关键图表”这类集中放图章节。
- 不要输出“暂无”“不确定”占满章节；如果关键材料缺失，就在开头说明材料不足。
- 只有在 source excerpt、TeX tables、captions 都缺少方法/实验依据时，才允许写“材料不足”。如果 TeX tables 中有实验表，必须还原主表结论，不要误报材料不足。
- 对标题、方法名、模块名、变量名、数据集、指标、表格数字要忠实。
- H1 标题要自然、短、具体；如果标题读起来像“摘要压缩”或包含抽象废话，必须改写。
- 方法节不能只写“通过 X 提升 Y”这类概括句；每个核心机制至少要说明一次“怎么计算/怎么执行”。
- 方法节可以适当长于其他节，但不要引入 source/PDF 没有的实现细节。
- 语言像技术同事写的精读笔记，不要像产品营销稿。
- 只保留关键公式。所有公式使用 `<latex>...</latex>`，不要使用 `$$...$$`，不要把公式写成 Markdown 标题。
- 重要的单行公式独占一段：上一段解释公式来源，下一段解释符号和直觉。
- 图片必须像参考文档一样嵌入：先写一句“XXX 的整体设计如下图所示。”，下一行放 marker，再紧跟一段 `**图：...**` 图解。
- 图片位置以“原文引用位置”为准：如果 figure 有 label/ref context，必须放在该上下文对应内容附近；不要因为 TeX source 中 figure 环境靠前/靠后就跟着移动。
- `**图：...**` 必须用中文转述 caption 和图中信息，不要直接复制英文 caption；TeX 宏如 `\formername` 必须展开成真实方法名。
- 每个顶层章节最多放 2 张图；不要连续放图；不要把 3 张以上图放在同一个章节。
- 如果下方有可插入图片锚点，优先使用正文关键方法图/实验图；marker 必须逐字保留，不要删、不改、不翻译。宁可少放图，也不要把图放错位置或集中堆到文末。

arXiv metadata：
- ID: {metadata.paper_id}
- Title: {metadata.title}
- Authors: {authors}
- Published: {metadata.published}
- Updated: {metadata.updated}
- Categories: {', '.join(metadata.categories)}
- Abstract: {metadata.summary}

解析警告：
{warnings}

解压后的 TeX source 目录：
{source_dir}

Source 文件结构：
```text
{source_tree}
```

图片/媒体资源文件：
{source_assets}

TeX captions：
{source_captions}

TeX figure pairs（权威图文对应，优先级高于文件名猜测；解释图片时必须使用同一项里的 caption）：
{figure_pairs}

Figure reference context（决定图片应该贴近哪里；优先级高于 TeX figure 环境顺序）：
{figure_refs}

TeX tables（优先用于实验结果；如果这里有主表，不要说实验材料缺失）：
```tex
{source_tables}
```

可插入图片锚点（每个 marker 必须逐字保留，独立成行，插在相关图解段落之前；marker 后紧跟 `**图：...**` 图解；不要修改 marker 内任何字符）：
{figure_markers}

TeX/source excerpt：
```tex
{source_text}
```

PDF text excerpt：
```text
{pdf_text}
```
"""


def _figure_marker_text(figure_inserts: List[Tuple[str, Path, str]]) -> str:
    if not figure_inserts:
        return "- 无"
    return "\n".join(figure_prompt_lines(figure_inserts))


def _figure_pair_text(bundle: PaperBundle) -> str:
    if not bundle.source_figures:
        return "- 无"
    lines = []
    for figure in bundle.source_figures[:30]:
        label = f" label={figure.label}" if figure.label else ""
        lines.append(f"- asset={figure.asset}{label} file={figure.tex_file} caption={figure.caption}")
    return "\n".join(lines)



def _figure_reference_text(bundle: PaperBundle) -> str:
    if not bundle.source_figures or not bundle.source_text:
        return "- 无"
    labels = [figure.label for figure in bundle.source_figures if figure.label]
    seen = set()
    lines = []
    for label in labels[:40]:
        if label in seen:
            continue
        seen.add(label)
        pattern = re.compile(rf"(?:Fig(?:ure)?\.?|图)?\s*~?\\(?:ref|autoref|cref)\{{{re.escape(label)}\}}", re.I)
        contexts = []
        for match in pattern.finditer(bundle.source_text):
            start = max(0, match.start() - 220)
            end = min(len(bundle.source_text), match.end() + 260)
            context = re.sub(r"\s+", " ", bundle.source_text[start:end]).strip()
            contexts.append(context)
            if len(contexts) >= 2:
                break
        if contexts:
            lines.append(f"- label={label}: " + " || ".join(contexts))
    return "\n".join(lines) if lines else "- 无"

def fallback_markdown(bundle: PaperBundle, reason: str) -> str:
    metadata = bundle.metadata
    authors = ", ".join(metadata.authors)
    warnings = "\n".join(f"- {item}" for item in bundle.parse_warnings) or "- 无"
    return f"""# {metadata.title}
原文：{metadata.abs_url}
作者：{authors}
一句话总结：{metadata.summary}

## 先说结论
- OpenAI 总结生成失败，本文档先回填 arXiv 摘要和解析状态。
- 失败原因：{reason}

## 背景：为什么需要这篇论文
{metadata.summary}

## 方法：作者到底怎么做
不确定。需要重新运行总结生成。

## 关键公式/机制
不确定。需要重新运行总结生成。

## 实验结果怎么看
不确定。需要重新运行总结生成。

## 图表速读
不确定。需要重新运行总结生成。

## 局限和风险
- 当前文档不是完整 AI 解读，只是失败 fallback。

## 我该怎么读这篇论文
- 10 分钟：先读摘要、引言和结论。
- 30 分钟：补充阅读方法和实验主表。
- 深读：结合 TeX source 和 PDF 重新生成完整总结。

## 术语表
暂无。

## 解析说明
{warnings}
"""
