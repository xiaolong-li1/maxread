from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from .models import PaperBundle
from .render import figure_prompt_lines
from .repository import find_repository_url


def select_key_source_tables(source_tables: List[str], max_tables: int = 6) -> List[str]:
    """Select a compact evidence set: main results, efficiency, and ablation."""
    tables = []
    seen_content = set()
    for item in source_tables:
        table = str(item or "").strip()
        if not table:
            continue
        canonical = _canonical_source_table(table)
        if canonical in seen_content:
            continue
        seen_content.add(canonical)
        tables.append(table)
    limit = max(0, int(max_tables))
    if limit <= 0:
        return []
    if len(tables) <= limit:
        return tables

    def category_and_score(index: int, table: str) -> tuple[str, int]:
        text = re.sub(r"\s+", " ", table).lower()
        appendix = any(word in text for word in (
            "supplementary", "appendix", "complete results", "full results", "additional results",
        ))
        if any(word in text for word in ("ablation", "effect of", "component", "w/o", "without ", "sensitivity")):
            category, score = "ablation", 90
        elif any(word in text for word in ("efficiency", "throughput", "latency", "memory", "flops", "speed")):
            category, score = "efficiency", 95
        elif any(word in text for word in (
            "main result", "comparison", "performance", "benchmark", "imagenet", "ucf", "geneval", "accuracy", "fid", "fvd",
        )):
            category, score = "result", 100
        else:
            category, score = "other", 35
        if appendix:
            score -= 45
        score += max(0, 18 - index)
        return category, score

    ranked = []
    for index, table in enumerate(tables):
        category, score = category_and_score(index, table)
        ranked.append((score, index, category, table))

    selected_indices = set()
    for category in ("result", "efficiency", "ablation"):
        candidates = [item for item in ranked if item[2] == category]
        if candidates:
            selected_indices.add(max(candidates)[1])
    for _score, index, _category, _table in sorted(ranked, reverse=True):
        if len(selected_indices) >= limit:
            break
        selected_indices.add(index)
    return [table for index, table in enumerate(tables) if index in selected_indices]


def _canonical_source_table(table: str) -> str:
    match = re.search(r"\\begin\{tabular\*?\}.*?\\end\{tabular\*?\}", str(table or ""), flags=re.S)
    content = match.group(0) if match else str(table or "")
    content = re.sub(r"\\(?:label|caption)\s*\{.*?\}", "", content, flags=re.S)
    content = re.sub(r"\\(?:textbf|mathbf|underline|emph)\s*\{([^{}]*)\}", r"\1", content)
    return re.sub(r"\s+", "", content).lower()


FINAL_SYSTEM_PROMPT = """你是“读不动了 / MaxRead”的论文解读编辑。
目标读者：懂技术但没时间读全文的人。
输出中文 Markdown，后续会转换成飞书 Docx XML。风格参考高质量中文论文精读飞书文档：TL;DR 先行、结构强、段落短、公式和图表嵌在对应论述处，最后给整体评价。

规则：
1. 论文事实只来自输入材料。
2. 不确定就写“不确定”。
3. 不要写“作为 AI”。
4. 不要虚构指标、数据集、结论。
5. 方法部分保真优先：必须根据 TeX/source 中的 Method/Approach/Algorithm/Model/Training/Inference 小节写，不允许只凭摘要改写。
6. 公式必须解释来源、变量、计算顺序、直觉和作用，不能只罗列推导结果。
7. 目前没有接入知乎资料，不要编造“中文社区/知乎解读”。
8. 如果输入材料不足以写出方法、实验、图表和评价，开头明确写“材料不足”，不要输出伪完整精读。
9. 不要只翻译摘要；必须结合 TeX/source 内容组织方法、公式、实验和图表。
10. 输出的第一个非空行必须是 Markdown H1（以 `# ` 开头）；禁止在 H1 前输出说明、JSON、YAML 或代码围栏。
11. 直接输出最终 Markdown，不要使用包住全文的 ```markdown``` 或其他代码围栏。
12. API、函数、类、字段、配置项、文件路径等程序标识符必须使用 Markdown 行内代码，例如 `tensor_meta()`；只有数学表达式才能使用 `<latex>...</latex>`。
13. 方法节不是摘要的扩写，而是让没读过论文的技术读者能够复述数据/状态如何流过系统。写作前先在内部建立“任务设定 -> 模块输入/操作/输出 -> 训练或推理流程 -> 端到端结果”的方法账本，再组织成 Markdown；不要输出这个账本本身。
14. 每个核心模块至少交代：为什么需要它、接收什么、做了什么、产生什么、下一步如何使用；不能只写“通过 X 提升 Y”。
15. 方法节中的公式必须嵌在推导或执行步骤中：公式前说明它解决的局部问题，公式后说明符号、计算顺序和直觉；没有足够材料时写“原文未展开”，不要用泛化解释补空白。
16. 论文有多个方法子模块时，必须使用原文真实的小节顺序或等价的 `###` 小节承载它们；不要把所有模块、公式和训练细节压进一两个大段落。
17. 只在读者理解下一步计算所必需时定义符号，并紧贴公式解释；不要单独生成符号账本、作用域矩阵或形式化审计段落。同一符号确有歧义时，用一句话澄清即可。
18. 边界情况和 sanity check 只用于论文核心结论、容易误解的分组/reset 条件或图文冲突；不要为每个公式机械穷举同组/跨组、同块/跨块。
19. 核心观察必须按“实验或图示设置 -> 实际观测 -> 作者的机制解释/假设 -> 导出的设计 -> 证据与边界”展开。观测、假设、经验结果和数学必然结论必须使用不同措辞。
20. 图与公式看似矛盾时，应核对输入组织和实现前提；能用一两句解释清楚就不要展开成长推导，无法消解时简洁标成论文边界。
21. 方法中存在彼此正交的设计轴时必须拆开，例如 position design 与 frequency allocation、训练目标与推理调度；不要把多个设计统称为一个方法动作。
22. 方法写到“读者能顺着输入、核心动作和输出复述流程”为止；不要为了显得严谨新增论文没有要求的符号体系、反例或证明。
23. 机制解释仍要区分定义、观测、作者解释和实验支持，但用自然语言完成；可视化或 probe 只能增强可信度，不能单独证明因果。
"""


def build_final_user_prompt(
    bundle: PaperBundle,
    figure_inserts: List[Tuple[str, Path, str]] | None = None,
    figure_visuals: Dict[str, str] | None = None,
    figure_owners: Dict[str, str] | None = None,
    editorial_guidance: str = "",
) -> str:
    evidence, instructions = _build_final_prompt_parts(
        bundle,
        figure_inserts=figure_inserts,
        figure_visuals=figure_visuals,
        figure_owners=figure_owners,
        editorial_guidance=editorial_guidance,
    )
    return evidence + "\n\n" + instructions


def build_paper_evidence_prefix(
    bundle: PaperBundle,
    figure_inserts: List[Tuple[str, Path, str]] | None = None,
    figure_visuals: Dict[str, str] | None = None,
    figure_owners: Dict[str, str] | None = None,
    editorial_guidance: str = "",
) -> str:
    evidence, _instructions = _build_final_prompt_parts(
        bundle,
        figure_inserts=figure_inserts,
        figure_visuals=figure_visuals,
        figure_owners=figure_owners,
        editorial_guidance=editorial_guidance,
    )
    return evidence


def _build_final_prompt_parts(
    bundle: PaperBundle,
    figure_inserts: List[Tuple[str, Path, str]] | None = None,
    figure_visuals: Dict[str, str] | None = None,
    figure_owners: Dict[str, str] | None = None,
    editorial_guidance: str = "",
) -> tuple[str, str]:
    metadata = bundle.metadata
    repository_url = _repository_url_text(bundle)
    warnings = "\n".join(f"- {item}" for item in bundle.parse_warnings) or "- 无"
    source_text = bundle.source_text or "[TeX source unavailable]"
    pdf_section = ""
    if not str(bundle.source_text or "").strip():
        pdf_text = bundle.pdf_text or "[PDF text unavailable]"
        pdf_section = f"""

PDF text excerpt（仅在 TeX source 不可用时启用）：
```text
{pdf_text}
```"""
    source_dir = str(bundle.source_dir) if bundle.source_dir else "[source dir unavailable]"
    source_tree = bundle.source_tree or "[source tree unavailable]"
    source_assets = "\n".join(f"- {item}" for item in bundle.source_assets) or "- 无"
    source_captions = "\n".join(f"- {item}" for item in bundle.source_captions) or "- 无"
    selected_tables = select_key_source_tables(bundle.source_tables)
    source_tables = "\n\n".join(f"[Table {i}]\n{item}" for i, item in enumerate(selected_tables, start=1)) or "- 无"
    source_table_summary = f"原文解析到 {len(bundle.source_tables)} 张表，本次选择 {len(selected_tables)} 张关键表。"
    figure_markers = _figure_marker_text(
        figure_inserts or [], figure_visuals or {}, figure_owners or {}
    )
    figure_pairs = _figure_pair_text(bundle)
    figure_refs = _figure_reference_text(bundle)
    guidance = str(editorial_guidance or "").strip() or "- 无"
    heading_prefix = f"[{metadata.paper_id}] " if metadata.source_kind == "arxiv" else ""
    source_label = metadata.source_label or (f"arXiv {metadata.paper_id}" if metadata.source_kind == "arxiv" else "技术文档")
    raw_prompt = f"""请根据下面材料生成最终飞书文档 Markdown。

文档结构必须尽量贴近这个形态：
# {heading_prefix}{{中文标题}}：{{一句话定位}}
**{{英文标题}}**  
原文：{metadata.abs_url}

标题规则：
- H1 必须像中文技术精读标题，读者一眼知道“研究问题 + 方法抓手”，不要像机器翻译或论文摘要压缩。
- 中文标题部分控制在 8-18 个汉字；一句话定位控制在 12-28 个汉字。
- 优先使用读者熟悉、完整的术语：深层网络、深层神经网络、模型蒸馏、浅层网络、注意力迁移、量化训练、稀疏注意力、视频生成等；不要写“深网”“复刻函数”“函数逼近”“模型压缩训练”这类抽象空话，除非原文标题/方法名就是这样。
- H1 不要重复英文题名的逐词翻译；中文标题可以是问题式或结论式，但副标题必须具体说明方法或核心发现。
- 好例子：`# [1312.6184] 深层网络一定要很深吗？：用深层模型蒸馏浅层网络`
- 坏例子：`# [1312.6184] 深网真的需要很深吗：用模型压缩训练浅层网络复刻深层函数`
- 当前来源类型：{source_label}。非 arXiv 技术文档的 H1 不要伪造 arXiv 编号。

---

**TL;DR**：用 2-3 句话说明这篇论文解决什么问题、核心方法是什么、最值得记住的结论是什么。

| 维度 | 一句话 |
| --- | --- |
| 问题 | {{这篇论文要解决的瓶颈}} |
| 方法 | {{核心机制，不超过 25 字}} |
| 证据 | {{最重要实验/图表结论}} |
| 适用 | {{适用场景或边界}} |
| 仓库 | {{如果且仅如果输入材料中存在经明确代码语境验证的 GitHub/GitLab/Bitbucket/Codeberg/HuggingFace/SourceForge 仓库 URL，填该 URL；项目主页、论文主页、demo 页面不要填在这里}} |

## 1. 这篇论文要解决什么问题
解释背景、核心痛点、已有路线和本文切入点。只写读者理解后文必需的信息。

## 2. 核心观察 / 关键直觉
提炼论文中真正驱动方法设计的 2-4 个观察。每个观察都要交代：作者在什么输入/对照/图中看到了什么；这只是观测、作者解释还是已被消融支持的结论；它具体导出了哪个设计动作。能用图说明的，在本节就插图，不要放到单独图表章节。

## 3. 方法框架
方法部分必须是全文最准确的一节。按论文 Method/Approach/Algorithm/Model/Training/Inference 的原始小节顺序写，不要重排成泛泛的“核心思想”。整体框架图必须放在这里。

本节必须覆盖：
- 先用一个短段交代任务设定与上下文：输入、输出、要解决的具体瓶颈，以及为什么自然导出这套设计。不要让读者在没有上下文时直接撞上模块名和公式。
- 在 `## 3` 下保留论文真实的方法子节；若原文没有清晰小节，就按“任务设定 / 整体流程 / 核心机制 / 训练或推理”拆成 2-4 个有信息量的 `###`，不要为了凑标题拆成一句话小节。
- 再按原文小节或执行顺序展开核心模块。每个模块都说明“为什么需要它 -> 接收什么 -> 如何计算/执行 -> 产出什么并交给下一步 -> 它具体缓解哪个瓶颈”，形成连续因果链，而不是模块清单。模块之间要有承接句，说明上一步的输出如何成为下一步输入。
- 只保留理解核心机制所需的公式。公式前后说明它在流程中做什么、关键变量是什么以及输出交给谁；无需为次要符号写完整定义表。
- 只有当分组、reset、mask 或相对位置是论文核心创新且容易误读时，才补一组代表性边界说明；一两句话能讲清时不要扩写成形式证明。
- 如果有算法/训练流程/推理流程，按原文步骤复述，不要省略条件、阈值、采样策略、损失项。
- 论文确实区分训练与推理时，用简短流程说明差异；没有复杂阶段差异时不要为了凑结构新增端到端例子。
- 如果方法依赖图，图必须贴在正文引用它的位置附近：优先放在出现 `Fig./Figure/图` 引用、`\ref{{label}}` 或对应模块描述之后，而不是按 TeX figure 环境出现顺序放。
- 图解必须结合同一 figure pair 的 caption 和 visual 描述；不要只按文件名猜图，也不要用 A 图解释 B 图。
- 如果某个细节 source 里没有，写“原文未展开”，不要自行补全。

## 4. 实验结果
简要交代实验设置、baseline 和指标，完整数据优先放进 Markdown 表格。正文只提炼最重要的 3-5 个结论、明显退化和速度/质量取舍，不逐行复述表格。实验图放在对应结论附近。

## 5. 消融与补充分析
用紧凑表格保留所有有语义的命名配置、开关、比例、步长和数值，不能只写最佳配置和一行总括；每组只用一小段说明控制变量、主要趋势、反例与能支持的结论。区分组件消融、机制 probe、敏感性和失败案例，但不要逐行扩写成散文。附录/Appendix/Supplement 中的关键扩展实验只在它直接解释核心机制或边界时纳入，不要机械堆附录细节。

## 6. 局限性与开放问题
只写材料支持的局限；推断必须标明。附录里提到的限制、适用边界、额外失败案例或未解决问题，可以作为本节依据。

## 7. 整体评价
给出对这篇论文贡献、可信度、适用场景和阅读价值的判断。

写作要求：
- 方法节以“读者能顺着流程说明白”为停止条件，可以比其他章节长，但不要追求形式完备；第 1、2、4、5、6、7 节保持紧凑，图表已表达的数据不要在正文逐行复述。
- 段落按一个完整论点自然分段；不要为了追求短段把定义、前提和结论拆散。连续论述较长时用小表、公式、例子或图帮助定位，但不要机械地每三段插入组件。
- 顶层章节最多 7 个；不要生成“图表解读”“图表补充”“附：关键图表”这类集中放图章节。
- 不要输出“暂无”“不确定”占满章节；如果关键材料缺失，就在开头说明材料不足。
- 只有在 source excerpt、TeX tables、captions 都缺少方法/实验依据时，才允许写“材料不足”。如果 TeX tables 中有实验表，必须还原主表结论，不要误报材料不足。
- 对标题、方法名、模块名、变量名、数据集、指标、表格数字要忠实。
- H1 标题要自然、短、具体；如果标题读起来像“摘要压缩”或包含抽象废话，必须改写。
- 开头不要放作者列表或作者信息；只保留标题、英文标题、原文链接，以及可选仓库行。
- 仓库行只能使用下方的 Repository URL；如果该值为“无”，不要从正文里自行挑项目主页、demo 页、论文主页或其他 GitHub 链接充当仓库。
- 方法节不能只写“通过 X 提升 Y”这类概括句；每个核心机制至少要说明一次“怎么计算/怎么执行”。
- 方法节要让没读过原文的技术读者能沿着数据/状态流复述整个流程；术语第一次出现时先给语境，再给缩写或公式。
- 方法节应让读者理解任务设定、核心数据流和关键模块如何衔接；训练/推理边界只在论文确实提供时说明。
- 方法节可以适当长于其他节，但不要引入 source/PDF 没有的实现细节。
- 对论文 motivation 中的可视化观测，使用“作者观察到/作者据此假设/实验支持”这类有证据层级的表述；只有能由定义直接推出时才写“必然/因此公式表明”。
- 行文中必须明确区分观测、作者解释、经验支持、数学结论，不能让四者在同一句因果陈述中互相替代。
- 表格只选最能支撑主结论的主结果、效率和关键消融；不要搬运所有 source/附录表。入选表内保留必要对照行和关键数值，正文只总结差异。
- 语言像技术同事写的精读笔记，不要像产品营销稿。
- 只保留关键公式。所有公式使用 `<latex>...</latex>`，不要使用 `$$...$$`，不要把公式写成 Markdown 标题。
- API、函数、类、字段、配置项和文件路径使用 Markdown 行内代码；不要把 `tensor_meta()`、`on_worker`、`publish(req_id)` 这类程序标识符包进 `<latex>`。
- 重要的单行公式独占一段：上一段解释公式来源，下一段解释符号和直觉。
- 图片必须像参考文档一样嵌入：先写一句“XXX 的整体设计如下图所示。”，下一行放 marker，再紧跟一个普通段落 `图题：...`。不要给题注加粗、加引用块或自行编号；发布编译器会按成稿中的出现顺序生成“图 1、图 2”。
- caption 以“并列图组”开头时，panel 短说明已经绘制在图内；正文和 `图题：...` 只总结两图共同结论，不逐 panel 重复描述。
- 图片位置以“原文引用位置”为准：如果 figure 有 label/ref context，必须放在该上下文对应内容附近；不要因为 TeX source 中 figure 环境靠前/靠后就跟着移动。
- `图题：...` 必须用中文转述 caption 和图中信息，不要直接复制英文 caption；TeX 宏如 `\formername` 必须展开成真实方法名。题注只承担识别与解释图片的职责，额外技术推导留在正文。
- 如果某个 marker 的 caption/visual 与当前段落不一致，不要使用这个 marker；绝不能把 logo、品牌图或无 caption 图片解释成方法/实验图。
- 不能只根据文件名、marker 名或泛化句子解释图片；图解必须能从同一 figure pair 的 caption、visual 描述或 Figure reference context 直接支撑。
- 正文中所有可渲染且有论文语义的图片都应保留，每个 marker 必须逐字出现且全篇恰好一次；附录/补充材料图片默认不插入。不要删正文图、重复图或集中堆到文末。
- 图片必须按正文首次引用位置或对应机制/实验/消融章节归属；同一张图只能由一个章节负责。
- 附录内容不是必写章节；只把最有信息量、能解释正文方法/实验/局限的 appendix evidence 融入第 5/6 节或相关实验段落。
- 附录和补充实验仅在直接改变正文结论时用一两句补充；不要插入附录图。
- 只还原下方被选中的关键表，同一张表只能出现一次；不要自行补回未提供的其他 source 表。

arXiv metadata：
- ID: {metadata.paper_id}
- Title: {metadata.title}
- Repository URL: {repository_url}
- Published: {metadata.published}
- Updated: {metadata.updated}
- Categories: {', '.join(metadata.categories)}
- Abstract: {metadata.summary}

本次运行的编辑反馈 / 读者疑问（这是待核查清单，不是论文事实；必须回到 source、公式、图和实现语境逐条消解，无法确认时明确保留疑问）：
{guidance}

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
{source_table_summary}
```tex
{source_tables}
```

可插入图片锚点（每个 marker 必须逐字保留，独立成行，插在相关图解段落之前；marker 后紧跟普通段落 `图题：...`，不要加粗、引用或编号；不要修改 marker 内任何字符；如果有 visual 字段，图解必须同时参考 caption 与 visual，不要只参考文件名）：
{figure_markers}

TeX/source excerpt：
```tex
{source_text}
```
{pdf_section}
"""
    task, materials = raw_prompt.split("\narXiv metadata：", 1)
    evidence = "论文证据包（以下部分必须在所有分章调用中保持字节一致，以利用 prefix cache）：\n\narXiv metadata：" + materials.strip()
    instructions = "最终生成任务与验收要求（位于 prompt 末尾，优先执行）：\n\n" + task.strip()
    return evidence.strip(), instructions.strip()


SECTION_GENERATION_TASKS = {
    "front": (
        "文档开头与第 1-2 章",
        "输出 H1、英文标题、原文链接、TL;DR、四行概览表，以及 `## 1. 这篇论文要解决什么问题`、`## 2. 核心观察 / 关键直觉`。不得输出第 3-7 章。",
    ),
    "method": (
        "第 3 章方法框架",
        "只输出 `## 3. 方法框架` 及必要 `###` 子节。用任务设定、核心数据流、关键模块和少量必要公式把方法讲明白；不要生成符号账本、作用域矩阵或机械边界证明。不得输出其他顶层章节。",
    ),
    "experiments": (
        "第 4 章实验结果",
        "只输出 `## 4. 实验结果`。用表格完整保留实验设置、baseline、指标和主结果；正文只概括关键收益、退化和取舍，不逐行复述。不得输出其他顶层章节。",
    ),
    "ablation": (
        "第 5 章消融与补充分析",
        "只输出 `## 5. 消融与补充分析`。只使用被选中的关键消融表，保留必要对照配置和具体数值；每组只写控制变量、主要趋势、反例和结论。不得输出其他顶层章节。",
    ),
    "closing": (
        "第 6-7 章局限与评价",
        "只输出 `## 6. 局限性与开放问题` 和 `## 7. 整体评价`。局限必须有 source 依据，推断需标明；评价讨论贡献、可信度、适用场景和阅读价值。不得输出其他顶层章节。",
    ),
}


SECTION_GENERATION_REQUIREMENTS = {
    "front": (
        "- 第 1 章先补齐读懂论文所需的背景、已有路线及其具体缺口，不能从摘要直接跳到方法名。\n"
        "- 第 2 章的每个观察都按“观测对象/对照 -> 看到什么 -> 证据层级 -> 导出哪个设计”展开；区分直接观测、作者解释、实验支持和数学结论。\n"
        "- 只保留固定的开篇概览表；不要把后续 source 实验表提前复制到本章。"
    ),
    "method": (
        "- 方法是全文信息最完整的一章，但目标是讲清楚而不是形式化审计。沿原文小节或真实执行顺序，先讲任务输入输出与必要上下文，再讲模块。\n"
        "- 符号第一次出现时用一句话说明即可；只有真正影响结论的分组、reset 或作用域前提才展开，禁止单独制作符号账本或作用域矩阵。\n"
        "- 每个模块按“为何需要 -> 输入 -> 计算/操作 -> 输出 -> 交给下一步 -> 缓解何种瓶颈”写成连续数据流。\n"
        "- 只保留核心公式并解释其作用；次要参数、显然的代数步骤和论文未强调的边界无需逐项展开。\n"
        "- 方法图只在它对应的模块附近出现一次；不要在本章复写实验或消融表。"
    ),
    "experiments": (
        "- 用一个紧凑段落交代数据集/任务、baseline、指标和必要评测设置，避免展开通用常识。\n"
        "- 主结果的完整数字由指定表格承载；正文只提炼 3-5 个最重要的收益、退化、速度/质量取舍或失败模式。\n"
        "- 本章指定的每张 source 表只还原一次；若前文已解释同一结论，用文字回指，不再创建内容相同的摘要表。"
    ),
    "ablation": (
        "- 每组用一小段交代“固定什么 -> 只改变什么 -> 主要趋势/反例 -> 支持什么 -> 尚不能证明什么”；对照行、命名配置和数值留在表格中。\n"
        "- 分开组件消融、机制 probe、超参敏感性、效率/scaling 和失败案例，但每类只保留必要解释；关键表仍完整保留命名行。\n"
        "- 不复制第 4 章主表。需要引用主结果时用文字和章节名回指，仅还原分配给本章的 source 表。"
    ),
    "closing": (
        "- 局限只写 source 支持的事实；基于结果作出的外推必须明确标成推断。\n"
        "- 整体评价分别讨论贡献、证据可信度、适用边界和阅读价值，不重复粘贴前文章节的图表或结果表。"
    ),
}


def build_section_user_prompt(
    evidence_prefix: str,
    section_key: str,
    paper_id: str,
    markers: List[str] | None = None,
    table_ids: List[int] | None = None,
) -> str:
    title, task = SECTION_GENERATION_TASKS[section_key]
    section_requirements = SECTION_GENERATION_REQUIREMENTS[section_key]
    length_instruction = (
        "- 方法章以读者能顺着输入、核心动作和输出复述流程为停止条件；讲清后立即收束，不追求符号或证明完备。"
        if section_key == "method"
        else "- 保持紧凑：图表不计入篇幅，非表格正文只写理解本章必需的上下文和结论；禁止逐行复述图表。"
    )
    marker_text = "\n".join(f"- {marker}" for marker in (markers or [])) or "- 无"
    table_text = "\n".join(f"- Table {table_id}，输出前独立保留标记 `[MaxReadTable:{table_id}]`" for table_id in (table_ids or [])) or "- 无"
    return (
        evidence_prefix.rstrip()
        + "\n\n分章生成任务："
        + title
        + "\n"
        + task
        + "\n\n本章允许且必须保留的图片 marker：\n"
        + marker_text
        + "\n\n本章必须还原且只能在本章出现的 source 表格：\n"
        + table_text
        + "\n\n本章输出合同（最终检查后直接输出 Markdown）：\n"
        + "- 只输出本章要求的内容，不要 JSON、解释或代码围栏。\n"
        + "- 数学公式只用 `<latex>...</latex>`；程序标识符使用反引号。\n"
        + "- 事实与数字只来自前缀证据；不确定时明确说明，不得补造。\n"
        + "- 允许 marker 必须逐字保留、独立成行并放在相关论述附近；不得新增其他 marker。\n"
        + "- 每个指定 Table 必须还原为 Markdown 表格，并在表格前独立输出对应 `[MaxReadTable:N]`；不得输出其他 Table marker。\n"
        + "- 除开篇固定概览表和本章被分配的 source 表，不要自行创造与其他章节重复的摘要表；跨章重复结论改用文字回指。\n"
        + "- 图和表都采用唯一所有权：只处理分配给本章的项目，不预告、复制或重新排版其他章节的图表。\n"
        + section_requirements
        + "\n"
        + length_instruction
        + "\n"
        + f"- 论文 ID：{paper_id}。"
    )


def _figure_marker_text(
    figure_inserts: List[Tuple[str, Path, str]],
    figure_visuals: Dict[str, str] | None = None,
    figure_owners: Dict[str, str] | None = None,
) -> str:
    if not figure_inserts:
        return "- 无"
    return "\n".join(figure_prompt_lines(figure_inserts, figure_visuals or {}, figure_owners or {}))


def _figure_pair_text(bundle: PaperBundle) -> str:
    if not bundle.source_figures:
        return "- 无"
    lines = []
    for figure in [item for item in bundle.source_figures if not getattr(item, "is_appendix", False)][:60]:
        label = f" label={figure.label}" if figure.label else ""
        owner = f" owner={figure.owner_section} evidence={figure.owner_evidence}" if figure.owner_section else " owner=unknown"
        lines.append(f"- asset={figure.asset}{label} file={figure.tex_file}{owner} caption={figure.caption}")
    return "\n".join(lines)



def _figure_reference_text(bundle: PaperBundle) -> str:
    if not bundle.source_figures or not bundle.source_text:
        return "- 无"
    labels = [figure.label for figure in bundle.source_figures if figure.label and not getattr(figure, "is_appendix", False)]
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


def _repository_url_text(bundle: PaperBundle) -> str:
    url = _find_repository_url(bundle)
    return url or "无"


def _find_repository_url(bundle: PaperBundle) -> str:
    return find_repository_url(bundle)


def _is_repository_url(url: str) -> bool:
    lower = url.lower()
    if any(host in lower for host in (
        "github.com/",
        "gitlab.com/",
        "bitbucket.org/",
        "codeberg.org/",
        "sourceforge.net/",
        "huggingface.co/",
    )):
        return True
    return any(word in lower for word in (
        "/code",
        "project",
        "github.io",
        "software",
        "demo",
    ))

def fallback_markdown(bundle: PaperBundle, reason: str) -> str:
    metadata = bundle.metadata
    warnings = "\n".join(f"- {item}" for item in bundle.parse_warnings) or "- 无"
    repository_url = _find_repository_url(bundle)
    repository_line = f"仓库：{repository_url}\n" if repository_url else ""
    return f"""# {metadata.title}
原文：{metadata.abs_url}
{repository_line}\
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
