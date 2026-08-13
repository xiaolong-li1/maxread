from maxread.models import ArticleImage, ArticleSection
from maxread.web_article import ArticleHTMLParser
from maxread.web_article import RenderedSnapshot
from maxread.web_article import _article_material_too_thin
from maxread.web_article import _inject_rendered_snapshot_blocks
from maxread.web_article import _is_raster_image
from maxread.web_article import _is_svg_image
from maxread.web_article import _should_capture_rendered_snapshots


def test_article_parser_extracts_text_images_tables_code():
    html = """
    <html><head><title>Demo</title><meta name="author" content="A"></head>
    <body><article>
      <h1>Main Title</h1>
      <p>First paragraph with $x+y$.</p>
      <figure><img src="/img.png" alt="Alt text"><figcaption>Figure caption</figcaption></figure>
      <pre><code>print(1)</code></pre>
      <table><tr><td>A</td><td>B</td></tr></table>
    </article></body></html>
    """
    parser = ArticleHTMLParser("https://example.com/post")
    parser.feed(html)
    assert parser.title == "Demo"
    assert "Main Title" in parser.sections
    assert "First paragraph" in parser.main_text()
    assert parser.images[0].url == "https://example.com/img.png"
    assert parser.images[0].caption == "Figure caption"
    available_images = [ArticleImage(url=parser.images[0].url, local_path=__file__, caption=parser.images[0].caption, source_index=1)]
    assert "[ArticleImage:1] Alt text" in parser.main_text(available_images)
    assert "print(1)" in parser.code_blocks[0]
    assert "A B" in parser.tables[0]


def test_article_parser_drops_obvious_incomplete_claims():
    parser = ArticleHTMLParser("https://example.com/post")
    parser.feed("<html><body><p>We find that .</p><p>Complete finding remains.</p></body></html>")

    text = parser.main_text()

    assert "We find that ." not in text
    assert "Complete finding remains." in text


def test_raster_image_detection_rejects_svg():
    assert _is_raster_image(b"\x89PNG\r\n\x1a\nxxx") is True
    assert _is_raster_image(b"<svg></svg>") is False



def test_article_parser_builds_section_blocks_in_source_order():
    html = """
    <html><body><article>
      <h2>First section</h2>
      <p>Alpha text.</p>
      <figure><img src="/first.png" alt="First alt"><figcaption>First caption</figcaption></figure>
      <h2>Second section</h2>
      <p>Beta text.</p>
      <figure><img src="/second.svg" alt="Second alt"><figcaption>Second caption</figcaption></figure>
    </article></body></html>
    """
    parser = ArticleHTMLParser("https://example.com/post")
    parser.feed(html)
    available_images = [
        ArticleImage(url=parser.images[0].url, local_path=__file__, caption=parser.images[0].caption, source_index=1),
        ArticleImage(url=parser.images[1].url, local_path=None, caption=parser.images[1].caption, source_index=2),
    ]

    sections = parser.section_blocks(available_images)

    assert [section.title for section in sections] == ["First section", "Second section"]
    assert sections[0].blocks == ["Alpha text.", "[ArticleImage:1] First caption"]
    assert sections[1].blocks == ["Beta text."]


def test_article_parser_keeps_nested_heading_text_and_distill_math():
    html = """
    <html><body><d-article>
      <h2><a id="methods" href="#methods">Methods</a></h2>
      <p>The residual stream <d-math>h_\\ell</d-math> is updated.</p>
      <p><d-math block>J_\\ell = \\mathbb{E}[\\partial h_L / \\partial h_\\ell]</d-math></p>
      <h3><a id="jlens" href="#jlens">The Jacobian Lens</a></h3>
      <p><span style="font-style: italic;">Verbalizable</span> concepts are reported.</p>
    </d-article></body></html>
    """
    parser = ArticleHTMLParser("https://example.com/post")
    parser.feed(html)

    sections = parser.section_blocks()

    assert parser.sections == ["Methods", "The Jacobian Lens"]
    assert parser.math_blocks == [
        r"h_\ell",
        r"J_\ell = \mathbb{E}[\partial h_L / \partial h_\ell]",
    ]
    assert sections[0].title == "Methods"
    assert r"The residual stream <latex>h_\ell</latex> is updated." in sections[0].blocks
    assert r"<latex>J_\ell = \mathbb{E}[\partial h_L / \partial h_\ell]</latex>" in sections[0].blocks
    assert sections[1].blocks == ["Verbalizable concepts are reported."]


def test_article_parser_attaches_visual_toc_images_to_matching_sections():
    html = """
    <html><body>
      <nav class="visual-toc">
        <a href="#methods" class="visual-toc-top">
          <figure><img src="./png/methods.png"/></figure>
          <d-tochead>Methods</d-tochead>
        </a>
      </nav>
      <article>
        <h2><a id="methods">Methods</a></h2>
        <p>Method body.</p>
      </article>
    </body></html>
    """
    parser = ArticleHTMLParser("https://example.com/post/index.html")
    parser.feed(html)
    available_images = [
        ArticleImage(url=parser.images[0].url, local_path=__file__, caption=parser.images[0].caption, source_index=1),
    ]

    sections = parser.section_blocks(available_images)

    assert parser.images[0].url == "https://example.com/post/png/methods.png"
    assert parser.images[0].caption == "Methods"
    assert parser.visual_toc_image_indexes() == {1}
    assert sections[0].title == "Methods"
    assert sections[0].blocks == ["[ArticleImage:1] Methods", "Method body."]


def test_rendered_snapshots_are_inserted_by_original_section(tmp_path):
    overview = ArticleImage("rendered:10", tmp_path / "overview.png", caption="原网页标题区和可视目录", source_index=10)
    method = ArticleImage("rendered:11", tmp_path / "method.png", caption="Method figure", source_index=11)
    later = ArticleImage("rendered:12", tmp_path / "later.png", caption="Later figure", source_index=12)
    sections = [
        ArticleSection("Methods", 2, ["Method body explains setup.", "More text explains results."]),
        ArticleSection("Results", 2, ["Result body."]),
    ]
    snapshots = [
        RenderedSnapshot(overview, section_title="", y=100, kind="overview"),
        RenderedSnapshot(method, section_title="Methods", anchor_text="Method body explains setup.", y=300, kind="figure"),
        RenderedSnapshot(later, section_title="Methods", anchor_text="More text explains results.", y=500, kind="figure"),
    ]

    updated = _inject_rendered_snapshot_blocks(sections, snapshots)

    assert updated[0].title == "原网页标题区和可视目录"
    assert updated[0].blocks == ["[ArticleImage:10] 原网页标题区和可视目录"]
    assert updated[1].title == "Methods"
    assert updated[1].blocks == [
        "Method body explains setup.",
        "[ArticleImage:11] Method figure",
        "More text explains results.",
        "[ArticleImage:12] Later figure",
    ]
    assert updated[2].title == "Results"


def test_unsectioned_rendered_snapshot_stays_in_first_content_section(tmp_path):
    chart = ArticleImage("rendered:10", tmp_path / "chart.png", caption="Benchmark chart", source_index=10)
    sections = [
        ArticleSection("Title", 1, ["Intro text explains benchmark."]),
        ArticleSection("Later", 2, ["Later body."]),
    ]
    snapshots = [RenderedSnapshot(chart, section_title="", anchor_text="Intro text explains benchmark.", y=300, kind="div")]

    updated = _inject_rendered_snapshot_blocks(sections, snapshots)

    assert [section.title for section in updated] == ["Title", "Later"]
    assert updated[0].blocks == ["Intro text explains benchmark.", "[ArticleImage:10] Benchmark chart"]
    assert updated[1].blocks == ["Later body."]


def test_should_capture_rendered_snapshots_for_visual_blogs():
    assert _should_capture_rendered_snapshots("https://transformer-circuits.pub/2026/workspace/index.html", "<html></html>")
    assert _should_capture_rendered_snapshots("https://example.com/post", "<d-article><svg></svg></d-article>")
    assert not _should_capture_rendered_snapshots("https://example.com/post", "<article><p>Plain text</p></article>")


def test_article_material_too_thin_requires_rendered_fallback():
    assert _article_material_too_thin("", [], [], [], [], [])
    assert not _article_material_too_thin(
        "这是一段足够长的正文。" * 80,
        [],
        [],
        [],
        [],
        [],
    )
    assert not _article_material_too_thin(
        "short",
        ["Methods"],
        [],
        [],
        [],
        [],
    )


def test_article_image_marker_replacement_uses_source_index(tmp_path):
    from maxread.article_pipeline import _replace_article_image_markers

    img1 = tmp_path / "first.png"
    img3 = tmp_path / "third.png"
    img1.write_bytes(b"\x89PNG\r\n\x1a\nxxx")
    img3.write_bytes(b"\x89PNG\r\n\x1a\nxxx")
    text = "[ArticleImage:1] one\n\n[ArticleImage:3] three"
    inserts = [
        ("[MaxReadFigure:1:first]", img1, "Caption one", 1),
        ("[MaxReadFigure:2:third]", img3, "Caption three", 3),
    ]

    replaced = _replace_article_image_markers(text, inserts)

    assert "[MaxReadFigure:1:first]\n**图：Caption one**" in replaced
    assert "[MaxReadFigure:2:third]\n**图：Caption three**" in replaced



def test_svg_image_detection():
    assert _is_svg_image(b"<?xml version='1.0'?><svg></svg>") is True
    assert _is_svg_image(b"   <svg viewBox='0 0 10 10'></svg>") is True
    assert _is_svg_image(b"\x89PNG\r\n\x1a\nxxx") is False
