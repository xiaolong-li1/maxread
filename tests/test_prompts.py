import json
from pathlib import Path

from maxread import repository
from maxread.article_prompts import ARTICLE_SYSTEM_PROMPT, build_article_user_prompt
from maxread.models import ArxivMetadata, ArticleBundle, ArticleSection, PaperBundle
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
    assert "为什么需要它 -> 接收什么 -> 如何计算/执行" in prompt
    assert "端到端例子" in prompt
    assert "35%-50%" in prompt
    assert "原文未展开" in prompt


def test_paper_prompt_keeps_program_identifiers_out_of_latex():
    prompt = build_final_user_prompt(_bundle())

    assert "程序标识符必须使用 Markdown 行内代码" in FINAL_SYSTEM_PROMPT
    assert "`tensor_meta()`" in prompt
    assert "`publish(req_id)`" in prompt


def test_paper_prompt_allows_selective_appendix_evidence():
    prompt = build_final_user_prompt(_bundle())
    assert "附录/Appendix/Supplement" in prompt
    assert "关键扩展实验" in prompt
    assert "不要机械堆附录细节" in prompt


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


def test_article_prompt_prefers_blog_localization_over_relayout():
    bundle = ArticleBundle(
        article_id="a1",
        url="https://example.com/blog",
        title="A Technical Blog",
        site_name="Example",
        section_blocks=[
            ArticleSection(title="Introduction", level=2, blocks=["This section introduces the idea."]),
            ArticleSection(title="Methods", level=2, blocks=["This section explains the method."]),
        ],
        sections=["Introduction", "Methods"],
    )
    prompt = build_article_user_prompt(bundle, [])

    assert "汉化后的原文导读" in ARTICLE_SYSTEM_PROMPT
    assert "逐 section 汉化重述" in ARTICLE_SYSTEM_PROMPT
    assert "## 总结" in prompt
    assert "## 正文汉化" in prompt
    assert "## 阅读提示" in prompt
    assert "不要把 blog 改造成论文阅读报告" in prompt
    assert "不要新增总结表格" in prompt


def test_paper_prompt_omits_authors_and_adds_optional_repository_row():
    bundle = _bundle()
    bundle.source_text += "\nCode is available at https://github.com/example/maxread-paper."
    prompt = build_final_user_prompt(bundle)
    assert "{{作者列表}}" not in prompt
    assert "- Authors:" not in prompt
    assert "开头不要放作者列表或作者信息" in prompt
    assert "| 仓库 |" in prompt
    assert "Repository URL: https://github.com/example/maxread-paper" in prompt


def test_paper_prompt_repository_is_none_when_absent():
    prompt = build_final_user_prompt(_bundle())
    assert "Repository URL: 无" in prompt


def test_paper_prompt_ignores_template_link_and_uses_explicit_code_url():
    bundle = _bundle()
    bundle.source_text += """
% This version of CVPR template is provided by Ming-Ming Cheng.
% https://github.com/MCG-NKU/CVPR_Template.
Our code is available at: https://github.com/hila-chefer/Transformer-Explainability.
"""

    prompt = build_final_user_prompt(bundle)

    assert "Repository URL: https://github.com/hila-chefer/Transformer-Explainability" in prompt
    assert "Repository URL: https://github.com/MCG-NKU/CVPR_Template" not in prompt


def test_paper_prompt_strips_tex_comment_urls_before_repository_detection():
    bundle = _bundle()
    bundle.source_text += "\n% Code: https://github.com/example/comment-only-repository"

    prompt = build_final_user_prompt(bundle)

    assert "Repository URL: 无" in prompt


def test_paper_prompt_excludes_bibliography_repository_links():
    bundle = _bundle()
    bundle.source_text += """
% FILE: main.tex
No project repository is provided.
% FILE: references.bib
note = {Repository: https://github.com/cited/model}
"""

    prompt = build_final_user_prompt(bundle)

    assert "Repository URL: 无" in prompt


def test_paper_prompt_excludes_pdf_reference_project_pages_when_source_exists():
    bundle = _bundle()
    bundle.pdf_text = """
Main paper text without a project link.

References
Cited project page: https://cited-work.github.io/project
"""

    prompt = build_final_user_prompt(bundle)

    assert repository.find_repository_url(bundle) == ""
    assert "Repository URL: 无" in prompt


def test_paper_prompt_strips_pdf_references_when_source_is_unavailable():
    bundle = _bundle()
    bundle.source_text = ""
    bundle.pdf_text = ("Main paper discussion without a repository. " * 20) + """

References
Cited project page: https://cited-work.github.io/project
"""

    prompt = build_final_user_prompt(bundle)

    assert repository.find_repository_url(bundle) == ""
    assert "Repository URL: 无" in prompt


def test_paper_prompt_ignores_regex_that_looks_like_invalid_url():
    bundle = _bundle()
    bundle.source_text += r"\nMatching expression: http://[^/]*"

    prompt = build_final_user_prompt(bundle)

    assert "Repository URL: 无" in prompt


def test_paper_prompt_resolves_code_link_from_project_page():
    bundle = _bundle()
    bundle.source_text += "\nProject page: https://gca-spatial-reasoning.github.io/"
    html = """
    <a href="https://github.com/Zx55">Zeren Chen</a>
    <a href="https://github.com/gca-spatial-reasoning/gca">Code</a>
    """
    original_urlopen = repository.urllib.request.urlopen

    class Response:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return html.encode("utf-8")

    def fake_urlopen(_request, timeout=0):
        return Response()

    try:
        repository.urllib.request.urlopen = fake_urlopen
        prompt = build_final_user_prompt(bundle)
    finally:
        repository.urllib.request.urlopen = original_urlopen

    assert "Repository URL: https://github.com/gca-spatial-reasoning/gca" in prompt
    assert "https://github.com/Zx55" not in prompt


def test_paper_prompt_resolves_homepage_context_to_github_repo():
    bundle = _bundle()
    bundle.source_text += "\nProject page: http://ziplab.co/PSA"
    html = """
    <a href="https://github.com/ziplab/Pyramid-Sparse-Attention">Code</a>
    """
    original_urlopen = repository.urllib.request.urlopen

    class Response:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return html.encode("utf-8")

    def fake_urlopen(request, timeout=0):
        assert request.full_url == "http://ziplab.co/PSA"
        return Response()

    try:
        repository.urllib.request.urlopen = fake_urlopen
        prompt = build_final_user_prompt(bundle)
    finally:
        repository.urllib.request.urlopen = original_urlopen

    assert "Repository URL: https://github.com/ziplab/Pyramid-Sparse-Attention" in prompt
    assert "Repository URL: http://ziplab.co/PSA" not in prompt


def test_paper_prompt_does_not_use_unresolved_project_page_as_repository():
    bundle = _bundle()
    bundle.source_text += "\nProject page: https://example.com/project"
    original_urlopen = repository.urllib.request.urlopen

    class Response:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return b"<html><a href='/'>Home</a></html>"

    def fake_urlopen(_request, timeout=0):
        return Response()

    try:
        repository.urllib.request.urlopen = fake_urlopen
        prompt = build_final_user_prompt(bundle)
    finally:
        repository.urllib.request.urlopen = original_urlopen

    assert "Repository URL: 无" in prompt
    assert "Repository URL: https://example.com/project" not in prompt


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
    assert "不要仅凭 marker 名称" in prompt
    assert "程序标识符" in REVIEW_SYSTEM_PROMPT
    assert "不要把程序标识符包装成 `<latex>`" in prompt


class _MethodTruncatingReviewLLM:
    def __init__(self, reviewed):
        self.reviewed = reviewed

    def responses_text(self, system, user, **kwargs):
        return json.dumps({"markdown": self.reviewed, "issues": []}, ensure_ascii=False)


def test_review_markdown_keeps_original_when_review_truncates_method_section():
    method = "## 3. 方法框架\n\n" + ("模块接收状态并计算下一阶段输入，随后解释公式直觉。" * 45)
    tail = "\n\n## 4. 实验结果\n\n" + ("实验结果完整。" * 180)
    original = "# 标题\n\n**TL;DR**：摘要。\n\n" + method + tail
    reviewed = "# 标题\n\n**TL;DR**：摘要。\n\n## 3. 方法框架\n\n方法有效。" + tail

    result = review_markdown_with_report(_MethodTruncatingReviewLLM(reviewed), original, [])

    assert result.markdown == original + "\n"
    assert any("truncated method section" in issue.detail for issue in result.issues)


class _FenceLLM:
    def responses_text(self, system, user, **kwargs):
        return '{"markdown":"# T\\n\\n[MaxReadFigure:1:a]", "issues":[{"category":"tex_macro", "severity":"medium", "detail":"清理宏"}]}'


def test_review_markdown_strips_code_fence():
    out = review_markdown(_FenceLLM(), "# T", ["[MaxReadFigure:1:a]"])
    assert out.startswith("# T")
    assert "```" not in out


class _RefusalLLM:
    def responses_text(self, system, user, **kwargs):
        return "I'm sorry, but I cannot assist with that request."


class _ReasoningLLM:
    def __init__(self):
        self.kwargs = None

    def responses_text(self, system, user, **kwargs):
        self.kwargs = kwargs
        return '{"markdown":"# T", "issues":[]}'


class _TruncatedReviewLLM:
    def responses_text(self, system, user, **kwargs):
        return '{"markdown":"微调时，只剩文章后半段。", "issues":[]}'


def test_review_markdown_uses_override_reasoning_effort():
    llm = _ReasoningLLM()
    review_markdown_with_report(llm, "# T", [], reasoning_effort="low")
    assert llm.kwargs == {"reasoning_effort": "low"}


def test_review_markdown_keeps_original_when_reviewer_returns_refusal():
    original = "# 原文标题\n\n正文。"
    result = review_markdown_with_report(_RefusalLLM(), original, [])
    assert result.markdown == original + "\n"
    assert any("non-json" in issue.detail for issue in result.issues)
    assert any("kept original" in issue.detail for issue in result.issues)


def test_review_markdown_keeps_original_when_review_drops_document_front_half():
    original = "# BERT\n\n**TL;DR**：摘要。\n\n" + ("完整正文。" * 300) + "\n\n[MaxReadFigure:1:bert]"

    result = review_markdown_with_report(_TruncatedReviewLLM(), original, ["[MaxReadFigure:1:bert]"])

    assert result.markdown == original + "\n"
    assert any(issue.category == "layout" and issue.severity == "high" and "kept original" in issue.detail for issue in result.issues)


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


def test_visible_review_issues_hides_marker_name_only_false_positive():
    rows = [
        {
            "category": "figure_marker",
            "detail": "[MaxReadFigure:5:tre] 的 marker 名称像机构或 Logo，与当前段落可能不匹配，需人工确认原图。",
        },
        {
            "category": "figure_marker",
            "detail": "图解和周围段落明显不匹配，无法判断，需要人工检查。",
        },
    ]
    assert visible_review_issues(rows) == [rows[1]]
