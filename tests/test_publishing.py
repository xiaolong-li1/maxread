from pathlib import Path

from maxread.publishing import image_display_size, image_display_width, prepare_feishu_image, publish_marker_image


class FlakyFeishu:
    def __init__(self, anchor_id="anchor_1", fail_insert=False, missing_block_id=False, fail_move=False, fail_delete=False, fail_remove=False):
        self.anchor_id = anchor_id
        self.fail_insert = fail_insert
        self.missing_block_id = missing_block_id
        self.fail_move = fail_move
        self.fail_delete = fail_delete
        self.fail_remove = fail_remove
        self.insert_calls = []
        self.moves = []
        self.deleted = []
        self.removed = []

    def find_text_block_id(self, doc_url, text):
        return self.anchor_id

    def insert_image(self, doc_url, image_path, caption="", width=720, height=0):
        self.insert_calls.append({"doc_url": doc_url, "image_path": image_path, "caption": caption, "width": width, "height": height})
        if self.fail_insert:
            raise RuntimeError("server internal error")
        return {"data": {"block_id": "image_1"}} if not self.missing_block_id else {"ok": True}

    def move_block_after(self, doc_url, anchor_block_id, source_block_id):
        self.moves.append((anchor_block_id, source_block_id))
        if self.fail_move:
            raise RuntimeError("move failed")
        return {"ok": True}

    def delete_block(self, doc_url, block_id):
        self.deleted.append(block_id)
        if self.fail_delete:
            raise RuntimeError("delete failed")
        return {"ok": True}

    def remove_text(self, doc_url, text):
        self.removed.append(text)
        if self.fail_remove:
            raise RuntimeError("remove failed")
        return {"ok": True}


def _png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGBA", (20, 12), (255, 0, 0, 120)).save(path)
    return path


def test_publish_marker_image_moves_uploaded_block_and_removes_marker(tmp_path):
    image = _png(tmp_path / "main.png")
    feishu = FlakyFeishu()

    result = publish_marker_image(feishu, "doc", image, "Main overview", "[MaxReadFigure:1:main]")

    assert result.inserted is True
    assert result.marker_removed is True
    assert len(feishu.insert_calls) == 1
    assert feishu.insert_calls[0]["width"] == 640
    assert feishu.insert_calls[0]["height"] == 384
    assert feishu.moves == [("anchor_1", "image_1")]
    assert feishu.deleted == ["anchor_1"]
    assert feishu.removed == []
    assert result.warnings == []


def test_publish_marker_image_does_not_append_when_anchor_fails(tmp_path):
    image = _png(tmp_path / "overview.png")
    feishu = FlakyFeishu(anchor_id="")

    result = publish_marker_image(feishu, "doc", image, "Overview", "[MaxReadFigure:1:overview]")

    assert result.inserted is False
    assert result.fallback_appended is False
    assert feishu.insert_calls == []
    assert feishu.removed == []
    assert any(item.startswith("image-anchor-missing:overview.png") for item in result.warnings)


def test_publish_marker_image_keeps_marker_when_upload_fails(tmp_path):
    image = _png(tmp_path / "pipeline.png")
    feishu = FlakyFeishu(fail_insert=True)

    result = publish_marker_image(feishu, "doc", image, "Pipeline", "[MaxReadFigure:1:pipeline]")

    assert result.inserted is False
    assert feishu.removed == []
    assert any(item.startswith("image-insert-failed:pipeline.png") for item in result.warnings)


def test_publish_marker_image_reports_missing_uploaded_block_id(tmp_path):
    image = _png(tmp_path / "missing-id.png")
    feishu = FlakyFeishu(missing_block_id=True)

    result = publish_marker_image(feishu, "doc", image, "Missing id", "[MaxReadFigure:1:missing]")

    assert result.inserted is False
    assert result.fallback_appended is True
    assert feishu.moves == []
    assert any(item.startswith("image-block-id-missing:missing-id.png") for item in result.warnings)


def test_publish_marker_image_rolls_back_appended_block_when_move_fails(tmp_path):
    image = _png(tmp_path / "move.png")
    feishu = FlakyFeishu(fail_move=True)

    result = publish_marker_image(feishu, "doc", image, "Move", "[MaxReadFigure:1:move]")

    assert result.inserted is False
    assert feishu.deleted == ["image_1"]
    assert feishu.removed == []
    assert any(item.startswith("image-move-failed:move.png") for item in result.warnings)


def test_publish_marker_image_flags_appended_image_when_rollback_fails(tmp_path):
    image = _png(tmp_path / "rollback.png")
    feishu = FlakyFeishu(fail_move=True, fail_delete=True)

    result = publish_marker_image(feishu, "doc", image, "Rollback", "[MaxReadFigure:1:rollback]")

    assert result.inserted is False
    assert result.fallback_appended is True
    assert any(item.startswith("image-rollback-failed:rollback.png") for item in result.warnings)


def test_publish_marker_image_keeps_inserted_image_when_marker_cleanup_fails(tmp_path):
    image = _png(tmp_path / "cleanup.png")
    feishu = FlakyFeishu(fail_delete=True)

    result = publish_marker_image(feishu, "doc", image, "Cleanup", "[MaxReadFigure:1:cleanup]")

    assert result.inserted is True
    assert result.marker_removed is False
    assert feishu.deleted == ["anchor_1"]
    assert any(item.startswith("image-marker-remove-failed:cleanup.png") for item in result.warnings)


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


def test_image_display_size_caps_tall_composite_without_cropping(tmp_path):
    from PIL import Image

    composite = tmp_path / "densification.png"
    Image.new("RGB", (1037, 1246), "white").save(composite)

    width, height = image_display_size(composite)

    assert (width, height) == (466, 560)
    assert abs(width / height - 1037 / 1246) < 0.01


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


def test_prepare_feishu_image_constrains_upload_copy_without_rejecting_or_rewriting_source(tmp_path):
    import hashlib
    import os

    from PIL import Image

    source = tmp_path / "large-source.png"
    image = Image.frombytes("RGB", (2400, 1800), os.urandom(2400 * 1800 * 3))
    image.save(source, format="PNG")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert source.stat().st_size > 10 * 1024 * 1024

    safe = prepare_feishu_image(source)

    assert safe != source
    assert safe.parent.name == "feishu_safe"
    assert safe.exists()
    assert safe.stat().st_size <= 10 * 1024 * 1024
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    with Image.open(safe) as opened:
        assert max(opened.size) <= 2200
