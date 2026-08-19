from paloma_data.field_review import _review_confidence, _review_fingerprint


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
