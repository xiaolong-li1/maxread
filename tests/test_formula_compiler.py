from maxread.formula_compiler import FormulaTokenKind, compile_formula_markup
from maxread.render import markdown_to_docx_xml, polish_markdown


def test_compiler_removes_html_paragraph_wrapper_around_formula():
    result = compile_formula_markup("说明：<p><latex>V=[T;F;M]</latex></p>结束")

    assert result.text == "说明：<latex>V=[T;F;M]</latex>结束"
    assert any(token.kind == FormulaTokenKind.FORMULA for token in result.tokens)
    assert any(item.code == "recovered-formula-wrapper" for item in result.diagnostics)


def test_compiler_recovers_escaped_html_wrapper_without_unescaping_prose():
    result = compile_formula_markup("<p>&lt;p&gt;<latex>x<br/>y</latex>&lt;/p&gt;</p>")

    assert result.text == "<latex>x y</latex>"
    assert "&lt;p&gt;" not in result.text
    assert "<br" not in result.text


def test_xml_renderer_defensively_compiles_legacy_wrappers():
    xml = markdown_to_docx_xml("<p><latex>V=[T;F;M]</latex></p>")

    assert "&lt;p&gt;" not in xml
    assert "&lt;/p&gt;" not in xml
    assert "<latex>V=[T;F;M]</latex>" in xml


def test_unknown_html_in_formula_is_diagnosed_and_retained():
    result = compile_formula_markup("<latex>x<span>y</span></latex>")

    assert "<span>y</span>" in result.text
    assert any(item.code == "unknown-html-in-formula" and item.severity == "high" for item in result.diagnostics)


def test_compiler_recovers_overescaped_latex_commands_but_keeps_row_breaks():
    result = compile_formula_markup(
        r"<latex>\\mathbf{x}=\\mathcal{N}(x)\\mathrm{d}x\\begin{aligned}a\\ b\\j\\le i</latex>"
    )

    assert result.text == r"<latex>\mathbf{x}=\mathcal{N}(x)\mathrm{d}x\begin{aligned}a\\ b\\j\le i</latex>"


def test_polish_markdown_handles_legacy_formula_wrappers():
    output = polish_markdown("公式：<p><latex>V=[T;F;M]</latex></p>")

    assert "<p>" not in output
    assert "</p>" not in output
    assert "<latex>V=[T;F;M]</latex>" in output
