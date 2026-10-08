"""Circuit breaker (audit 2026-10-08, resilience): when a dependency keeps failing, stop
calling it for a while and fail fast, instead of making every request wait for its
timeout and piling retries onto a provider that is already down.

States: CLOSED (calls go through; consecutive failures are counted) -> OPEN after
`failures` in a row (calls fail at once with `CircuitOpen` for `cooldown_s`) -> HALF_OPEN
(one trial call; success closes, failure re-opens).

The state is per process. Each replica learns on its own within `failures` calls, which
is cheap and needs no shared store that could itself be down.

An LLM outage must never take down the rest of the product: agenda, CRM, the clinical
record and consents make no model calls, and the chat answers 503 with a Retry-After the
console shows as "the assistant is unavailable, try again shortly"."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")
State = Literal["closed", "open", "half_open"]


class CircuitOpen(RuntimeError):
    """The dependency is known to be down; retry after `retry_after` seconds."""

    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"{name} is unavailable; retry in {retry_after:.0f}s")
        self.name = name
        self.retry_after = retry_after


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failures: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failures = failures
        self.cooldown_s = cooldown_s
        self.clock = clock
        self._count = 0
        self._opened_at: float | None = None
        self._trial = False

    @property
    def state(self) -> State:
        if self._opened_at is None:
            return "closed"
        if self.clock() - self._opened_at >= self.cooldown_s:
            return "half_open"
        return "open"

    def _before(self) -> None:
        state = self.state
        if state == "open" or (state == "half_open" and self._trial):
            assert self._opened_at is not None
            remaining = self.cooldown_s - (self.clock() - self._opened_at)
            raise CircuitOpen(self.name, max(remaining, 1.0))
        if state == "half_open":
            self._trial = True

    def _success(self) -> None:
        if self._opened_at is not None:
            log.warning("%s recovered; circuit closed", self.name)
        self._count, self._opened_at, self._trial = 0, None, False

    def _failure(self) -> None:
        self._count += 1
        if self._trial or self._count >= self.failures:
            if self._opened_at is None or self._trial:
                log.error("%s failing; circuit open for %.0fs", self.name, self.cooldown_s)
            self._opened_at, self._trial = self.clock(), False

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        self._before()
        try:
            result = await fn()
        except Exception:
            self._failure()
            raise
        except BaseException:  # cancelled: says nothing about the dependency
            self._trial = False
            raise
        self._success()
        return result
