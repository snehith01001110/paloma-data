from datetime import datetime, timedelta, timezone

from paloma_data.field_resolution import (
    SOURCE_POLICIES,
    FieldResolver,
    _address_parts,
    _admissible_websites,
    _agreement_groups,
    _conflict_evidence_ids,
    _corroborate_operating_status,
    _filter_changed_decisions,
    _is_own_website,
    _manual_review_covers_current_evidence,
    _reapply_manual_projections,
    _require_candidate_contact_corroboration,
    _review_reason,
    _website_host,
)


def test_government_names_are_legal_not_display_names():
    assert SOURCE_POLICIES["ca_abc"].name_kind == "legal"
    assert SOURCE_POLICIES["datasf"].name_kind == "legal"
    assert SOURCE_POLICIES["overture"].name_kind == "display"
    assert SOURCE_POLICIES["fsq"].name_kind == "display"


def test_verified_first_party_name_outranks_aggregator_name():
    resolver = FieldResolver(None)  # scoring is pure and does not touch the database
    now = datetime.now(timezone.utc)
    official = {
        "authority": 1.0,
        "evidence_confidence": 0.995,
        "identity_confidence": 0.995,
        "source_updated_at": now,
    }
    overture = {
        "authority": SOURCE_POLICIES["overture"].name_authority,
        "evidence_confidence": 0.92,
        "identity_confidence": 1.0,
        "source_updated_at": now,
    }
    assert resolver._evidence_score(official) > 0.98
    assert resolver._evidence_score(official) > resolver._evidence_score(overture)


def test_aggregator_name_cannot_look_like_099_field_confidence():
    resolver = FieldResolver(None)
    score = resolver._evidence_score(
        {
            "authority": SOURCE_POLICIES["overture"].name_authority,
            "evidence_confidence": 0.92,
            "identity_confidence": 1.0,
            "source_updated_at": datetime.now(timezone.utc),
        }
    )
    assert score < 0.85


def test_direct_upstream_wins_over_conflicting_overture_copy():
    resolver = FieldResolver(None)
    rows = [
        {
            "evidence_id": "direct",
            "field_name": "phone_e164",
            "value_text": "+14155550100",
            "normalized_value": "+14155550100",
            "value_json": None,
            "source": "fsq",
            "upstream_origin_keys": ["foursquare"],
            "authority": 0.90,
            "evidence_confidence": 0.96,
            "identity_confidence": 1.0,
            "source_updated_at": None,
        },
        {
            "evidence_id": "copy",
            "field_name": "phone_e164",
            "value_text": "+14155550999",
            "normalized_value": "+14155550999",
            "value_json": None,
            "source": "overture",
            "upstream_origin_keys": ["foursquare"],
            "authority": 0.88,
            "evidence_confidence": 0.96,
            "identity_confidence": 1.0,
            "source_updated_at": None,
        },
    ]

    selected = resolver._select_attribute(rows, 0.68)

    assert selected is not None
    assert selected["best_source"] == "fsq"


def test_reviewed_civic_polygon_outranks_a_broader_registration_label():
    resolver = FieldResolver(None)
    base = {
        "field_name": "neighborhood",
        "value_json": None,
        "identity_confidence": 0.985,
        "source_updated_at": None,
        "upstream_origin_keys": ["datasf"],
    }
    rows = [
        {
            **base,
            "evidence_id": "registration",
            "value_text": "Sunset/Parkside",
            "normalized_value": "sunset/parkside",
            "source": "datasf",
            "authority": 0.90,
            "evidence_confidence": 0.96,
        },
        {
            **base,
            "evidence_id": "boundary",
            "value_text": "Outer Sunset",
            "normalized_value": "outer sunset",
            "source": "datasf_neighborhoods",
            "authority": 0.94,
            "evidence_confidence": 0.98,
        },
    ]

    selected = resolver._select_neighborhood(rows)

    assert selected is not None
    assert selected["value_text"] == "Outer Sunset"
    assert selected["best_source"] == "datasf_neighborhoods"


def test_private_candidate_contact_requires_two_independent_origins():
    provider_only = {
        "best_source": "overture",
        "source_count": 1,
        "value_text": "+15105550100",
    }
    corroborated = {**provider_only, "source_count": 2}

    assert (
        _require_candidate_contact_corroboration("phone_e164", provider_only) is None
    )
    assert (
        _require_candidate_contact_corroboration("phone_e164", corroborated)
        is corroborated
    )


def test_reviewed_candidate_contact_can_use_one_first_party_observation():
    reviewed = {
        "best_source": "manual",
        "source_count": 1,
        "value_text": "https://example.com",
    }

    assert (
        _require_candidate_contact_corroboration("website_url", reviewed) is reviewed
    )


def test_unchanged_decisions_are_deduplicated_but_a_return_is_appended():
    first = ("venue", "hours", "selected", "old-fingerprint")
    second = ("venue", "hours", "unknown", "new-fingerprint")

    assert _filter_changed_decisions(
        [first], {("venue", "hours"): "old-fingerprint"}
    ) == []
    assert _filter_changed_decisions(
        [first], {("venue", "hours"): "new-fingerprint"}
    ) == [first]
    assert _filter_changed_decisions(
        [first, second], {("venue", "hours"): "old-fingerprint"}
    ) == [second]


def test_conflict_retains_every_evidence_id():
    rows = [
        {"evidence_id": "second"},
        {"evidence_id": "first"},
        {"evidence_id": "second"},
    ]

    assert _conflict_evidence_ids(rows) == ["first", "second"]


def test_manual_review_is_preserved_until_new_evidence_arrives():
    rows = [{"evidence_id": "first"}, {"evidence_id": "second"}]

    assert _manual_review_covers_current_evidence({"first", "second"}, rows)
    assert not _manual_review_covers_current_evidence({"first"}, rows)
    assert not _manual_review_covers_current_evidence(None, rows)


def test_manual_projection_reconciles_all_mutable_durable_fields():
    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)

    connection = Connection()
    _reapply_manual_projections(connection)

    sql = "\n".join(connection.statements)
    assert len(connection.statements) == 9
    for field in (
        "phone_e164",
        "website_url",
        "address",
        "latitude",
        "longitude",
        "operating_status",
        "neighborhood",
        "hours",
        "price_level",
    ):
        assert field in sql
    assert "hours_expires_at" in sql
    assert "evidence.expires_at > now()" in sql
    assert "observation.metadata as observation_metadata" in sql
    assert "d.observation_metadata->>'evidence_kind'" in sql


def test_first_party_hours_do_not_require_a_second_origin() -> None:
    now = datetime.now(timezone.utc)
    selected = {
        "best_source": "manual",
        "observed_at": now,
        "expires_at": now + timedelta(days=30),
        "source_items": [
            {"kind": "first_party", "url": "https://example.com/hours"}
        ],
        "metadata": {"evidence_kind": "first_party"},
        "independent_origin_keys": ["manual:reviewer"],
    }

    assert _review_reason("hours", [], selected, True) is None


def test_disagreeing_current_hours_enter_the_conflict_queue() -> None:
    selected = {"independent_origin_keys": ["merchant"]}
    rows = [
        {"normalized_value": "schedule-a"},
        {"normalized_value": "schedule-b"},
    ]

    assert (
        _review_reason("hours", rows, selected, True)
        == "authoritative_hours_disagreement"
    )


def test_geocoder_rounding_is_one_value_not_a_conflict():
    """Three sources placing one storefront within metres of each other agree."""
    groups = _agreement_groups("latitude", ["37.7859", "37.7858", "37.7860"])

    assert len(set(groups.values())) == 1


def test_genuinely_distant_coordinates_stay_in_the_queue():
    # ~245m apart: two different buildings, not one geocoder rounding differently.
    groups = _agreement_groups("longitude", ["-122.4551", "-122.4577", "-122.4579"])

    assert len(set(groups.values())) > 1


def test_cosmetic_address_variants_are_one_doorway():
    for variants in (
        ["5152 moorpark ave", "5152 moorpark ave unit 20"],
        ["501 503 505 jones st", "501 jones st"],
        ["1849 lincoln way", "1849 lincoln wy"],
        ["1340 5th st", "1340 fifth st"],
        ["401 2nd st", "401 and 407 second st"],
        ["1160 oak knoll ave", "1160 w oak knoll ave"],
        ["240 242 ofarrell st", "242 o farrell st", "242 ofarrell st"],
        ["29 n san pedro st", "29 n san pedro st downstairs"],
        ["2000 denmark st", "2000 denmark st gundlach bundschu winery"],
    ):
        assert len(set(_agreement_groups("address", variants).values())) == 1, variants


def test_different_addresses_are_still_conflicts():
    for variants in (
        ["444 presidio ave", "488 presidio ave"],
        ["115 sansome st", "200 bush st"],
        ["100 n main st", "100 s main st"],
        ["452 1st st e", "452 1st st w"],
    ):
        assert len(set(_agreement_groups("address", variants).values())) > 1, variants


def test_a_street_named_front_is_not_read_as_a_unit_designator():
    parts = _address_parts("100 n front st")

    assert parts is not None
    assert parts.street == "frontst"


def test_only_located_fields_relax_exact_equality():
    """A different phone number is a real disagreement, whatever the field policy is."""
    numbers = ["+14155550100", "+14155550999"]

    assert len(set(_agreement_groups("phone_e164", numbers).values())) == 2


def test_agreeing_sources_no_longer_open_a_field_conflict():
    """Two geocoders 11m apart corroborate one point instead of deadlocking."""
    resolver = FieldResolver(None)
    rows = [
        {
            "evidence_id": "overture",
            "field_name": "latitude",
            "value_text": "37.3200",
            "normalized_value": "37.3200",
            "value_json": None,
            "source": "overture",
            "upstream_origin_keys": ["meta"],
            "authority": SOURCE_POLICIES["overture"].location_authority,
            "evidence_confidence": 0.98,
            "identity_confidence": 0.995,
            "source_updated_at": datetime.now(timezone.utc),
        },
        {
            "evidence_id": "fsq",
            "field_name": "latitude",
            "value_text": "37.3199",
            "normalized_value": "37.3199",
            "value_json": None,
            "source": "fsq",
            "upstream_origin_keys": ["foursquare"],
            "authority": SOURCE_POLICIES["fsq"].location_authority,
            "evidence_confidence": 0.98,
            "identity_confidence": 1.0,
            "source_updated_at": datetime.now(timezone.utc),
        },
    ]

    selected = resolver._select_attribute(rows, 0.68)

    assert selected is not None
    # The highest-authority reading is published, corroborated by the other origin.
    assert selected["best_source"] == "fsq"
    assert selected["normalized_value"] == "37.3199"
    assert selected["independent_origin_keys"] == ["foursquare", "meta"]
    assert _review_reason("latitude", rows, selected, False) is None


def test_a_deadlocked_coordinate_pair_was_the_bug_being_fixed():
    """Without grouping these two rows select nothing, which is what queued 116 rows."""
    resolver = FieldResolver(None)
    far_apart = [
        {
            "evidence_id": "overture",
            "field_name": "latitude",
            "value_text": "37.3400",
            "normalized_value": "37.3400",
            "value_json": None,
            "source": "overture",
            "upstream_origin_keys": ["meta"],
            "authority": SOURCE_POLICIES["overture"].location_authority,
            "evidence_confidence": 0.98,
            "identity_confidence": 0.995,
            "source_updated_at": datetime.now(timezone.utc),
        },
        {
            "evidence_id": "fsq",
            "field_name": "latitude",
            "value_text": "37.3199",
            "normalized_value": "37.3199",
            "value_json": None,
            "source": "fsq",
            "upstream_origin_keys": ["foursquare"],
            "authority": SOURCE_POLICIES["fsq"].location_authority,
            "evidence_confidence": 0.98,
            "identity_confidence": 1.0,
            "source_updated_at": datetime.now(timezone.utc),
        },
    ]

    selected = resolver._select_attribute(far_apart, 0.68)

    assert selected is None
    assert (
        _review_reason("latitude", far_apart, selected, False)
        == "conflicting_admissible_evidence"
    )


def test_directory_listings_are_not_a_venue_website():
    for url in (
        "https://www.yelp.com/biz/some-bar-san-francisco",
        "https://groupon.com/deals/some-bar",
        "https://2101-club.hub.biz",
        "https://buddhaloungesanfrancisco.gastro-america.com",
        "https://ratebeer.com/brewers/some-brewery/1234",
        "https://www.eventbrite.com/e/some-tasting-12345",
    ):
        row = {"value_text": url, "normalized_value": _website_host({"value_text": url})}
        assert not _is_own_website(row), url


def test_a_venue_profile_on_a_platform_is_admissible_but_the_root_is_not():
    profile = "https://facebook.com/BartlettHallSF"
    root = "https://facebook.com/"

    assert _is_own_website({"value_text": profile, "normalized_value": "facebook.com"})
    assert not _is_own_website({"value_text": root, "normalized_value": "facebook.com"})


def test_a_real_venue_domain_stays_admissible():
    for url in (
        "https://bourbonandbranch.com",
        "https://www.clinecellars.com/visit",
        "https://mybar.toasttab.com/order",
    ):
        row = {"value_text": url, "normalized_value": _website_host({"value_text": url})}
        assert _is_own_website(row), url


def test_a_directory_listing_no_longer_opens_a_website_conflict():
    """Kona Club had one real domain and one Twitter link; that is not a disagreement."""
    rows = [
        {
            "evidence_id": "fsq",
            "field_name": "website_url",
            "value_text": "https://konaclub.net",
            "normalized_value": "konaclub.net",
            "value_json": None,
            "source": "fsq",
            "upstream_origin_keys": ["foursquare"],
            "authority": SOURCE_POLICIES["fsq"].website_authority,
            "evidence_confidence": 0.92,
            "identity_confidence": 1.0,
            "source_updated_at": datetime.now(timezone.utc),
        },
        {
            "evidence_id": "overture",
            "field_name": "website_url",
            "value_text": "https://ratebeer.com/brewers/kona/999",
            "normalized_value": "ratebeer.com",
            "value_json": None,
            "source": "overture",
            "upstream_origin_keys": ["meta"],
            "authority": SOURCE_POLICIES["overture"].website_authority,
            "evidence_confidence": 0.92,
            "identity_confidence": 0.995,
            "source_updated_at": datetime.now(timezone.utc),
        },
    ]
    admissible = [row for row in rows if _is_own_website(row)]

    resolver = FieldResolver(None)
    selected = resolver._select_attribute(admissible, 0.68)

    assert selected is not None
    assert selected["normalized_value"] == "konaclub.net"
    assert _review_reason("website_url", admissible, selected, False) is None


def test_an_owner_may_choose_a_listing_the_resolver_would_reject():
    """Club Malibu's hub.biz page was entered by hand; the filter governs sources."""
    chosen = {
        "source": "manual",
        "value_text": "https://club-malibu.hub.biz",
        "normalized_value": "club-malibu.hub.biz",
    }
    inferred = {
        "source": "fsq",
        "value_text": "https://2101-club.hub.biz",
        "normalized_value": "2101-club.hub.biz",
    }

    assert _is_own_website(chosen)
    assert not _is_own_website(inferred)


def test_an_owned_domain_beats_a_social_profile_for_the_same_venue():
    """Kona Club has konaclub.net and a Twitter profile; that is not a disagreement."""
    domain = {
        "source": "fsq",
        "value_text": "http://konaclub.net",
        "normalized_value": "konaclub.net",
    }
    profile = {
        "source": "overture",
        "value_text": "http://twitter.com/kona_club",
        "normalized_value": "twitter.com",
    }

    assert _admissible_websites([domain, profile]) == [domain]


def test_a_venue_with_only_a_social_profile_keeps_it():
    profile = {
        "source": "fsq",
        "value_text": "http://facebook.com/DovreClubSF",
        "normalized_value": "facebook.com",
    }
    listing = {
        "source": "overture",
        "value_text": "http://yelp.com/biz/dovre-club",
        "normalized_value": "yelp.com",
    }

    assert _admissible_websites([profile, listing]) == [profile]


def test_a_hand_entered_profile_outranks_an_inferred_domain():
    chosen = {
        "source": "manual",
        "value_text": "https://instagram.com/smittysloungeoakland",
        "normalized_value": "instagram.com",
    }
    inferred = {
        "source": "overture",
        "value_text": "http://smittys.example.com",
        "normalized_value": "smittys.example.com",
    }

    assert chosen in _admissible_websites([chosen, inferred])


def _status_row(source, origin, value="open", evidence_id="e"):
    return {
        "evidence_id": evidence_id,
        "field_name": "operating_status",
        "value_text": value,
        "normalized_value": value,
        "value_json": None,
        "source": source,
        "upstream_origin_keys": [origin],
        "authority": 0.84,
        "evidence_confidence": 0.94,
        "identity_confidence": 1.0,
        "source_updated_at": datetime.now(timezone.utc),
    }


def test_a_licence_corroborates_an_open_status_no_human_needed():
    """Overture republishing Foursquare is one origin; CA ABC is a second."""
    selected = {
        "normalized_value": "open",
        "independent_origin_keys": ["foursquare"],
        "evidence_ids": ["fsq"],
    }
    regulator = [_status_row("ca_abc", "ca_abc", evidence_id="abc")]

    corroborated = _corroborate_operating_status(selected, regulator)

    assert corroborated["independent_origin_keys"] == ["ca_abc", "foursquare"]
    assert corroborated["evidence_ids"] == ["abc", "fsq"]
    assert _review_reason("operating_status", [], corroborated, True) is None


def test_a_licence_alone_cannot_publish_a_venue_as_open():
    """A licence outlives the business, so it may never assert a status by itself."""
    assert _corroborate_operating_status(None, [_status_row("ca_abc", "ca_abc")]) is None


def test_a_licence_that_disagrees_does_not_corroborate():
    selected = {
        "normalized_value": "closed",
        "independent_origin_keys": ["foursquare"],
        "evidence_ids": ["fsq"],
    }

    corroborated = _corroborate_operating_status(
        selected, [_status_row("ca_abc", "ca_abc", value="open")]
    )

    assert corroborated["independent_origin_keys"] == ["foursquare"]
    assert (
        _review_reason("operating_status", [], corroborated, True)
        == "single_origin_high_risk_field"
    )
