from pathlib import Path

from PIL import Image
import pytest

from paloma_data.media_processing import ARTWORK_VARIANTS, render_artwork_variants


def test_render_artwork_variants_are_fixed_size_content_addressed_jpegs(tmp_path: Path):
    source = tmp_path / "wide source.png"
    Image.new("RGBA", (2_000, 1_100), (30, 80, 60, 180)).save(source)

    variants = render_artwork_variants(
        source,
        tmp_path / "rendered",
        filename_prefix="venue/unsafe name",
    )

    assert [item.variant for item in variants] == [spec.name for spec in ARTWORK_VARIANTS]
    for item, spec in zip(variants, ARTWORK_VARIANTS, strict=True):
        assert (item.width, item.height) == (spec.width, spec.height)
        assert item.mime_type == "image/jpeg"
        assert item.path.name.startswith(f"venue-unsafe-name-{spec.name}-")
        assert item.path.name.endswith(".jpg")
        assert item.byte_size == item.path.stat().st_size
        assert len(item.sha256) == 64
        with Image.open(item.path) as rendered:
            assert rendered.mode == "RGB"
            assert rendered.size == (spec.width, spec.height)


def test_render_artwork_variants_rejects_tiny_inputs(tmp_path: Path):
    source = tmp_path / "tiny.jpg"
    Image.new("RGB", (300, 200), "black").save(source)

    with pytest.raises(ValueError, match="at least 640x400"):
        render_artwork_variants(source, tmp_path / "rendered", filename_prefix="tiny")
