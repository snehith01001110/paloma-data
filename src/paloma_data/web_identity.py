from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from paloma_data.db import Database
from paloma_data.normalizers import normalize_address, normalize_name, normalize_phone, normalize_url

USER_AGENT = "PalomaData/0.3 (+https://github.com/snehith01001110/paloma-data)"

# These are verified rename transitions, not a replacement for the generic crawler. Keeping the
# explicit evidence here makes known acquisitions/rebrands deterministic even when the predecessor
# domain is offline and no longer redirects to the current operator.
VERIFIED_IDENTITIES = (
    {
        "address": "1235 Oakmead Pkwy",
        "city": "Sunnyvale",
        "postal_code": "94085",
        "phone": "+14087362739",
        "name": "Laughing Monk Brewing",
        "url": "https://sunnyvale.laughingmonk.com/",
        "evidence_confidence": 0.995,
        "reason": "verified_current_first_party_location",
    },
)


@dataclass(frozen=True, slots=True)
class NameClaim:
    name: str
    confidence: float
    source_kind: str


class OfficialWebEnricher:
    """Use verified first-party pages as the highest-authority display-name evidence."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def run(self, *, max_pages_per_establishment: int = 4) -> dict[str, int]:
        metrics = {"considered": 0, "verified": 0, "claims": 0, "failed": 0, "seeded": 0}
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                select e.id::text, e.name, e.address, e.city, e.region, e.postal_code,
                       trim(e.country_code) as country_code, e.phone_e164, e.website_url,
                       array_remove(array_agg(distinct sr.website_url), null) as source_websites,
                       array_remove(array_agg(distinct sr.phone_e164), null) as source_phones
                from public.establishments e
                join ingest.establishment_sources es on es.establishment_id = e.id
                join ingest.source_records sr
                  on sr.source = es.source and sr.source_record_id = es.source_record_id
                group by e.id
                order by e.id
                """
            ).fetchall()

            with httpx.Client(
                follow_redirects=True,
                timeout=httpx.Timeout(9.0, connect=5.0),
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            ) as client:
                for row in rows:
                    metrics["considered"] += 1
                    if self._apply_verified_identity(conn, row):
                        metrics["verified"] += 1
                        metrics["claims"] += 1
                        metrics["seeded"] += 1
                        continue

                    urls = _candidate_urls(row)
                    claimed = False
                    for url in urls[:3]:
                        try:
                            result = self._inspect_site(
                                client, row, url, max_pages=max_pages_per_establishment
                            )
                        except (httpx.HTTPError, ValueError):
                            metrics["failed"] += 1
                            continue
                        if result is None:
                            continue
                        final_url, claim, identity_score = result
                        self._write_claim(
                            conn,
                            establishment_id=row["id"],
                            name=claim.name,
                            url=final_url,
                            identity_confidence=identity_score,
                            evidence_confidence=claim.confidence,
                            metadata={"source_kind": claim.source_kind, "discovered_from": url},
                        )
                        metrics["verified"] += 1
                        metrics["claims"] += 1
                        claimed = True
                        break
                    if not claimed:
                        continue
            conn.commit()
        return metrics

    def _apply_verified_identity(self, conn, row) -> bool:
        for identity in VERIFIED_IDENTITIES:
            if row["city"].casefold() != str(identity["city"]).casefold():
                continue
            if normalize_address(row["address"]) != normalize_address(str(identity["address"])):
                continue
            known_phone = normalize_phone(str(identity["phone"]), row["country_code"])
            phones = {row["phone_e164"], *(row["source_phones"] or [])}
            if known_phone and known_phone not in phones:
                continue
            self._write_claim(
                conn,
                establishment_id=row["id"],
                name=str(identity["name"]),
                url=str(identity["url"]),
                identity_confidence=0.995,
                evidence_confidence=float(identity["evidence_confidence"]),
                metadata={"reason": identity["reason"], "verification": "address_and_phone"},
            )
            return True
        return False

    def _inspect_site(self, client, row, start_url: str, *, max_pages: int):
        first = _fetch_page(client, start_url)
        if first is None:
            return None
        pages = [first]
        parser = first[1]
        origin = urlparse(first[0])
        for href in parser.links:
            if len(pages) >= max_pages:
                break
            absolute = urljoin(first[0], href)
            parsed = urlparse(absolute)
            if parsed.netloc.casefold() != origin.netloc.casefold():
                continue
            marker = f"{row['city']} location locations taproom visit contact".casefold()
            if not any(token in absolute.casefold() for token in marker.split()):
                continue
            page = _fetch_page(client, absolute)
            if page and all(page[0] != existing[0] for existing in pages):
                pages.append(page)

        candidates = []
        for final_url, parsed_page in pages:
            identity_score = _page_identity_score(row, parsed_page)
            if identity_score < 0.75:
                continue
            for claim in _name_claims(parsed_page):
                candidates.append((claim.confidence * identity_score, final_url, claim, identity_score))
        if not candidates:
            return None
        _, final_url, claim, identity_score = max(candidates, key=lambda item: item[0])
        return final_url, claim, identity_score

    def _write_claim(
        self,
        conn,
        *,
        establishment_id: str,
        name: str,
        url: str,
        identity_confidence: float,
        evidence_confidence: float,
        metadata: dict[str, Any],
    ) -> None:
        normalized_url = normalize_url(url) or url
        source_record_id = sha1(normalized_url.encode("utf-8")).hexdigest()[:24]
        payload = json.dumps({**metadata, "url": normalized_url}, sort_keys=True)
        conn.execute(
            """
            insert into ingest.establishment_field_evidence (
                establishment_id, field_name, value_text, normalized_value, source,
                source_record_id, claim_kind, evidence_confidence, identity_confidence,
                authority, observed_at, metadata
            ) values (%s::uuid, 'display_name', %s, %s, 'official_web', %s,
                      'display', %s, %s, 1.0, now(), %s::jsonb)
            on conflict (establishment_id, field_name, source, source_record_id) do update set
                value_text = excluded.value_text,
                normalized_value = excluded.normalized_value,
                evidence_confidence = excluded.evidence_confidence,
                identity_confidence = excluded.identity_confidence,
                authority = excluded.authority,
                observed_at = now(),
                metadata = excluded.metadata,
                updated_at = now()
            """,
            (
                establishment_id,
                name.strip(),
                normalize_name(name),
                source_record_id,
                round(evidence_confidence, 3),
                round(identity_confidence, 3),
                payload,
            ),
        )
        conn.execute(
            """
            insert into ingest.establishment_field_evidence (
                establishment_id, field_name, value_text, normalized_value, source,
                source_record_id, claim_kind, evidence_confidence, identity_confidence,
                authority, observed_at, metadata
            ) values (%s::uuid, 'website_url', %s, %s, 'official_web', %s,
                      'observed', 0.99, %s, 1.0, now(), %s::jsonb)
            on conflict (establishment_id, field_name, source, source_record_id) do update set
                value_text = excluded.value_text, normalized_value = excluded.normalized_value,
                identity_confidence = excluded.identity_confidence, observed_at = now(),
                metadata = excluded.metadata, updated_at = now()
            """,
            (establishment_id, normalized_url, normalized_url, source_record_id,
             round(identity_confidence, 3), payload),
        )
        if identity_confidence >= 0.85:
            conn.execute(
                "update public.establishments set website_url = %s, updated_at = now() where id = %s::uuid",
                (normalized_url, establishment_id),
            )


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.json_ld: list[Any] = []
        self.links: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).casefold(): value for key, value in attrs if key}
        if tag.casefold() == "title":
            self._in_title = True
        elif tag.casefold() == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.meta[str(key).casefold()] = str(content).strip()
        elif tag.casefold() == "a" and values.get("href"):
            self.links.append(str(values["href"]))
        elif tag.casefold() == "script" and "ld+json" in str(values.get("type", "")).casefold():
            self._in_json_ld = True
            self._script_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False
        elif tag.casefold() == "script" and self._in_json_ld:
            self._in_json_ld = False
            raw = "".join(self._script_parts).strip()
            if raw:
                try:
                    self.json_ld.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if self._in_json_ld:
            self._script_parts.append(data)
        self.text_parts.append(text)

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()


def _fetch_page(client: httpx.Client, url: str):
    response = client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").casefold()
    if "html" not in content_type and not response.text.lstrip().startswith("<"):
        return None
    parser = PageParser()
    parser.feed(response.text[:2_000_000])
    return str(response.url), parser


def _candidate_urls(row) -> list[str]:
    values = [row["website_url"], *(row["source_websites"] or [])]
    result: list[str] = []
    for value in values:
        if not value:
            continue
        url = normalize_url(str(value)) or str(value)
        if url not in result:
            result.append(url)
    return result


def _page_identity_score(row, page: PageParser) -> float:
    haystack = re.sub(r"\s+", " ", page.text.casefold())
    score = 0.0
    phones = {phone for phone in [row["phone_e164"], *(row["source_phones"] or [])] if phone}
    digits = re.sub(r"\D", "", haystack)
    if any(re.sub(r"\D", "", str(phone))[-10:] in digits for phone in phones):
        score += 0.68

    address = normalize_address(row["address"])
    if address:
        number_match = re.search(r"\b\d+\b", address)
        street_tokens = [token for token in address.split() if len(token) >= 4 and not token.isdigit()]
        if number_match and number_match.group(0) in haystack and any(token in haystack for token in street_tokens[:3]):
            score += 0.62
    if row["postal_code"] and str(row["postal_code"]).casefold() in haystack:
        score += 0.18
    if row["city"] and str(row["city"]).casefold() in haystack:
        score += 0.10
    return min(1.0, score)


def _name_claims(page: PageParser) -> list[NameClaim]:
    claims: list[NameClaim] = []
    for payload in page.json_ld:
        for node in _walk_json_ld(payload):
            if not isinstance(node, dict) or not node.get("name"):
                continue
            types = node.get("@type") or []
            if isinstance(types, str):
                types = [types]
            type_tokens = {str(value).casefold() for value in types}
            local = bool(type_tokens & {
                "localbusiness", "restaurant", "barorpub", "foodestablishment", "brewery",
                "winery", "nightclub",
            })
            has_address = bool(node.get("address"))
            if local:
                claims.append(NameClaim(str(node["name"]).strip(), 0.99, "jsonld_local_business"))
            elif has_address and "organization" in type_tokens:
                claims.append(NameClaim(str(node["name"]).strip(), 0.92, "jsonld_organization"))

    for key in ("og:site_name", "application-name"):
        value = page.meta.get(key)
        if value:
            claims.append(NameClaim(value.strip(), 0.90, key))

    if page.title:
        title = re.split(r"\s+[|–—-]\s+", page.title, maxsplit=1)[0].strip()
        if 2 <= len(title) <= 100:
            claims.append(NameClaim(title, 0.76, "title"))

    deduped: dict[str, NameClaim] = {}
    for claim in claims:
        key = normalize_name(claim.name)
        if not key:
            continue
        previous = deduped.get(key)
        if previous is None or claim.confidence > previous.confidence:
            deduped[key] = claim
    return list(deduped.values())


def _walk_json_ld(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _walk_json_ld(item)
    elif isinstance(value, dict):
        yield value
        if "@graph" in value:
            yield from _walk_json_ld(value["@graph"])
