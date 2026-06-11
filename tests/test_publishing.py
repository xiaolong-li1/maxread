from pathlib import Path

from maxread.publishing import image_display_width, prepare_feishu_image, publish_marker_image


class FlakyFeishu:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.insert_calls = []
        self.removed = []

    def insert_image(self, doc_url, image_path, caption="", width=720, height=0, selection=""):
        self.insert_calls.append({"doc_url": doc_url, "image_path": image_path, "caption": caption, "width": width, "height": height, "selection": selection})
        if len(self.insert_calls) <= self.fail_times:
            raise RuntimeError("server internal error")
        return {"ok": True}

    def remove_text(self, doc_url, text):
        self.removed.append(text)
        return {"ok": True}


def _png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGBA", (20, 12), (255, 0, 0, 120)).save(path)
    return path


def test_publish_marker_image_retries_and_removes_marker_after_success(tmp_path):
    image = _png(tmp_path / "main.png")
    feishu = FlakyFeishu(fail_times=1)

    result = publish_marker_image(feishu, "doc", image, "Main overview", "[MaxReadFigure:1:main]")

    assert result.inserted is True
    assert result.marker_removed is True
    assert len(feishu.insert_calls) == 2
    assert feishu.insert_calls[0]["selection"] == "[MaxReadFigure:1:main]"
    assert feishu.insert_calls[0]["width"] == 640
    assert feishu.insert_calls[0]["height"] == 384
    assert feishu.insert_calls[1]["width"] == 525
    assert feishu.insert_calls[1]["height"] == 315
    assert feishu.removed == ["[MaxReadFigure:1:main]"]
    assert any("image-insert-failed:main.png:selected" in item for item in result.warnings)


def test_publish_marker_image_appends_as_last_resort(tmp_path):
    image = _png(tmp_path / "overview.png")
    feishu = FlakyFeishu(fail_times=2)

    result = publish_marker_image(feishu, "doc", image, "Overview", "[MaxReadFigure:1:overview]")

    assert result.inserted is True
    assert result.fallback_appended is True
    assert feishu.insert_calls[-1]["selection"] == ""
    assert feishu.removed == ["[MaxReadFigure:1:overview]"]


def test_publish_marker_image_keeps_marker_when_all_attempts_fail(tmp_path):
    image = _png(tmp_path / "pipeline.png")
    feishu = FlakyFeishu(fail_times=99)

    result = publish_marker_image(feishu, "doc", image, "Pipeline", "[MaxReadFigure:1:pipeline]")

    assert result.inserted is False
    assert feishu.removed == []
    assert any(item.startswith("image-missing:pipeline.png") for item in result.warnings)


def test_prepare_feishu_image_writes_safe_rgb_png(tmp_path):
    image = _png(tmp_path / "rgba.png")

    safe = prepare_feishu_image(image)

    assert safe.name == "rgba.png"
    assert safe.parent.name == "feishu_safe"
    assert safe.exists()
    from PIL import Image

    with Image.open(safe) as opened:
        assert opened.mode == "RGB"


def test_prepare_feishu_image_crops_large_white_border(tmp_path):
    from PIL import Image, ImageDraw

    image = tmp_path / "wide.png"
    canvas = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((120, 40, 1080, 260), fill=(80, 30, 160))
    canvas.save(image)

    safe = prepare_feishu_image(image)

    with Image.open(safe) as opened:
        assert opened.height < 320
        assert opened.width > 900


def test_image_display_width_scales_wide_and_square_images(tmp_path):
    from PIL import Image

    wide = tmp_path / "wide.png"
    square = tmp_path / "square.png"
    Image.new("RGB", (2200, 500), "white").save(wide)
    Image.new("RGB", (800, 800), "white").save(square)

    assert image_display_width(wide) == 720
    assert image_display_width(square) == 560


def test_prepare_feishu_image_keeps_normal_chart_margins(tmp_path):
    from PIL import Image, ImageDraw

    image = tmp_path / "chart.png"
    canvas = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((30, 60, 970, 440), fill=(240, 240, 240), outline=(80, 80, 80))
    canvas.save(image)

    safe = prepare_feishu_image(image)

    with Image.open(safe) as opened:
        assert opened.size == (1000, 500)
