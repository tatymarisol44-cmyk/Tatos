"""API-key auth (key -> tenant) and a per-tenant token-bucket rate limiter.

- memory: in-process buckets. Right for dev and single-replica deployments only: with N
  replicas a tenant would get N times its budget.
- redis:  one bucket per tenant shared by every replica, updated atomically by a Lua
  script that uses Redis' clock (replica clocks do not matter). If Redis is unreachable
  the limiter fails open and logs: rate limiting must not take the API down."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import Header, HTTPException, Request, status

from orchestrator.config import Settings

log = logging.getLogger(__name__)


class Limiter(Protocol):
    async def allow(self, key: str) -> bool: ...

    async def close(self) -> None: ...


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """In-process token bucket (see module docstring for when this is enough)."""

    def __init__(self, per_minute: int) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.setdefault(key, _Bucket(self.capacity, now))
        bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    async def close(self) -> None:
        return None


# KEYS[1] = bucket key; ARGV[1] = capacity; ARGV[2] = refill rate in tokens per ms.
_BUCKET_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1]) or capacity
local ts = tonumber(data[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate) + 1000)
return allowed
"""


class RedisRateLimiter:
    """Token bucket shared by all replicas. Same semantics as `RateLimiter`."""

    def __init__(self, redis: Any, per_minute: int, prefix: str = "agency:ratelimit:") -> None:
        self.redis = redis
        self.capacity = per_minute
        self.rate_per_ms = per_minute / 60_000.0
        self.prefix = prefix
        self._script = redis.register_script(_BUCKET_SCRIPT)

    async def allow(self, key: str) -> bool:
        try:
            allowed = await self._script(
                keys=[self.prefix + key], args=[self.capacity, self.rate_per_ms]
            )
        except Exception as exc:  # fail open: an outage of the limiter is not an outage
            log.warning("rate limiter unavailable (%s); allowing request", type(exc).__name__)
            return True
        return bool(allowed)

    async def close(self) -> None:
        await self.redis.aclose()


def build_limiter(settings: Settings) -> Limiter:
    if settings.rate_limit_backend == "redis":
        if settings.redis_url is None:
            raise ValueError("RATE_LIMIT_BACKEND=redis requires REDIS_URL")
        from redis.asyncio import Redis

        client = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_timeout=0.5,  # a slow limiter must not slow every request
            socket_connect_timeout=0.5,
        )
        return RedisRateLimiter(client, settings.rate_limit_per_minute)
    return RateLimiter(settings.rate_limit_per_minute)


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


async def request_actor(
    x_actor: str | None = Header(
        default=None,
        max_length=128,
        pattern=r"^[\w.@-]+$",
        description="The person acting (e.g. 'dr.lopez'), recorded in the audit trail. "
        "Declared by the calling application, which authenticates its own users.",
    ),
) -> str:
    return x_actor or "api"


async def require_tenant(request: Request, x_api_key: str | None = Header(default=None)) -> str:
    settings: Settings = request.app.state.settings
    tenant = resolve_tenant(settings, x_api_key)
    limiter: Limiter = request.app.state.limiter
    if not await limiter.allow(tenant):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers={"Retry-After": "60"}
        )
    return tenant
