from __future__ import annotations

from collections.abc import Iterator
import csv
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO, TextIOWrapper
import re
from urllib.parse import urljoin
from zipfile import ZipFile
from typing import Any

import httpx

from paloma_data.models import SourceRecord
from paloma_data.taxonomy import classify_abc


class _CSVLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.href: str | None = None
        self._current_href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._current_href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            text = " ".join(self._text).strip().casefold()
            href = self._current_href
            if "download data as csv" in text or ("csv" in text and href.lower().endswith(".zip")):
                self.href = href
            self._current_href = None
            self._text = []


class CaliforniaABCAdapter:
    source = "ca_abc"

    def __init__(self, reports_url: str = "https://www.abc.ca.gov/licensing/licensing-reports/") -> None:
        self.reports_url = reports_url

    def backfill(self) -> Iterator[SourceRecord]:
        with httpx.Client(
            timeout=120.0,
            follow_redirects=True,
            headers={"User-Agent": "paloma-data/0.1"},
        ) as client:
            csv_zip_url = self._discover_csv_zip(client)
            response = client.get(csv_zip_url)
            response.raise_for_status()
            yield from self._parse_zip(response.content)

    def incremental(self, cursor: str | None = None) -> Iterator[SourceRecord]:
        # ABC publishes a fresh raw export every business day. Stable source IDs + payload hashes
        # make this an incremental reconciliation even though the transport is a snapshot.
        yield from self.backfill()

    def _discover_csv_zip(self, client: httpx.Client) -> str:
        response = client.get(self.reports_url)
        response.raise_for_status()
        parser = _CSVLinkParser()
        parser.feed(response.text)
        if not parser.href:
            # Fallback: ABC occasionally changes link text; choose a same-page ZIP href containing csv.
            candidates = re.findall(
                r'href=["\']([^"\']+\.zip(?:\?[^"\']*)?)["\']', response.text, re.I
            )
            csv_candidates = [href for href in candidates if "csv" in href.casefold()]
            if not csv_candidates:
                raise RuntimeError("Could not discover California ABC CSV data export URL")
            parser.href = csv_candidates[0]
        return urljoin(self.reports_url, parser.href)

    def _parse_zip(self, payload: bytes) -> Iterator[SourceRecord]:
        with ZipFile(BytesIO(payload)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not csv_names:
                raise RuntimeError("ABC ZIP contained no CSV file")
            # Prefer the largest CSV if the archive contains support/reference files.
            name = max(csv_names, key=lambda n: archive.getinfo(n).file_size)
            with (
                archive.open(name) as raw,
                TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="") as text,
            ):
                reader = csv.DictReader(text)
                for row in reader:
                    record = self._to_record(row)
                    if record is not None:
                        yield record

    def _to_record(self, row: dict[str, Any]) -> SourceRecord | None:
        normalized = {_header(k): v for k, v in row.items() if k is not None}
        license_number = _pick(normalized, "license_number", "licensenumber", "license_no", "lic_no")
        license_type = _pick(normalized, "license_type", "licensetype", "type")
        name = _pick(normalized, "dba_name", "business_name", "premises_name", "dba")
        address = _pick(
            normalized, "premises_address", "address", "prem_addr_1", "premisesaddress"
        )
        city = _pick(normalized, "premises_city", "city", "prem_city")
        region = _pick(normalized, "premises_state", "state", "prem_state") or "CA"
        postal = _pick(normalized, "premises_zip", "zip", "zipcode", "prem_zip")
        status_raw = _pick(normalized, "status", "license_status", "lic_status") or ""

        if not license_number or not license_type or not name or not address or not city:
            return None

        classification = classify_abc(name, license_type)
        if not classification.eligible:
            return None

        status = _canonical_status(status_raw)
        source_id = f"{license_number}:{license_type}"
        permitted = {
            "license_number": license_number,
            "license_type": license_type,
            "license_status": status_raw,
            "primary_owner": _pick(normalized, "primary_owner", "owner_name", "owner"),
            "district_code": _pick(normalized, "district_code", "district"),
            "geo_code": _pick(normalized, "geo_code", "geocode"),
            "expiration_date": _pick(
                normalized, "expiration_date", "expir_date", "expiration"
            ),
        }
        updated_at = _parse_date(
            _pick(normalized, "status_date", "effective_date", "original_issue_date", "issue_date")
        )

        return SourceRecord(
            source=self.source,
            source_record_id=source_id,
            name=name.strip(),
            address=address.strip(),
            city=city.strip(),
            region=region.strip(),
            postal_code=postal.strip() if postal else None,
            country_code="US",
            source_status=status,
            source_updated_at=updated_at,
            primary_type_slug=classification.primary_type_slug,
            classification_confidence=classification.confidence,
            category_evidence={"reason": classification.reason, "license_type": license_type},
            permitted_metadata=permitted,
        )


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _pick(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _canonical_status(value: str) -> str:
    text = value.casefold()
    if any(token in text for token in ("cancel", "revok", "surrender", "closed", "inactive")):
        return "closed"
    if "pending" in text:
        return "pending"
    return "open"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(value[:10], fmt)
        except ValueError:
            continue
    return None
