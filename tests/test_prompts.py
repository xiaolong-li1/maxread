from pathlib import Path

from maxread.models import ArxivMetadata, PaperBundle
from maxread.prompts import FINAL_SYSTEM_PROMPT, build_final_user_prompt
from maxread.review import REVIEW_SYSTEM_PROMPT, build_review_user_prompt, parse_review_response, review_markdown, review_markdown_with_report, visible_review_issues


def _bundle():
    return PaperBundle(
        metadata=ArxivMetadata(
            paper_id="2604.12946",
            title="Fake Method Paper",
            authors=["A"],
            summary="Abstract",
            published="",
            updated="",
            categories=["cs.CL"],
            pdf_url="",
            abs_url="https://arxiv.org/abs/2604.12946",
        ),
        pdf_path=Path("paper.pdf"),
        source_path=Path("paper.source"),
        source_dir=Path("source"),
        source_text="\\section{Method} Detailed method with variables x and y.",
        pdf_text="",
    )


def test_paper_prompt_requires_method_fidelity():
    prompt = build_final_user_prompt(_bundle())
    assert "方法部分保真优先" in FINAL_SYSTEM_PROMPT
    assert "Method/Approach/Algorithm/Model/Training/Inference" in prompt
    assert "输入是什么、输出是什么" in prompt
    assert "变量含义、计算顺序" in prompt
    assert "原文未展开" in prompt


def test_paper_prompt_includes_visual_figure_description():
    marker = "[MaxReadFigure:1:plot]"
    prompt = build_final_user_prompt(
        _bundle(),
        [(marker, Path("plot.png"), "Caption says loss curve.")],
        {marker: "图中有两张折线图，左侧是训练 loss，右侧是 perplexity。"},
    )
    assert "visual：图中有两张折线图" in prompt
    assert "不要只参考文件名" in prompt


def test_paper_prompt_requires_readable_h1_title():
    prompt = build_final_user_prompt(_bundle())
    assert "标题规则" in prompt
    assert "研究问题 + 方法抓手" in prompt
    assert "不要写“深网”" in prompt
    assert "深层网络一定要很深吗？" in prompt
    assert "用深层模型蒸馏浅层网络" in prompt


def test_review_prompt_checks_abstract_h1_title():
    prompt = build_review_user_prompt("# [1312.6184] 深网真的需要很深吗：用模型压缩训练浅层网络复刻深层函数", [])
    assert "H1 标题是否自然、短、具体" in prompt
    assert "深网/复刻函数" in REVIEW_SYSTEM_PROMPT
    assert "抽象废话" in REVIEW_SYSTEM_PROMPT


def test_review_prompt_preserves_markers_and_checks_format():
    prompt = build_review_user_prompt("正文\n\n[MaxReadFigure:1:local]", ["[MaxReadFigure:1:local]"])
    assert "只输出 JSON" in REVIEW_SYSTEM_PROMPT
    assert "[MaxReadFigure:1:local]" in prompt
    assert "长英文 caption" in prompt
    assert "\\formername" in prompt


class _FenceLLM:
    def responses_text(self, system, user):
        return '{"markdown":"# T\\n\\n[MaxReadFigure:1:a]", "issues":[{"category":"tex_macro", "severity":"medium", "detail":"清理宏"}]}'


def test_review_markdown_strips_code_fence():
    out = review_markdown(_FenceLLM(), "# T", ["[MaxReadFigure:1:a]"])
    assert out.startswith("# T")
    assert "```" not in out


class _RefusalLLM:
    def responses_text(self, system, user):
        return "I'm sorry, but I cannot assist with that request."


def test_review_markdown_keeps_original_when_reviewer_returns_refusal():
    original = "# 原文标题\n\n正文。"
    result = review_markdown_with_report(_RefusalLLM(), original, [])
    assert result.markdown == original + "\n"
    assert any("non-json" in issue.detail for issue in result.issues)
    assert any("kept original" in issue.detail for issue in result.issues)


def test_parse_review_response_collects_issues():
    result = parse_review_response('{"markdown":"# T", "issues":[{"category":"english_caption", "severity":"medium", "detail":"长英文图注"}]}')
    assert result.markdown == "# T\n"
    assert result.issues[0].category == "english_caption"
    assert result.issues[0].detail == "长英文图注"


def test_parse_review_response_filters_resolved_fix_notes():
    raw = '{"markdown":"# T", "issues":[{"category":"heading", "severity":"low", "detail":"修正一级标题中重复标点。"}, {"category":"factual_risk", "severity":"high", "detail":"仍存在实验结论与表格不一致，需要人工检查。"}]}'
    result = parse_review_response(raw)
    assert len(result.issues) == 1
    assert result.issues[0].category == "factual_risk"


def test_visible_review_issues_hides_existing_resolved_rows():
    rows = [
        {"detail": "已改为中文转述。"},
        {"detail": "候选清单中的 marker 未在原稿出现，按要求未补回。"},
        {"detail": "将少量英文小节标题调整为中文。"},
        {"detail": "图注中存在英文提示词，已补充中文转述。"},
        {"detail": "仍存在图文错位，需要人工检查。"},
    ]
    assert visible_review_issues(rows) == [rows[4]]
