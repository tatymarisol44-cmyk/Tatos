"""Where rendered creatives live until a platform fetches them (ADR 0015, M3).

Instagram downloads the media itself from a URL that must be reachable when it publishes.
Nothing here is ever public: with Google Cloud Storage the bucket enforces public access
prevention and the platform gets a V4 signed URL that expires (Google: signed URLs are not
affected by public access prevention; the longest lifetime is 7 days). Without a bucket the
files stay on local disk and there is no URL, so URL-based publishing is refused upstream.

Objects are keyed by tenant first, so one tenant's prefix never lists another's."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import shutil
from datetime import UTC, datetime, timedelta
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

    async def local_file(self, name: str) -> Path | None:
        """A readable local copy, for uploads that send the file itself (TikTok). Replicas
        do not share disks, so a store backed by a bucket fetches it when missing."""
        ...

    async def signed_url(self, name: str) -> str | None:
        """A time-limited URL a platform can fetch, or None if this store cannot make one."""
        ...


def media_signature(key: bytes, name: str, expires: int) -> str:
    return hmac.new(key, f"{name}|{expires}".encode(), hashlib.sha256).hexdigest()


class LocalMediaStore:
    """Files under MEDIA_DIR. With PUBLIC_BASE_URL (e.g. a Codespace's public port, or a
    single-node deployment) the API itself serves each creative at a URL signed with HMAC
    and valid for MEDIA_URL_TTL_MINUTES (`GET /media/...`), which is what Instagram and
    Facebook fetch. Without it there is no URL, and URL-based publishing is refused
    upstream. Several replicas do not share disks: use the bucket there."""

    def __init__(
        self,
        root: Path,
        public_base_url: str | None = None,
        key: bytes | None = None,
        ttl: timedelta = timedelta(hours=1),
    ) -> None:
        self.root = root
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.key = key
        self.ttl = ttl

    def _path(self, name: str) -> Path:
        path = (self.root / name).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("object name escapes the media directory")
        return path

    async def put(self, name: str, source: Path, content_type: str) -> None:
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source, target)

    def path_for(self, name: str) -> Path:
        return self._path(name)

    async def local_file(self, name: str) -> Path | None:
        path = self._path(name)
        return path if path.exists() else None

    async def signed_url(self, name: str) -> str | None:
        if not (self.public_base_url and self.key) or not self._path(name).exists():
            return None
        expires = int((datetime.now(UTC) + self.ttl).timestamp())
        sig = media_signature(self.key, name, expires)
        return f"{self.public_base_url}/media/{name}?exp={expires}&sig={sig}"

    def verify(self, name: str, expires: int, sig: str) -> Path | None:
        """The file behind a signed URL, or None if the URL is forged, expired or unknown."""
        if self.key is None or expires < int(datetime.now(UTC).timestamp()):
            return None
        if not hmac.compare_digest(media_signature(self.key, name, expires), sig):
            return None
        try:
            path = self._path(name)
        except ValueError:
            return None
        return path if path.is_file() else None


class GcsMediaStore:
    """Google Cloud Storage. Credentials come from the environment (Application Default
    Credentials: a workload identity in production, a key file only in development)."""

    def __init__(self, bucket: str, ttl: timedelta, cache_dir: Path, client: Any = None) -> None:
        if client is None:  # pragma: no cover - needs Google credentials
            from google.cloud import storage

            client = storage.Client()
        self.bucket = client.bucket(bucket)
        self.ttl = ttl
        self.cache = LocalMediaStore(cache_dir)

    async def put(self, name: str, source: Path, content_type: str) -> None:
        blob = self.bucket.blob(name)
        await asyncio.to_thread(blob.upload_from_filename, str(source), content_type=content_type)
        await self.cache.put(name, source, content_type)

    async def local_file(self, name: str) -> Path | None:
        cached = await self.cache.local_file(name)
        if cached is not None:
            return cached
        target = self.cache.path_for(name)  # created on another replica: fetch it
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.bucket.blob(name).download_to_filename, str(target))
        return target

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
            settings.media_dir,
        )
    # A key of its own, derived from PSEUDONYM_KEY (domain-separated): no new secret.
    key = hmac.new(settings.pseudonym_secret(), b"media-url-v1", hashlib.sha256).digest()
    # Only an https address can be fetched by Meta; a local http one would be useless.
    public = settings.public_base_url
    return LocalMediaStore(
        settings.media_dir,
        public if public.startswith("https://") else None,
        key,
        timedelta(minutes=settings.media_url_ttl_minutes),
    )
