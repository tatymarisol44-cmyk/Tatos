"""API-key auth (key -> tenant) and a per-tenant token-bucket rate limiter.

The limiter is in-process: with several replicas each one enforces its own budget.
Swap it for a Redis-backed limiter (or the API gateway's) when scaling out."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request, status

from orchestrator.config import Settings


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.setdefault(key, _Bucket(self.capacity, now))
        bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True


def resolve_tenant(settings: Settings, api_key: str | None) -> str:
    keys = settings.tenant_keys()
    if not keys:
        if settings.app_env == "prod":
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API keys not configured")
        return "anonymous"
    if api_key:
        for known, tenant in keys.items():
            if secrets.compare_digest(api_key, known):
                return tenant
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


async def require_tenant(request: Request, x_api_key: str | None = Header(default=None)) -> str:
    settings: Settings = request.app.state.settings
    tenant = resolve_tenant(settings, x_api_key)
    limiter: RateLimiter = request.app.state.limiter
    if not limiter.allow(tenant):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers={"Retry-After": "60"}
        )
    return tenant
