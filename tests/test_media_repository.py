from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

import pytest

from paloma_data.media_repository import EstablishmentMediaRepository


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows

    def fetchone(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Result:
        self.calls.append((query, parameters))
        return _Result(self.responses.pop(0))


class _Database:
    def __init__(self, connection: _Connection) -> None:
        self.conn = connection

    @contextmanager
    def connection(self):
        yield self.conn


def test_targets_are_generic_and_include_every_published_city_by_default() -> None:
    connection = _Connection(
        [
            [
                {
                    "establishment_id": "454159f6-5572-4852-b8f3-3bdb8faf92cd",
                    "name": "Any Future Venue",
                    "address": "1 Main Street",
                    "city": "Future City",
                    "latitude": 37.7,
                    "longitude": -122.0,
                }
            ]
        ]
    )
    repository = EstablishmentMediaRepository(_Database(connection))

    targets = repository.targets(limit=25)

    assert [target.name for target in targets] == ["Any Future Venue"]
    query, parameters = connection.calls[0]
    assert "publication_state = 'published'" in query
    assert "cover_image_url is null" in query
    assert parameters == ([], [], True, 25)


def test_quality_approval_can_only_use_the_guarded_database_function() -> None:
    asset_id = "a5a11e3a-78c5-44e3-8139-a6863e6320df"
    connection = _Connection([[{"id": asset_id}]])
    repository = EstablishmentMediaRepository(_Database(connection))

    repository.approve_asset(asset_id, reviewed_by=" reviewer ", notes=" looks good ")

    query, parameters = connection.calls[0]
    assert "catalog.approve_establishment_media_asset" in query
    assert "update catalog.establishment_media_assets" not in query.casefold()
    assert parameters == (asset_id, "reviewer", "looks good")


def test_register_asset_requires_all_three_responsive_variants() -> None:
    repository = EstablishmentMediaRepository(_Database(_Connection([])))

    with pytest.raises(ValueError, match="hero, card, and thumbnail"):
        repository.register_rendered_asset(
            asset_id=UUID("a5a11e3a-78c5-44e3-8139-a6863e6320df"),
            establishment_id="454159f6-5572-4852-b8f3-3bdb8faf92cd",
            source_id=None,
            asset_kind="category_illustration",
            generator="test",
            generator_version="1",
            prompt_sha256="0" * 64,
            input_sha256=None,
            attribution_text=None,
            disclosure_text="Category artwork; not the actual establishment",
            output_license_id="Paloma-Proprietary",
            output_license_url=None,
            variants=[],
        )


def test_source_review_rejects_unknown_verdict_before_database_access() -> None:
    connection = _Connection([])
    repository = EstablishmentMediaRepository(_Database(connection))

    with pytest.raises(ValueError, match="Unsupported media review verdict"):
        repository.record_source_review(
            "454159f6-5572-4852-b8f3-3bdb8faf92cd",
            verdict="probably_nearby",
            reviewed_by="reviewer",
            notes="Proximity is not identity.",
        )

    assert connection.calls == []
