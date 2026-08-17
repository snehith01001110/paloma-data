from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from paloma_data.models import SourceRecord


class SourceAdapter(ABC):
    source: str

    @abstractmethod
    def backfill(self) -> Iterator[SourceRecord]:
        raise NotImplementedError

    def incremental(self, cursor: str | None = None) -> Iterator[SourceRecord]:
        # Sources without a true delta feed may reuse a stable-ID snapshot. The database
        # payload hash makes unchanged rows cheap and preserves correctness.
        yield from self.backfill()
