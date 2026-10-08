from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis
import pytest
from pydantic import SecretStr

from orchestrator.api.security import RateLimiter, RedisRateLimiter, build_limiter
from orchestrator.catalog import Catalog
from orchestrator.checkpoint import Checkpointer
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

# --- distributed rate limiting (Redis) -------------------------------------------------


async def test_budget_is_shared_across_replicas() -> None:
    server = fakeredis.FakeServer()
    replica_a = RedisRateLimiter(fakeredis.FakeAsyncRedis(server=server), per_minute=3)
    replica_b = RedisRateLimiter(fakeredis.FakeAsyncRedis(server=server), per_minute=3)
    results = [
        await replica_a.allow("acme"),
        await replica_b.allow("acme"),
        await replica_a.allow("acme"),
        await replica_b.allow("acme"),  # 4th request, whichever replica: over budget
    ]
    assert results == [True, True, True, False]
    assert await replica_b.allow("globex")  # budgets are per tenant
    await replica_a.close()


async def test_redis_bucket_refills_and_expires() -> None:
    redis = fakeredis.FakeAsyncRedis()
    limiter = RedisRateLimiter(redis, per_minute=1)
    assert await limiter.allow("t")
    assert not await limiter.allow("t")
    key = limiter.prefix + "t"
    assert 0 < await redis.pttl(key) <= 61_000  # idle buckets do not pile up
    # Pretend the last refill was a minute ago.
    ts = int(await redis.hget(key, "ts"))
    await redis.hset(key, "ts", ts - 60_000)
    assert await limiter.allow("t")


class _BrokenRedis:
    def register_script(self, script: str) -> Any:
        async def run(**kwargs: Any) -> int:
            raise ConnectionError("redis down")

        return run

    async def aclose(self) -> None:
        return None


async def test_redis_outage_degrades_to_a_local_bucket(caplog: pytest.LogCaptureFixture) -> None:
    # Default policy (A30): no outage of the API, and no unlimited spend either.
    limiter = RedisRateLimiter(_BrokenRedis(), per_minute=2)
    assert [await limiter.allow("t") for _ in range(3)] == [True, True, False]
    assert "rate limiter unavailable (ConnectionError); policy local" in caplog.text


@pytest.mark.parametrize(("policy", "allowed"), [("open", True), ("closed", False)])
async def test_redis_outage_explicit_policies(policy: str, allowed: bool) -> None:
    limiter = RedisRateLimiter(_BrokenRedis(), per_minute=1, on_outage=policy)
    assert [await limiter.allow("t") for _ in range(3)] == [allowed] * 3


def test_prod_refuses_an_ephemeral_profile(settings: Settings, catalog: Catalog) -> None:
    # A33: defaults that lose state on restart (or split it between replicas) are refused.
    settings.app_env = "prod"
    with pytest.raises(RuntimeError) as exc:
        Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    message = str(exc.value)
    for problem in (
        "DATABASE_URL is SQLite",
        "CHECKPOINTER_BACKEND=memory",
        "VECTOR_BACKEND=memory",
        "RATE_LIMIT_BACKEND=memory",
    ):
        assert problem in message
    settings.prod_allow_ephemeral = True  # explicit acceptance for a demo...
    settings.database_url = SecretStr("postgresql+psycopg://u:p@db/agency?sslmode=require")
    with pytest.raises(ValueError, match="PSEUDONYM_KEY"):  # ...that still needs its keys
        Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    settings.pseudonym_key = SecretStr("k" * 48)
    Orchestrator(settings, catalog=catalog, llm=FakeLLM())  # ...and Postgres


def test_build_limiter(settings: Settings) -> None:
    assert isinstance(build_limiter(settings), RateLimiter)
    settings.rate_limit_backend = "redis"
    with pytest.raises(ValueError, match="REDIS_URL"):
        build_limiter(settings)
    settings.redis_url = SecretStr("redis://localhost:6379/0")
    assert isinstance(build_limiter(settings), RedisRateLimiter)  # connects lazily


# --- Postgres TLS in prod ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("postgresql://u:p@db:5432/agency", False),
        ("postgresql://u:p@db:5432/agency?sslmode=prefer", False),
        ("postgresql://u:p@db:5432/agency?sslmode=disable", False),
        ("postgresql://u:p@db:5432/agency?sslmode=require", True),
        ("postgresql://u:p@db:5432/agency?sslmode=verify-full", True),
        ("host=db dbname=agency sslmode=verify-ca", True),
    ],
)
async def test_prod_requires_encrypted_postgres(settings: Settings, url: str, ok: bool) -> None:
    settings.app_env = "prod"
    settings.checkpointer_backend = "postgres"
    settings.postgres_url = SecretStr(url)
    if ok:
        Checkpointer(settings)
    else:
        with pytest.raises(ValueError, match="sslmode"):
            Checkpointer(settings)


async def test_insecure_postgres_needs_explicit_opt_out(settings: Settings) -> None:
    settings.checkpointer_backend = "postgres"
    settings.postgres_url = SecretStr("postgresql://u:p@db/agency")
    Checkpointer(settings)  # dev: allowed
    settings.app_env = "prod"
    settings.postgres_allow_insecure = True
    Checkpointer(settings)  # prod with an explicit, documented opt-out


# --- erasure and retention -------------------------------------------------------------


async def _chat(orch: Orchestrator, tenant: str, thread: str) -> None:
    await orch.chat("deploy with docker", tenant=tenant, thread_id=thread)


async def test_delete_thread_is_scoped_to_the_tenant(orchestrator: Orchestrator) -> None:
    await _chat(orchestrator, "acme", "t1")
    await _chat(orchestrator, "globex", "t1")
    assert await orchestrator.delete_thread("acme", "t1")
    assert not await orchestrator.delete_thread("acme", "t1")
    keys = [k async for k, _ in orchestrator.checkpointer.threads()]
    assert keys == ["globex:t1"]


async def test_delete_tenant_threads_does_not_touch_prefix_lookalikes(
    orchestrator: Orchestrator,
) -> None:
    await _chat(orchestrator, "acme", "a")
    await _chat(orchestrator, "acme", "b")
    await _chat(orchestrator, "acme-eu", "a")  # shares the "acme" prefix, not "acme:"
    assert await orchestrator.delete_tenant_threads("acme") == 2
    assert [k async for k, _ in orchestrator.checkpointer.threads()] == ["acme-eu:a"]


async def test_follow_up_after_erasure_starts_fresh(
    orchestrator: Orchestrator, fake_llm: FakeLLM
) -> None:
    await _chat(orchestrator, "acme", "t")
    await orchestrator.delete_thread("acme", "t")
    await _chat(orchestrator, "acme", "t")
    assert [m["role"] for m in fake_llm.calls[-1]] == ["system", "user"]


async def test_purge_deletes_only_inactive_threads(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _chat(orchestrator, "acme", "old")
    await _chat(orchestrator, "acme", "new")
    real = orchestrator.checkpointer.threads
    now = datetime.now(UTC)
    ages = {"acme:old": now - timedelta(days=100), "acme:new": now - timedelta(days=1)}

    async def aged() -> Any:
        async for key, _ in real():
            yield key, ages[key]

    monkeypatch.setattr(orchestrator.checkpointer, "threads", aged)
    assert await orchestrator.purge_threads(timedelta(days=90)) == 1
    monkeypatch.setattr(orchestrator.checkpointer, "threads", real)
    assert [k async for k, _ in orchestrator.checkpointer.threads()] == ["acme:new"]


async def test_threads_report_real_checkpoint_times(orchestrator: Orchestrator) -> None:
    before = datetime.now(UTC)
    await _chat(orchestrator, "acme", "t")
    [(key, ts)] = [item async for item in orchestrator.checkpointer.threads()]
    assert key == "acme:t" and ts is not None
    assert before - timedelta(seconds=5) <= ts <= datetime.now(UTC)


def test_cli_retention_needs_no_model_provider(
    settings: Settings, catalog: Catalog, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    # A31: the scheduled job starts the databases only. Any embedding or indexing call
    # (a paid provider in prod) would fail this test.
    from orchestrator import cli
    from orchestrator.embeddings import HashingEmbedder
    from orchestrator.router import Router

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the retention job must not call models or index the catalog")

    monkeypatch.setattr(HashingEmbedder, "embed", forbidden)
    monkeypatch.setattr(Router, "build_index", forbidden)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("orchestrator.service.load_catalog", lambda _path: catalog, raising=True)
    settings.thread_retention_days = 30
    assert cli.main(["retention", "--dry-run"]) == 0
    assert cli.main(["purge-threads", "--older-than-days", "7"]) == 0  # the old name
    out = capsys.readouterr().out
    assert '"dry_run": true' in out and '"older_than_days": 30' in out
    assert '"older_than_days": 7' in out and '"knowledge_leftover_versions": 0' in out


async def test_retention_dry_run_only_counts(orchestrator: Orchestrator) -> None:
    await orchestrator.chat("deploy with docker", tenant="acme", thread_id="old")
    counted = await orchestrator.retention(timedelta(seconds=-1), dry_run=True)
    assert counted["threads"] == 1
    assert await orchestrator.checkpointer.exists("acme:old")
    done = await orchestrator.retention(timedelta(seconds=-1))
    assert done["threads"] == 1 and not await orchestrator.checkpointer.exists("acme:old")


def test_cli_audit_anchor_and_verify(
    settings: Settings,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    tmp_path: Any,
) -> None:
    from orchestrator import cli

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("orchestrator.service.load_catalog", lambda _path: catalog, raising=True)
    assert cli.main(["audit-anchor"]) == 0  # a fresh database: no chains yet
    assert cli.main(["audit-verify"]) == 0
    # An anchor the database no longer backs (e.g. the log was replaced): exit 1.
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text('{"tenant": "acme", "seq": 3, "hash": "' + "a" * 64 + '"}\n', "utf-8")
    assert cli.main(["audit-verify", "--anchors", str(anchors)]) == 1
    assert "shorter than an external anchor" in capsys.readouterr().out
