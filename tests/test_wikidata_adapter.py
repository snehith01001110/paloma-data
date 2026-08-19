from paloma_data.adapters.wikidata import WikidataAdapter, _query


def _value(value):
    return {"type": "literal", "value": value}


def test_wikidata_row_becomes_cc0_field_scoped_evidence():
    record = WikidataAdapter("-123.2,36.8,-121.1,38.9")._to_record(
        {
            "item": _value("http://www.wikidata.org/entity/Q123"),
            "itemLabel": _value("Example Cocktail Bar"),
            "coord": _value("Point(-122.4194 37.7749)"),
            "streetAddress": _value("123 Valencia St"),
            "adminLabel": _value("San Francisco"),
            "phone": _value("+1 415 555 1212"),
            "website": _value("https://example.com"),
            "modified": _value("2026-08-18T12:00:00Z"),
        }
    )

    assert record is not None
    assert record.source_record_id == "Q123"
    assert record.data_license == "CC0-1.0"
    assert record.latitude == 37.7749
    assert record.longitude == -122.4194
    assert record.field_provenance["website_url"]["license_ids"] == ["CC0-1.0"]


def test_wikidata_bbox_query_uses_current_wikibase_corner_parameters():
    query = _query(-123.2, 36.8, -121.1, 38.9)
    assert "wikibase:cornerSouthWest" in query
    assert "wikibase:cornerNorthEast" in query
    assert query.count("?item wdt:P625 ?coord") == 1
