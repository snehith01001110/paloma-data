import pytest

from paloma_data.staging import SourceStager


def _stager(*, allow: bool = False) -> SourceStager:
    return SourceStager(
        None,
        allowed_cities=frozenset(),
        allowed_regions=frozenset(),
        allowed_countries=frozenset(),
        allow_snapshot_shrink=allow,
    )


def test_snapshot_guard_rejects_empty_and_catastrophic_shrink():
    with pytest.raises(RuntimeError, match="zero in-scope"):
        _stager()._validate_snapshot_size("fsq", current_count=0, previous_count=100)
    with pytest.raises(RuntimeError, match="shrank from 100"):
        _stager()._validate_snapshot_size("fsq", current_count=49, previous_count=100)


def test_snapshot_guard_allows_explicitly_confirmed_scope_change():
    _stager(allow=True)._validate_snapshot_size(
        "fsq", current_count=10, previous_count=100
    )
