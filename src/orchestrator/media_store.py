"""Where rendered creatives live until a platform fetches them (ADR 0015, M3).

Instagram downloads the media itself from a URL that must be reachable when it publishes.
Nothing here is ever public: with Google Cloud Storage the bucket enforces public access
prevention and the platform gets a V4 signed URL that expires (Google: signed URLs are not
affected by public access prevention; the longest lifetime is 7 days). Without a bucket the
files stay on local disk and there is no URL, so URL-based publishing is refused upstream.

Objects are keyed by tenant first, so one tenant's prefix never lists another's."""

from __future__ import annotations

import asyncio
import re
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from orchestrator.config import Settings

_KEY = re.compile(r"^[\w.-]{1,64}$")


def object_name(tenant: str, publication_id: str, suffix: str) -> str:
    if not (_KEY.fullmatch(tenant) and _KEY.fullmatch(publication_id)):
        raise ValueError("tenant and publication id must be simple names")
    if suffix not in (".jpg", ".mp4"):
        raise ValueError(f"unsupported media suffix {suffix!r}")
    return f"{tenant}/{publication_id}{suffix}"


class MediaStore(Protocol):
    async def put(self, name: str, source: Path, content_type: str) -> None: ...

    def local_path(self, name: str) -> Path | None:
        """A readable local copy (for uploads that send the file itself), if any."""
        ...

    async def signed_url(self, name: str) -> str | None:
        """A time-limited URL a platform can fetch, or None if this store cannot make one."""
        ...


class LocalMediaStore:
    """Development: files under MEDIA_DIR, no URL. Video uploaded as a file still works."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, name: str) -> Path:
        path = (self.root / name).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("object name escapes the media directory")
        return path

    async def put(self, name: str, source: Path, content_type: str) -> None:
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source, target)

    def local_path(self, name: str) -> Path | None:
        path = self._path(name)
        return path if path.exists() else None

    async def signed_url(self, name: str) -> str | None:
        return None


class GcsMediaStore:
    """Google Cloud Storage. Credentials come from the environment (Application Default
    Credentials: a workload identity in production, a key file only in development)."""

    def __init__(
        self, bucket: str, ttl: timedelta, client: Any = None, cache_dir: Path | None = None
    ) -> None:
        if client is None:  # pragma: no cover - needs Google credentials
            from google.cloud import storage

            client = storage.Client()
        self.bucket = client.bucket(bucket)
        self.ttl = ttl
        self.cache = LocalMediaStore(cache_dir) if cache_dir else None

    async def put(self, name: str, source: Path, content_type: str) -> None:
        blob = self.bucket.blob(name)
        await asyncio.to_thread(blob.upload_from_filename, str(source), content_type=content_type)
        if self.cache is not None:
            await self.cache.put(name, source, content_type)

    def local_path(self, name: str) -> Path | None:
        return self.cache.local_path(name) if self.cache else None

    async def signed_url(self, name: str) -> str | None:
        blob = self.bucket.blob(name)
        url: str = await asyncio.to_thread(
            blob.generate_signed_url, version="v4", expiration=self.ttl, method="GET"
        )
        return url


def build_media_store(settings: Settings) -> MediaStore:
    if settings.gcs_bucket:
        return GcsMediaStore(
            settings.gcs_bucket,
            timedelta(minutes=settings.media_url_ttl_minutes),
            cache_dir=settings.media_dir,
        )
    return LocalMediaStore(settings.media_dir)
