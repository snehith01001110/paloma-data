from paloma_data.evidence_ledger import _field_provenance, _policy_allows, _record_claims


def test_record_claims_uses_source_specific_status_semantics():
    base = {
        "source": "ca_abc",
        "name": "Legal Operator LLC",
        "normalized_name": "legal operator llc",
        "source_status": "open",
        "setting_slugs": [],
    }
    claims = list(_record_claims(base))
    assert {claim.field_name for claim in claims} == {"legal_name", "license_status"}


def test_overture_field_provenance_does_not_fall_back_when_property_data_exists():
    record = {
        "origin_keys": ["overture:unknown"],
        "data_license": "Overture-source-licenses",
        "field_provenance": {
            "website_url": {
                "origin_keys": ["foursquare"],
                "license_ids": ["Apache-2.0"],
                "source_items": [{"property": "/websites/0"}],
            }
        },
    }
    provenance = _field_provenance(record, "website_url")
    assert provenance["origin_keys"] == ["foursquare"]
    assert provenance["license_ids"] == ["Apache-2.0"]


def test_policy_gate_fails_closed_when_any_required_right_is_missing():
    policy = {
        "normalized_persistence_allowed": True,
        "source_derivation_allowed": True,
        "durable_storage_allowed": True,
        "canonical_derivation_allowed": False,
    }
    assert _policy_allows(policy) is False


def test_coordinates_are_grouped_at_roughly_building_precision():
    record = {
        "source": "fsq",
        "latitude": 37.7749123,
        "longitude": -122.4194123,
        "setting_slugs": [],
    }
    claims = {claim.field_name: claim for claim in _record_claims(record)}
    assert claims["latitude"].normalized_value == "37.7749"
    assert claims["longitude"].normalized_value == "-122.4194"


def test_website_identity_groups_same_host_across_location_paths():
    record = {
        "source": "fsq",
        "website_url": "http://www.example.com/venue?y_source=tracking",
        "setting_slugs": [],
    }
    claims = {claim.field_name: claim for claim in _record_claims(record)}
    assert claims["website_url"].normalized_value == "example.com"
