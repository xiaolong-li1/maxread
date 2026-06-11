from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Protocol

from .render import display_caption


class ImageDocClient(Protocol):
    def insert_image(self, doc_url: str, image_path: str, caption: str = "", width: int = 720, height: int = 0, selection: str = ""):
        ...

    def remove_text(self, doc_url: str, text: str):
        ...


@dataclass
class ImagePublishResult:
    marker: str
    image_path: Path
    inserted: bool = False
    fallback_appended: bool = False
    marker_removed: bool = False
    warnings: List[str] = field(default_factory=list)


def publish_marker_image(feishu: ImageDocClient, doc_url: str, image_path: Path | str, caption: str, marker: str) -> ImagePublishResult:
    """Insert an image for a marker with defensive fallbacks.

    Feishu media insertion has two independent failure modes: locating the
    selection block and binding the uploaded media.  A selected main figure
    should not disappear just because one media call fails, so we normalize the
    raster file and then try selected insertion before appending as a last-resort
    fallback.
    """
    original = Path(image_path)
    safe_path = prepare_feishu_image(original)
    display = display_caption(caption, safe_path)
    result = ImagePublishResult(marker=marker, image_path=safe_path)
    width, height = image_display_size(safe_path)
    small_width = max(360, min(width - 80, round(width * 0.82)))
    small_height = round(height * small_width / width) if width else 0
    fallback_width = min(width, 560)
    fallback_height = round(height * fallback_width / width) if width else 0

    attempts = [
        ("selected", marker, width, height),
        ("selected-small", marker, small_width, small_height),
        ("append-fallback", "", fallback_width, fallback_height),
    ]
    last_error = ""
    for label, selection, width, height in attempts:
        try:
            feishu.insert_image(doc_url, str(safe_path), caption=display, selection=selection, width=width, height=height)
            result.inserted = True
            result.fallback_appended = label == "append-fallback"
            if label != "selected":
                result.warnings.append(f"image-insert-fallback:{original.name}:{label}")
            break
        except Exception as exc:  # keep trying lower-risk placements
            last_error = str(exc)
            result.warnings.append(f"image-insert-failed:{original.name}:{label}:{_short_error(last_error)}")

    if not result.inserted:
        result.warnings.append(f"image-missing:{original.name}:{_short_error(last_error)}")
        return result

    try:
        feishu.remove_text(doc_url, marker)
        result.marker_removed = True
    except Exception as exc:
        result.warnings.append(f"image-marker-remove-failed:{original.name}:{_short_error(str(exc))}")
    return result


def prepare_feishu_image(image_path: Path | str) -> Path:
    """Return an RGB PNG that Feishu's media API tends to accept reliably."""
    path = Path(image_path)
    try:
        from PIL import Image
    except Exception:
        return path
    if not path.exists() or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return path
    safe_dir = path.parent / "feishu_safe"
    safe_path = safe_dir / f"{path.stem}.png"
    try:
        source_mtime = path.stat().st_mtime
        if safe_path.exists() and safe_path.stat().st_mtime >= source_mtime:
            return safe_path
        safe_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as image:
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            background.alpha_composite(image)
            rgb = background.convert("RGB")
            rgb = _crop_near_white_border(rgb)
            max_side = max(rgb.size)
            if max_side > 2200:
                scale = 2200 / max_side
                rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))), Image.LANCZOS)
            rgb.save(safe_path, format="PNG", optimize=True)
        return safe_path
    except Exception:
        return path


def image_display_width(image_path: Path | str) -> int:
    return image_display_size(image_path)[0]


def image_display_size(image_path: Path | str) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            source_width, source_height = image.size
    except Exception:
        return 640, 0
    if source_width <= 0 or source_height <= 0:
        return 640, 0
    ratio = source_width / source_height
    if ratio >= 3.2:
        width = 720
    elif ratio >= 2.3:
        width = 680
    elif ratio <= 0.75:
        width = 440
    elif ratio <= 1.15:
        width = 560
    else:
        width = 640
    height = max(1, round(width * source_height / source_width))
    return width, height


def _crop_near_white_border(image):
    try:
        from PIL import Image
        from PIL import ImageChops

        background = Image.new(image.mode, image.size, (255, 255, 255))
        diff = ImageChops.difference(image, background).convert("L")
        mask = diff.point(lambda value: 255 if value > 14 else 0)
        box = mask.getbbox()
        if not box:
            return image
        left, top, right, bottom = box
        pad = 18
        left = max(0, left - pad)
        top = max(0, top - pad)
        right = min(image.width, right + pad)
        bottom = min(image.height, bottom + pad)
        crop_area = (right - left) * (bottom - top)
        original_area = image.width * image.height
        if crop_area >= original_area * 0.92:
            return image
        if right - left >= image.width * 0.96 and bottom - top >= image.height * 0.72:
            return image
        if right - left < 32 or bottom - top < 32:
            return image
        return image.crop((left, top, right, bottom))
    except Exception:
        return image


def _short_error(text: str, limit: int = 260) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."
