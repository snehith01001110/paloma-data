from paloma_data.field_review import (
    _project_reviewed_value,
    _review_confidence,
    _review_fingerprint,
)


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))


def test_review_fingerprint_changes_with_judgment() -> None:
    selected = _review_fingerprint(12, "selected", "a", "reviewer", "verified")
    unknown = _review_fingerprint(12, "unknown", None, "reviewer", "unverified")
    assert selected != unknown
    assert len(selected) == 64


def test_review_confidence_is_bounded() -> None:
    evidence = {
        "evidence_confidence": 1.0,
        "identity_confidence": 1.0,
        "authority": 1.0,
    }
    assert _review_confidence(evidence) == 0.99


def test_reviewed_coordinates_project_into_the_public_location() -> None:
    conn = _RecordingConnection()

    _project_reviewed_value(
        conn,
        "00000000-0000-0000-0000-000000000001",
        "latitude",
        {"value_text": "37.775", "value_json": None},
        0.9,
    )
    _project_reviewed_value(
        conn,
        "00000000-0000-0000-0000-000000000001",
        "longitude",
        {"value_text": "-122.418", "value_json": None},
        0.9,
    )

    assert "st_y(location::geometry)" not in conn.calls[0][0]
    assert "st_x(location::geometry)" in conn.calls[0][0]
    assert conn.calls[0][1][0] == "37.775"
    assert "st_y(location::geometry)" in conn.calls[1][0]
    assert conn.calls[1][1][0] == "-122.418"


def test_reviewed_operating_status_projects_into_the_public_row() -> None:
    conn = _RecordingConnection()

    _project_reviewed_value(
        conn,
        "00000000-0000-0000-0000-000000000001",
        "operating_status",
        {"value_text": "open", "value_json": None},
        0.9,
    )

    assert "set status = %s" in conn.calls[0][0]
    assert conn.calls[0][1][0] == "open"
