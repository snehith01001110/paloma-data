from contextlib import contextmanager

import paloma_data.geocoding as geocoding
from paloma_data.geocoding import AddressGeocoder, GeocodeRequest, GeocodeResult, build_csv, parse_response


def test_batch_payload_is_headerless_id_street_city_state_zip():
    payload = build_csv([GeocodeRequest("42", "1 Main St", "Napa", "CA", "94559")])
    assert payload.strip() == "42,1 Main St,Napa,CA,94559"


def test_only_an_exact_match_places_a_venue():
    """A tie means several addresses fit, which is not enough to put a bar on a map."""
    response = "\n".join(
        [
            '"1","1 Main St","Match","Exact","1 MAIN ST, NAPA, CA, 94559","-122.28,38.29","1","L"',
            '"2","2 Main St","No_Match"',
            '"3","3 Main St","Tie"',
        ]
    )
    results = parse_response(response)
    assert set(results) == {"1"}
    assert results["1"].latitude == 38.29
    assert results["1"].longitude == -122.28


def test_a_malformed_coordinate_does_not_discard_the_rest_of_the_batch():
    response = "\n".join(
        [
            '"1","x","Match","Exact","A","not,coords","1","L"',
            '"2","y","Match","Exact","B","-122.4,37.8","1","L"',
        ]
    )
    assert set(parse_response(response)) == {"2"}


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.saved = []
        self.attempted = []
        self.commits = 0

    @contextmanager
    def connection(self):
        yield self

    def commit(self):
        self.commits += 1

    def records_needing_geocode(self, conn, source):
        return self.rows

    def save_geocode(self, conn, source, source_record_id, latitude, longitude, geocoder):
        self.saved.append((source_record_id, latitude, longitude, geocoder))

    def mark_geocode_attempted(self, conn, source, source_record_ids):
        self.attempted.extend(source_record_ids)


def _row(key):
    return {
        "source_record_id": key,
        "address": f"{key} Main St",
        "city": "Napa",
        "region": "CA",
        "postal_code": "94559",
    }


def test_addresses_that_do_not_match_are_still_marked_attempted(monkeypatch):
    """An address the geocoder cannot resolve must not be retried on every future run."""
    db = FakeDB([_row("a"), _row("b")])
    monkeypatch.setattr(
        geocoding,
        "geocode",
        lambda client, batch: {"a": GeocodeResult("a", 38.29, -122.28, "1 MAIN ST")},
    )

    metrics = AddressGeocoder(db).run("ca_abc")

    assert metrics == {"considered": 2, "matched": 1, "unmatched": 1, "failed_batches": 0}
    assert db.saved == [("a", 38.29, -122.28, "census")]
    assert db.attempted == ["a", "b"]


def test_a_failed_batch_is_left_unattempted_so_the_next_run_retries_it(monkeypatch):
    import httpx

    db = FakeDB([_row("a")])

    def explode(client, batch):
        raise httpx.ConnectError("census unavailable")

    monkeypatch.setattr(geocoding, "geocode", explode)

    metrics = AddressGeocoder(db).run("ca_abc")

    assert metrics["failed_batches"] == 1
    assert db.saved == []
    assert db.attempted == []


def test_nothing_to_geocode_costs_no_request():
    db = FakeDB([])
    assert AddressGeocoder(db).run("overture") == {
        "considered": 0,
        "matched": 0,
        "unmatched": 0,
        "failed_batches": 0,
    }
