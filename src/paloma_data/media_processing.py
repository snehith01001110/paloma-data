from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path

from PIL import Image, ImageOps


@dataclass(frozen=True, slots=True)
class ArtworkVariantSpec:
    name: str
    width: int
    height: int
    quality: int


ARTWORK_VARIANTS = (
    ArtworkVariantSpec("hero", 1_600, 1_000, 84),
    ArtworkVariantSpec("card", 960, 600, 82),
    ArtworkVariantSpec("thumbnail", 320, 200, 78),
)


@dataclass(frozen=True, slots=True)
class RenderedArtworkVariant:
    variant: str
    path: Path
    mime_type: str
    width: int
    height: int
    byte_size: int
    sha256: str

    def manifest(self) -> dict[str, str | int]:
        value = asdict(self)
        value["path"] = str(self.path)
        return value


@dataclass(frozen=True, slots=True)
class SourceImageSummary:
    path: Path
    mime_type: str
    width: int
    height: int
    byte_size: int
    sha256: str


def inspect_source_image(path: Path) -> SourceImageSummary:
    if not path.is_file():
        raise ValueError(f"Image does not exist: {path}")
    try:
        with Image.open(path) as image:
            image.verify()
            width, height = image.size
            mime_type = Image.MIME.get(image.format or "")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Image is not supported: {path}") from exc
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError(f"Unsupported source image type: {mime_type or 'unknown'}")
    return SourceImageSummary(
        path=path,
        mime_type=mime_type,
        width=width,
        height=height,
        byte_size=path.stat().st_size,
        sha256=_sha256_file(path),
    )


def render_artwork_variants(
    source_path: Path,
    output_directory: Path,
    *,
    filename_prefix: str,
) -> list[RenderedArtworkVariant]:
    """Create deterministic 8:5 JPEG variants with metadata stripped.

    The same geometry is reserved before and after an image loads in the app. Fixed variant
    dimensions keep remote source aspect ratios from changing a card or detail sheet's layout,
    while content-addressed filenames allow year-long immutable CDN caching.
    """
    if not source_path.is_file():
        raise ValueError(f"Artwork source does not exist: {source_path}")
    clean_prefix = _filename_component(filename_prefix)
    output_directory.mkdir(parents=True, exist_ok=True)

    try:
        with Image.open(source_path) as opened:
            source = ImageOps.exif_transpose(opened)
            source.load()
            rgb_source = _flatten_to_rgb(source)
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Artwork source is not a supported image: {source_path}") from exc

    if rgb_source.width < 640 or rgb_source.height < 400:
        raise ValueError("Artwork source must be at least 640x400 pixels")

    rendered: list[RenderedArtworkVariant] = []
    for spec in ARTWORK_VARIANTS:
        image = ImageOps.fit(
            rgb_source,
            (spec.width, spec.height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        temporary_path = output_directory / f".{clean_prefix}-{spec.name}.jpg"
        image.save(
            temporary_path,
            format="JPEG",
            quality=spec.quality,
            optimize=True,
            progressive=True,
            subsampling="4:2:0",
        )
        digest = _sha256_file(temporary_path)
        final_path = output_directory / f"{clean_prefix}-{spec.name}-{digest[:16]}.jpg"
        temporary_path.replace(final_path)
        rendered.append(
            RenderedArtworkVariant(
                variant=spec.name,
                path=final_path,
                mime_type="image/jpeg",
                width=spec.width,
                height=spec.height,
                byte_size=final_path.stat().st_size,
                sha256=digest,
            )
        )
    return rendered


def source_image_sha256(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"Image does not exist: {path}")
    return _sha256_file(path)


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image.copy()
    if "A" in image.getbands():
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (24, 29, 26, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _filename_component(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-_" else "-" for character in value)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    if not cleaned:
        raise ValueError("filename_prefix must contain a letter or number")
    return cleaned[:160]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()
