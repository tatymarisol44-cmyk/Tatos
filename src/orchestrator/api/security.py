"""API-key authentication (key -> principal: service, staff or patient; see auth.py),
role checks, and a per-tenant token-bucket rate limiter.

- memory: in-process buckets. Right for dev and single-replica deployments only: with N
  replicas a tenant would get N times its budget.
- redis:  one bucket per tenant shared by every replica, updated atomically by a Lua
  script that uses Redis' clock (replica clocks do not matter). If Redis is unreachable,
  RATE_LIMIT_ON_OUTAGE decides (A30): `local` (default) degrades to this replica's own
  bucket, so an outage neither takes the API down nor lifts every limit; `open` allows
  everything; `closed` refuses."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import anyio
from fastapi import Depends, Header, HTTPException, Request, status

from orchestrator.auth import PATIENT, Principal, PrincipalStore, Role, service_principal
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

    def __init__(
        self,
        redis: Any,
        per_minute: int,
        prefix: str = "agency:ratelimit:",
        on_outage: str = "local",
    ) -> None:
        self.redis = redis
        self.capacity = per_minute
        self.rate_per_ms = per_minute / 60_000.0
        self.prefix = prefix
        self.on_outage = on_outage
        self._local = RateLimiter(per_minute)  # this replica's bucket while Redis is down
        self._script = redis.register_script(_BUCKET_SCRIPT)

    async def allow(self, key: str) -> bool:
        try:
            allowed = await self._script(
                keys=[self.prefix + key], args=[self.capacity, self.rate_per_ms]
            )
        except Exception as exc:
            log.warning(
                "rate limiter unavailable (%s); policy %s", type(exc).__name__, self.on_outage
            )
            if self.on_outage == "open":
                return True
            if self.on_outage == "closed":
                return False
            return await self._local.allow(key)
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
        return RedisRateLimiter(
            client, settings.rate_limit_per_minute, on_outage=settings.rate_limit_on_outage
        )
    return RateLimiter(settings.rate_limit_per_minute)


def resolve_tenant(settings: Settings, api_key: str | None) -> str | None:
    """Tenant of a service key from API_KEYS, "anonymous" in dev without keys, or None
    when the key is not a service key (it may still be a staff or patient key)."""
    keys = settings.tenant_keys()
    if not keys:
        if settings.app_env == "prod":
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API keys not configured")
        return "anonymous"
    if api_key:
        for known, tenant in keys.items():
            if secrets.compare_digest(api_key, known):
                return tenant
    return None


def known_tenants(settings: Settings) -> set[str]:
    """Tenants this deployment serves: those with a service key or a pack."""
    return set(settings.tenant_keys().values()) | set(settings.tenant_packs)


async def _sso_principal(request: Request, token: str) -> Principal:
    settings: Settings = request.app.state.settings
    if not settings.oidc_issuer:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "single sign-on is not enabled")
    verifier = getattr(request.app.state, "oidc", None)
    if verifier is None:
        from orchestrator.oidc import OIDCVerifier

        verifier = request.app.state.oidc = OIDCVerifier(settings)
    from orchestrator.oidc import IdentityError

    try:  # the first call may fetch the provider's keys: keep it off the event loop
        identity = await anyio.to_thread.run_sync(verifier.verify, token, known_tenants(settings))
        store: PrincipalStore = request.app.state.orchestrator.principals
        principal = await store.from_identity(identity)
    except (IdentityError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "access revoked or signed out: sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def authenticate(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> Principal:
    """Any authenticated caller (service, staff or patient), rate-limited per tenant:
    a single-sign-on token (`Authorization: Bearer`) or a key (`X-API-Key`)."""
    settings: Settings = request.app.state.settings
    principal: Principal | None
    if authorization and authorization[:7].lower() == "bearer ":
        principal = await _sso_principal(request, authorization[7:].strip())
    elif (tenant := resolve_tenant(settings, x_api_key)) is not None:
        principal = service_principal(tenant)
    elif x_api_key:
        store: PrincipalStore = request.app.state.orchestrator.principals
        principal = await store.resolve(x_api_key)
    else:
        principal = None
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")
    limiter: Limiter = request.app.state.limiter
    if not await limiter.allow(principal.tenant):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers={"Retry-After": "60"}
        )
    return principal


async def require_staff(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
    """Service or staff keys only: patient keys open nothing but /v1/me."""
    if principal.kind == PATIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "patient keys only open /v1/me")
    return principal


async def require_patient(
    principal: Annotated[Principal, Depends(authenticate)],
) -> Principal:
    if principal.kind != PATIENT or principal.subject_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this endpoint needs a patient key")
    return principal


def requires(*roles: Role) -> Callable[..., Awaitable[Principal]]:
    """Dependency: a staff/service principal holding any of `roles` (admin holds all)."""

    async def check(principal: Annotated[Principal, Depends(require_staff)]) -> Principal:
        if not principal.has(*roles):
            names = ", ".join(r.value for r in roles)
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role: {names}")
        return principal

    return check


async def require_clinician(
    principal: Annotated[Principal, Depends(require_staff)],
) -> Principal:
    """Dependency for patients' clinical data: a person holding the clinician role
    (`reviewer`) themselves. Admin and service keys are refused (need to know)."""
    if not principal.is_clinician:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "requires the clinician role (reviewer) held by the person; admin does not grant it",
        )
    return principal


async def require_tenant(principal: Annotated[Principal, Depends(require_staff)]) -> str:
    """Any staff/service caller; returns the tenant (endpoints with no role check)."""
    return principal.tenant
