from pathlib import Path

import httpx
import pytest

from paloma_data.media_storage import SupabaseMediaStorage, _object_path


def test_upload_immutable_uses_new_object_path_and_public_url(tmp_path: Path):
    source = tmp_path / "image.jpg"
    source.write_bytes(b"jpeg payload")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith(
            "/storage/v1/object/paloma-establishment-media/venue-id/hero-hash.jpg"
        )
        assert request.headers["authorization"] == "Bearer service-secret"
        assert request.headers["cache-control"] == "max-age=31536000, immutable"
        assert request.content == b"jpeg payload"
        assert "x-upsert" not in request.headers
        return httpx.Response(200, json={"Key": "venue-id/hero-hash.jpg"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        storage = SupabaseMediaStorage(
            "https://project.supabase.co",
            "service-secret",
            client=client,
        )
        stored = storage.upload_immutable(
            source,
            bucket_id="paloma-establishment-media",
            object_path="venue-id/hero-hash.jpg",
            content_type="image/jpeg",
            public=True,
        )

    assert stored.public_url == (
        "https://project.supabase.co/storage/v1/object/public/"
        "paloma-establishment-media/venue-id/hero-hash.jpg"
    )


@pytest.mark.parametrize("value", ["", "/absolute.jpg", "../escape.jpg", "a/../../b", r"a\b"])
def test_object_path_rejects_unsafe_paths(value: str):
    with pytest.raises(ValueError):
        _object_path(value)
