from __future__ import annotations

from collections.abc import Iterator
import csv
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO, TextIOWrapper
import re
from typing import Any
from urllib.parse import urljoin, urlsplit
from zipfile import ZipFile

import httpx

from paloma_data.models import SourceRecord
from paloma_data.taxonomy import classify_abc


class _ExportLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.csv_href: str | None = None
        self.fixed_href: str | None = None
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
        if tag.lower() != "a" or not self._current_href:
            return
        text = " ".join(self._text).strip().casefold()
        href = self._current_href
        href_lower = href.casefold()
        if "download data as csv" in text or ("csv" in href_lower and ".zip" in href_lower):
            self.csv_href = href
        elif "download fixed-width data" in text or "m_tape460" in href_lower:
            self.fixed_href = href
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
            headers={
                "User-Agent": "paloma-data/0.2 (+https://github.com/snehith01001110/paloma-data)",
                "Accept": "application/json,text/html,application/zip,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            export_url = self._discover_export_url(client)
            response = client.get(export_url)
            response.raise_for_status()
            yield from self._parse_zip(response.content)

    def incremental(self, cursor: str | None = None) -> Iterator[SourceRecord]:
        # ABC refreshes the export each business day. Stable IDs + payload hashes make the
        # snapshot transport an idempotent incremental reconciliation at record level.
        yield from self.backfill()

    def _discover_export_url(self, client: httpx.Client) -> str:
        errors: list[str] = []

        # ABC runs WordPress. The REST representation contains the same official export links as
        # the human-facing page but avoids depending on HTML-page bot protection in cloud runners.
        parts = urlsplit(self.reports_url)
        api_url = f"{parts.scheme}://{parts.netloc}/wp-json/wp/v2/pages"
        try:
            response = client.get(
                api_url,
                params={"slug": "licensing-reports", "_fields": "content"},
            )
            response.raise_for_status()
            pages = response.json()
            if pages:
                rendered = pages[0].get("content", {}).get("rendered", "")
                found = self._export_href_from_html(rendered)
                if found:
                    return urljoin(self.reports_url, found)
            errors.append("wp-json:no_export_link")
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"wp-json:{type(exc).__name__}:{exc}")

        # Keep the public report page as a compatibility fallback if ABC changes WordPress APIs.
        try:
            response = client.get(self.reports_url)
            response.raise_for_status()
            found = self._export_href_from_html(response.text)
            if found:
                return urljoin(self.reports_url, found)
            errors.append("reports-page:no_export_link")
        except httpx.HTTPError as exc:
            errors.append(f"reports-page:{type(exc).__name__}:{exc}")

        raise RuntimeError("Could not discover California ABC official data export; " + " | ".join(errors))

    def _export_href_from_html(self, html: str) -> str | None:
        parser = _ExportLinkParser()
        parser.feed(html)
        if parser.csv_href:
            return parser.csv_href
        if parser.fixed_href:
            return parser.fixed_href

        # Resilient fallback for minor label changes in ABC's markup.
        zip_hrefs = re.findall(r'href=["\']([^"\']+\.zip(?:\?[^"\']*)?)["\']', html, re.I)
        csv_hrefs = [href for href in zip_hrefs if "csv" in href.casefold()]
        if csv_hrefs:
            return csv_hrefs[0]
        fixed_hrefs = [href for href in zip_hrefs if "m_tape460" in href.casefold()]
        return fixed_hrefs[0] if fixed_hrefs else None

    def _parse_zip(self, payload: bytes) -> Iterator[SourceRecord]:
        with ZipFile(BytesIO(payload)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if csv_names:
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
                return

            # ABC documents a second official export as 460-character fixed-width records.
            candidates = [name for name in archive.namelist() if not name.endswith("/")]
            if not candidates:
                raise RuntimeError("ABC ZIP contained no data file")
            name = max(candidates, key=lambda n: archive.getinfo(n).file_size)
            with archive.open(name) as raw:
                text = raw.read().decode("latin-1", errors="replace")
            for row in self._fixed_width_rows(text):
                record = self._to_record(row)
                if record is not None:
                    yield record

    def _fixed_width_rows(self, text: str) -> Iterator[dict[str, str]]:
        fields = (
            ("License Type", 0, 2),
            ("File Number", 2, 10),
            ("License or Application", 10, 13),
            ("Type Status", 13, 21),
            ("Type Original Issue Dates", 21, 32),
            ("Expiration Dates", 32, 43),
            ("Fee Codes", 43, 51),
            ("Duplicate Counts", 51, 54),
            ("Master Indicator", 54, 55),
            ("Term in Number of Months", 55, 57),
            ("Geo Code", 57, 61),
            ("District/Office Code", 61, 63),
            ("Primary Name", 63, 113),
            ("Premise Street Address 1", 113, 163),
            ("Premise Street Address 2", 163, 213),
            ("Premise City", 213, 238),
            ("Premise State", 238, 240),
            ("Premise Zip", 240, 250),
            ("DBA Name", 250, 300),
            ("Mail Street Address 1", 300, 350),
            ("Mail Street Address 2", 350, 400),
            ("Mail City", 400, 425),
            ("Mail State", 425, 427),
            ("Mail Zip", 427, 437),
            ("Premise County", 437, 453),
            ("Premise Census Tract Number", 453, 460),
        )
        # ABC's file may be newline-delimited or block-oriented. Remove line endings and consume
        # exact 460-character records so either representation parses identically.
        compact = text.replace("\r", "").replace("\n", "")
        for start in range(0, len(compact) - 459, 460):
            line = compact[start : start + 460]
            if not line.strip():
                continue
            yield {label: line[a:b].strip() for label, a, b in fields}

    def _to_record(self, row: dict[str, Any]) -> SourceRecord | None:
        normalized = {_header(k): v for k, v in row.items() if k is not None}
        license_number = _pick(
            normalized,
            "file_number",
            "license_number",
            "licensenumber",
            "license_no",
            "lic_no",
        )
        license_type = _pick(normalized, "license_type", "licensetype", "type")
        license_or_application = _pick(
            normalized, "license_or_application", "license_application", "lic_or_app"
        )
        status_raw = _pick(
            normalized,
            "type_status",
            "status",
            "license_status",
            "lic_status",
        ) or ""

        name = _pick(
            normalized,
            "dba_name",
            "business_name",
            "premises_name",
            "dba",
            "primary_name",
        )
        street_1 = _pick(
            normalized,
            "premise_street_address_1",
            "premises_street_address_1",
            "premises_address",
            "address",
            "prem_addr_1",
            "premisesaddress",
        )
        street_2 = _pick(
            normalized,
            "premise_street_address_2",
            "premises_street_address_2",
            "prem_addr_2",
        )
        address = _join_address(street_1, street_2)
        city = _pick(normalized, "premise_city", "premises_city", "city", "prem_city")
        region = _pick(
            normalized, "premise_state", "premises_state", "state", "prem_state"
        ) or "CA"
        postal = _pick(normalized, "premise_zip", "premises_zip", "zip", "zipcode", "prem_zip")

        if not license_number or not license_type or not name or not address or not city:
            return None

        classification = classify_abc(name, license_type)
        if not classification.eligible:
            return None

        status = _canonical_status(status_raw, license_or_application)
        source_id = f"{license_number}:{license_type}"
        permitted = {
            "file_number": license_number,
            "license_type": license_type,
            "license_or_application": license_or_application,
            "type_status": status_raw,
            "primary_name": _pick(
                normalized, "primary_name", "primary_owner", "owner_name", "owner"
            ),
            "district_code": _pick(
                normalized, "district_office_code", "district_code", "district"
            ),
            "geo_code": _pick(normalized, "geo_code", "geocode"),
            "expiration_date": _pick(
                normalized,
                "expiration_dates",
                "expiration_date",
                "expir_date",
                "expiration",
            ),
            "premise_county": _pick(normalized, "premise_county", "premises_county"),
        }
        updated_at = _parse_date(
            _pick(
                normalized,
                "type_original_issue_dates",
                "type_original_issue_date",
                "original_issue_date",
                "issue_date",
                "status_date",
                "effective_date",
            )
        )

        return SourceRecord(
            source=self.source,
            source_record_id=source_id,
            name=name.strip(),
            address=address,
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


def _join_address(street_1: str | None, street_2: str | None) -> str | None:
    parts = [part.strip() for part in (street_1, street_2) if part and part.strip()]
    return " ".join(parts) if parts else None


def _canonical_status(value: str, license_or_application: str | None = None) -> str:
    text = value.casefold()
    record_kind = (license_or_application or "").casefold()
    if any(token in text for token in ("cancel", "revok", "surrender", "closed", "inactive")):
        return "closed"
    if "pend" in text or "app" in record_kind:
        return "pending"
    return "open"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(candidate[:11], fmt)
        except ValueError:
            continue
    return None
