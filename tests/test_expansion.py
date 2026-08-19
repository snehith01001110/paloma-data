from contextlib import contextmanager

import pytest

from paloma_data.expansion import (
    ExpansionBlocked,
    ExpansionGate,
    load_expansion_manifest,
)


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        if "catalog_expansion_status" in query:
            return _Cursor({"status": self.status})
        return _Cursor()


class _Database:
    def __init__(self, status):
        self.conn = _Connection(status)

    @contextmanager
    def connection(self):
        yield self.conn


def test_manifest_covers_the_official_nine_county_region_and_101_jurisdictions():
    manifest = load_expansion_manifest()

    assert len(manifest.county_fips) == 9
    assert sum(len(cities) for cities in manifest.jurisdictions.values()) == 101
    assert len(manifest.sha256) == 64
    assert manifest.release("east-bay-pilot-v1").cities == ("Berkeley", "Oakland")


def test_unknown_release_fails_before_database_access():
    gate = ExpansionGate(_Database({"ready": True}))

    with pytest.raises(ValueError, match="Unknown expansion release"):
        gate.arm(_Connection({"ready": True}), "everything-at-once")


def test_blocked_release_never_arms_the_database_session():
    connection = _Connection(
        {"ready": False, "blockers": ["authorization_missing", "terms_not_active"]}
    )
    gate = ExpansionGate(_Database(connection.status))

    with pytest.raises(ExpansionBlocked, match="authorization_missing; terms_not_active"):
        gate.arm(connection, "east-bay-pilot-v1")

    assert len(connection.calls) == 1


def test_missing_authorization_does_not_mislabel_unavailable_capacity_as_exhausted():
    database = _Database(
        {
            "ready": False,
            "authorization_event_id": None,
            "available_slots": 0,
            "blockers": ["authorization_missing", "release_capacity_exhausted"],
        }
    )
    gate = ExpansionGate(database)

    status = gate.status(database.conn, "east-bay-pilot-v1")

    assert status["blockers"] == ["authorization_missing"]


def test_ready_release_arms_the_exact_manifest_identity_for_the_database_trigger():
    connection = _Connection({"ready": True, "blockers": []})
    gate = ExpansionGate(_Database(connection.status))

    release, status = gate.arm(connection, "east-bay-pilot-v1")

    assert status["ready"] is True
    assert release.maximum_new_publications == 25
    release_setting, manifest_setting = connection.calls[-2:]
    assert "paloma.expansion_release_id" in release_setting[0]
    assert release_setting[1] == ("east-bay-pilot-v1",)
    assert "paloma.expansion_manifest_sha256" in manifest_setting[0]
    assert manifest_setting[1] == (gate.manifest.sha256,)
