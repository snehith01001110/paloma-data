from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
import math
import re
from typing import Any

import httpx


MAPILLARY_GRAPH_URL = "https://graph.mapillary.com"
WIKIMEDIA_API_URL = "https://commons.wikimedia.org/w/api.php"
COMMONS_MIN_NAME_MATCH_SCORE = 0.8
COMMONS_MAX_GEOTAG_DISTANCE_METERS = 5_000
CC_BY_SA_4_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
MAPILLARY_TERMS_URL = (
    "https://help.mapillary.com/hc/en-us/articles/115001770409-"
    "CC-BY-SA-license-for-open-data"
)


@dataclass(frozen=True, slots=True)
class MediaRights:
    license_id: str
    license_url: str
    terms_url: str
    commercial_use_allowed: bool
    derivatives_allowed: bool
    raw_persistence_allowed: bool
    attribution_required: bool
    share_alike_required: bool

    @property
    def generation_allowed(self) -> bool:
        return (
            self.commercial_use_allowed
            and self.derivatives_allowed
            and self.raw_persistence_allowed
        )


MAPILLARY_RIGHTS = MediaRights(
    license_id="CC-BY-SA-4.0",
    license_url=CC_BY_SA_4_URL,
    terms_url=MAPILLARY_TERMS_URL,
    commercial_use_allowed=True,
    derivatives_allowed=True,
    raw_persistence_allowed=True,
    attribution_required=True,
    share_alike_required=True,
)


@dataclass(frozen=True, slots=True)
class EstablishmentMediaTarget:
    establishment_id: str
    name: str
    address: str
    city: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class OpenMediaCandidate:
    provider: str
    source_asset_id: str
    source_page_url: str
    preview_url: str
    creator: str | None
    attribution_text: str
    captured_at: datetime | None
    latitude: float | None
    longitude: float | None
    distance_meters: float | None
    camera_heading_degrees: float | None
    bearing_to_target_degrees: float | None
    heading_delta_degrees: float | None
    width: int | None
    height: int | None
    review_priority: float
    review_flags: tuple[str, ...]
    rights: MediaRights

    @property
    def generation_allowed_after_visual_review(self) -> bool:
        return self.rights.generation_allowed

    def manifest(self, *, include_ephemeral_preview: bool = True) -> dict[str, Any]:
        value = asdict(self)
        value["captured_at"] = self.captured_at.isoformat() if self.captured_at else None
        value["generation_allowed_after_visual_review"] = (
            self.generation_allowed_after_visual_review
        )
        if not include_ephemeral_preview:
            value.pop("preview_url", None)
        return value


class MapillaryMediaClient:
    """Discover open street imagery without treating proximity as a venue match.

    Mapillary thumbnail URLs are signed delivery URLs and are intentionally exposed only as
    ephemeral review inputs. The durable identifier is ``source_asset_id`` and the stable
    provenance link is ``source_page_url``.
    """

    def __init__(
        self,
        access_token: str,
        *,
        client: httpx.Client | None = None,
        endpoint: str = MAPILLARY_GRAPH_URL,
    ) -> None:
        if not access_token.strip():
            raise ValueError("A Mapillary access token is required")
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(45.0, connect=10.0),
            headers={"User-Agent": "PalomaData/0.5 media-discovery"},
        )
        self.access_token = access_token.strip()
        self.endpoint = endpoint.rstrip("/")

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> MapillaryMediaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search(
        self,
        target: EstablishmentMediaTarget,
        *,
        radius_meters: float = 180,
        limit: int = 20,
        now: datetime | None = None,
    ) -> list[OpenMediaCandidate]:
        if not 25 <= radius_meters <= 1_000:
            raise ValueError("radius_meters must be between 25 and 1000")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        response = self.client.get(
            f"{self.endpoint}/images",
            headers={"Authorization": f"OAuth {self.access_token}"},
            params={
                "bbox": _bbox(target.latitude, target.longitude, radius_meters),
                "limit": 2_000,
                "fields": ",".join(
                    (
                        "id",
                        "computed_geometry",
                        "captured_at",
                        "computed_compass_angle",
                        "creator",
                        "camera_type",
                        "is_pano",
                        "thumb_2048_url",
                        "width",
                        "height",
                    )
                ),
            },
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ValueError("Mapillary response data must be an array")

        candidates = [
            candidate
            for row in rows
            if isinstance(row, dict)
            and (
                candidate := _mapillary_candidate(
                    row,
                    target,
                    radius_meters=radius_meters,
                    now=now or datetime.now(timezone.utc),
                )
            )
            is not None
        ]
        candidates.sort(key=lambda item: (-item.review_priority, item.source_asset_id))
        return candidates[:limit]


class WikimediaCommonsMediaClient:
    """Find name-matched Commons files and retain their file-specific license metadata."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        endpoint: str = WIKIMEDIA_API_URL,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(45.0, connect=10.0),
            headers={
                "User-Agent": (
                    "PalomaData/0.5 media-discovery "
                    "(https://github.com/snehith01001110/paloma-data)"
                )
            },
        )
        self.endpoint = endpoint

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> WikimediaCommonsMediaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search(
        self,
        target: EstablishmentMediaTarget,
        *,
        limit: int = 20,
    ) -> list[OpenMediaCandidate]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        response = self.client.get(
            self.endpoint,
            params={
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "generator": "search",
                "gsrsearch": f'"{target.name}" filetype:bitmap',
                "gsrnamespace": 6,
                "gsrlimit": min(50, max(limit * 3, 10)),
                "prop": "imageinfo",
                "iiprop": "url|extmetadata|size|timestamp",
                "iiurlwidth": 2_048,
            },
        )
        response.raise_for_status()
        payload = response.json()
        pages = payload.get("query", {}).get("pages", []) if isinstance(payload, dict) else []
        if not isinstance(pages, list):
            raise ValueError("Wikimedia response pages must be an array")
        candidates = [
            candidate
            for page in pages
            if isinstance(page, dict)
            and (candidate := _commons_candidate(page, target)) is not None
        ]
        candidates.sort(key=lambda item: (-item.review_priority, item.source_asset_id))
        return candidates[:limit]


def _mapillary_candidate(
    row: dict[str, Any],
    target: EstablishmentMediaTarget,
    *,
    radius_meters: float,
    now: datetime,
) -> OpenMediaCandidate | None:
    asset_id = str(row.get("id") or "").strip()
    preview_url = str(row.get("thumb_2048_url") or "").strip()
    coordinates = row.get("computed_geometry", {}).get("coordinates")
    if (
        not asset_id
        or not preview_url.startswith("https://")
        or not isinstance(coordinates, list)
        or len(coordinates) < 2
    ):
        return None
    try:
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    distance, bearing = _distance_and_bearing(
        latitude,
        longitude,
        target.latitude,
        target.longitude,
    )
    if distance > radius_meters:
        return None
    heading = _optional_float(row.get("computed_compass_angle"))
    if heading is not None:
        heading %= 360
    delta = _angle_delta(heading, bearing) if heading is not None else None
    captured_at = _milliseconds_timestamp(row.get("captured_at"))
    creator_value = row.get("creator")
    creator = None
    if isinstance(creator_value, dict):
        creator = str(creator_value.get("username") or "").strip() or None

    flags = ["visual_identity_review_required"]
    if heading is None:
        flags.append("camera_heading_missing")
    elif delta is not None and delta > 65:
        flags.append("target_likely_out_of_frame")
    if captured_at is None:
        flags.append("capture_date_missing")
    elif (now - captured_at).days > 365 * 5:
        flags.append("capture_older_than_five_years")
    if distance < 8:
        flags.append("coordinates_may_be_inside_target")
    if row.get("camera_type") not in (None, "perspective"):
        flags.append("nonstandard_camera_projection")

    priority = _street_review_priority(
        distance_meters=distance,
        heading_delta_degrees=delta,
        captured_at=captured_at,
        now=now,
    )
    attribution = "Mapillary contributors"
    if creator:
        attribution = f"Mapillary image by {creator}"
    return OpenMediaCandidate(
        provider="mapillary",
        source_asset_id=asset_id,
        source_page_url=f"https://www.mapillary.com/app/?pKey={asset_id}&focus=photo",
        preview_url=preview_url,
        creator=creator,
        attribution_text=f"{attribution}, CC BY-SA 4.0; modified by Paloma",
        captured_at=captured_at,
        latitude=latitude,
        longitude=longitude,
        distance_meters=round(distance, 2),
        camera_heading_degrees=round(heading, 2) if heading is not None else None,
        bearing_to_target_degrees=round(bearing, 2),
        heading_delta_degrees=round(delta, 2) if delta is not None else None,
        width=_optional_int(row.get("width")),
        height=_optional_int(row.get("height")),
        review_priority=round(priority, 4),
        review_flags=tuple(flags),
        rights=MAPILLARY_RIGHTS,
    )


def _commons_candidate(
    page: dict[str, Any],
    target: EstablishmentMediaTarget,
) -> OpenMediaCandidate | None:
    title = str(page.get("title") or "").strip()
    image_info = page.get("imageinfo")
    if not title or not isinstance(image_info, list) or not image_info:
        return None
    info = image_info[0]
    if not isinstance(info, dict):
        return None
    metadata = info.get("extmetadata")
    if not isinstance(metadata, dict):
        return None
    rights = _commons_rights(metadata)
    if rights is None or not rights.generation_allowed:
        return None
    match_score = _name_match_score(title, target.name)
    if match_score < COMMONS_MIN_NAME_MATCH_SCORE:
        return None
    preview_url = str(info.get("thumburl") or info.get("url") or "").strip()
    source_page_url = str(info.get("descriptionurl") or "").strip()
    if not preview_url.startswith("https://") or not source_page_url.startswith("https://"):
        return None

    creator = _clean_html(_metadata_value(metadata, "Artist")) or None
    credit = _clean_html(_metadata_value(metadata, "Credit"))
    attribution = credit or (f"Wikimedia Commons image by {creator}" if creator else title)
    captured_at = _iso_timestamp(
        _metadata_value(metadata, "DateTimeOriginal") or str(info.get("timestamp") or "")
    )
    latitude = _optional_float(_metadata_value(metadata, "GPSLatitude"))
    longitude = _optional_float(_metadata_value(metadata, "GPSLongitude"))
    distance = None
    if latitude is not None and longitude is not None:
        distance, _ = _distance_and_bearing(
            latitude,
            longitude,
            target.latitude,
            target.longitude,
        )
        if distance > COMMONS_MAX_GEOTAG_DISTANCE_METERS:
            return None

    flags = ["visual_identity_review_required"]
    if distance is None:
        flags.append("source_coordinates_missing")
    if rights.share_alike_required:
        flags.append("generated_derivative_must_remain_share_alike")

    return OpenMediaCandidate(
        provider="wikimedia_commons",
        source_asset_id=str(page.get("pageid") or title),
        source_page_url=source_page_url,
        preview_url=preview_url,
        creator=creator,
        attribution_text=(
            f"{attribution}, {rights.license_id}; modified by Paloma"
            if rights.attribution_required
            else f"{attribution}; modified by Paloma"
        ),
        captured_at=captured_at,
        latitude=latitude,
        longitude=longitude,
        distance_meters=round(distance, 2) if distance is not None else None,
        camera_heading_degrees=None,
        bearing_to_target_degrees=None,
        heading_delta_degrees=None,
        width=_optional_int(info.get("width")),
        height=_optional_int(info.get("height")),
        review_priority=round(match_score, 4),
        review_flags=tuple(flags),
        rights=rights,
    )


def _commons_rights(metadata: dict[str, Any]) -> MediaRights | None:
    raw_name = _clean_html(_metadata_value(metadata, "LicenseShortName"))
    raw_url = _metadata_value(metadata, "LicenseUrl").strip()
    folded = raw_name.casefold().replace("_", "-")
    if "noncommercial" in folded or "nc" in _license_tokens(folded):
        return None
    if "no derivatives" in folded or "nd" in _license_tokens(folded):
        return None

    public_domain = "public domain" in folded or folded in {"pd", "pdm"}
    cc0 = "cc0" in folded or "cc zero" in folded
    cc_by_sa = "cc by-sa" in folded or "cc-by-sa" in folded
    cc_by = not cc_by_sa and ("cc by" in folded or "cc-by" in folded)
    if not (public_domain or cc0 or cc_by_sa or cc_by):
        return None

    if not raw_url.startswith("https://"):
        if cc0:
            raw_url = "https://creativecommons.org/publicdomain/zero/1.0/"
        elif public_domain:
            raw_url = "https://creativecommons.org/publicdomain/mark/1.0/"
        else:
            return None
    license_id = (
        "CC0-1.0"
        if cc0
        else "Public-Domain"
        if public_domain
        else raw_name.replace(" ", "-").upper()
    )
    return MediaRights(
        license_id=license_id,
        license_url=raw_url,
        terms_url=raw_url,
        commercial_use_allowed=True,
        derivatives_allowed=True,
        raw_persistence_allowed=True,
        attribution_required=not (cc0 or public_domain),
        share_alike_required=cc_by_sa,
    )


def _street_review_priority(
    *,
    distance_meters: float,
    heading_delta_degrees: float | None,
    captured_at: datetime | None,
    now: datetime,
) -> float:
    # About 20-80 m is usually more useful than either a building-interior coordinate or a
    # distant road frame. This score only orders manual review; it never proves identity.
    distance_score = max(0.0, 1.0 - abs(distance_meters - 50.0) / 180.0)
    heading_score = (
        0.35
        if heading_delta_degrees is None
        else max(0.0, 1.0 - heading_delta_degrees / 90.0)
    )
    if captured_at is None:
        recency_score = 0.25
    else:
        age_years = max(0.0, (now - captured_at).days / 365.25)
        recency_score = max(0.0, 1.0 - age_years / 10.0)
    return 0.45 * heading_score + 0.35 * distance_score + 0.20 * recency_score


def _bbox(latitude: float, longitude: float, radius_meters: float) -> str:
    latitude_delta = radius_meters / 111_320.0
    longitude_scale = max(0.01, math.cos(math.radians(latitude)))
    longitude_delta = radius_meters / (111_320.0 * longitude_scale)
    return ",".join(
        f"{value:.7f}"
        for value in (
            longitude - longitude_delta,
            latitude - latitude_delta,
            longitude + longitude_delta,
            latitude + latitude_delta,
        )
    )


def _distance_and_bearing(
    source_latitude: float,
    source_longitude: float,
    target_latitude: float,
    target_longitude: float,
) -> tuple[float, float]:
    radius = 6_371_000.0
    source_phi = math.radians(source_latitude)
    target_phi = math.radians(target_latitude)
    delta_phi = math.radians(target_latitude - source_latitude)
    delta_lambda = math.radians(target_longitude - source_longitude)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(source_phi)
        * math.cos(target_phi)
        * math.sin(delta_lambda / 2) ** 2
    )
    distance = 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    y = math.sin(delta_lambda) * math.cos(target_phi)
    x = (
        math.cos(source_phi) * math.sin(target_phi)
        - math.sin(source_phi) * math.cos(target_phi) * math.cos(delta_lambda)
    )
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    return distance, bearing


def _angle_delta(first: float, second: float) -> float:
    return abs((first - second + 180) % 360 - 180)


def _milliseconds_timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value) / 1_000, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _iso_timestamp(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metadata_value(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, dict):
        return ""
    return str(value.get("value") or "")


def _clean_html(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", unescape(value))
    return " ".join(no_tags.split())


def _license_tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value))


def _name_match_score(title: str, name: str) -> float:
    def tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", value.casefold())
            if token not in {"file", "jpg", "jpeg", "png", "webp", "the", "and"}
        }

    expected = tokens(name)
    actual = tokens(title)
    if not expected:
        return 0.0
    overlap = len(expected & actual) / len(expected)
    return min(1.0, 0.2 + 0.8 * overlap)


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
