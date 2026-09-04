from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx


PUBLIC_MEDIA_BUCKET = "paloma-establishment-media"
PRIVATE_SOURCE_BUCKET = "paloma-establishment-media-sources"
IMMUTABLE_CACHE_SECONDS = 31_536_000


@dataclass(frozen=True, slots=True)
class StoredMediaObject:
    bucket_id: str
    object_path: str
    public_url: str | None


class SupabaseMediaStorage:
    """Small server-only Storage API client for immutable media objects."""

    def __init__(
        self,
        project_url: str,
        service_role_key: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        project_url = project_url.strip().rstrip("/")
        if not project_url.startswith("https://"):
            raise ValueError("SUPABASE_URL must use https")
        if not service_role_key.strip():
            raise ValueError("A server-only Supabase secret key is required")
        self.project_url = project_url
        self.service_role_key = service_role_key.strip()
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> SupabaseMediaStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upload_immutable(
        self,
        local_path: Path,
        *,
        bucket_id: str,
        object_path: str,
        content_type: str,
        public: bool,
    ) -> StoredMediaObject:
        if not local_path.is_file():
            raise ValueError(f"Media file does not exist: {local_path}")
        normalized_path = _object_path(object_path)
        encoded_bucket = quote(bucket_id, safe="")
        encoded_path = quote(normalized_path, safe="/")
        response = self.client.post(
            f"{self.project_url}/storage/v1/object/{encoded_bucket}/{encoded_path}",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Content-Type": content_type,
                "Cache-Control": f"max-age={IMMUTABLE_CACHE_SECONDS}, immutable",
            },
            content=local_path.read_bytes(),
        )
        response.raise_for_status()
        public_url = None
        if public:
            public_url = (
                f"{self.project_url}/storage/v1/object/public/"
                f"{encoded_bucket}/{encoded_path}"
            )
        return StoredMediaObject(
            bucket_id=bucket_id,
            object_path=normalized_path,
            public_url=public_url,
        )


def _object_path(value: str) -> str:
    path = PurePosixPath(value.strip())
    normalized = str(path)
    if (
        not value.strip()
        or path.is_absolute()
        or normalized in {".", ".."}
        or ".." in path.parts
        or "\\" in value
    ):
        raise ValueError("Storage object path must be a safe relative POSIX path")
    return normalized
