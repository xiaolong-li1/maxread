from maxread.models import ArticleImage
from maxread.web_article import ArticleHTMLParser
from maxread.web_article import _is_raster_image
from maxread.web_article import _is_svg_image


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
