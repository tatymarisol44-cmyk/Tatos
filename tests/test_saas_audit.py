"""Reproductions from the SaaS audit: replicas, untrusted peers and interrupted I/O."""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.api.security import RateLimiter
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.media_store import GcsMediaStore, LocalMediaStore
from orchestrator.remote import A2AClient, RemoteAgentError, card_to_spec, discover_all
from orchestrator.service import Orchestrator


async def test_sign_in_budget_is_shared_by_api_replicas(
    settings: Settings, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.asyncio import Redis

    server = fakeredis.FakeServer()
    monkeypatch.setattr(Redis, "from_url", lambda *a, **kw: fakeredis.FakeAsyncRedis(server=server))
    settings.rate_limit_backend = "redis"
    settings.redis_url = SecretStr("redis://synthetic.test")
    settings.auth_rate_per_minute = 2
    apps = [
        create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
        for _ in range(2)
    ]
    try:
        results = [
            await apps[0].state.auth_limiter.allow("auth:client"),
            await apps[1].state.auth_limiter.allow("auth:client"),
            await apps[0].state.auth_limiter.allow("auth:client"),
        ]
        assert results == [True, True, False]
        assert await apps[0].state.limiter.allow("auth:client")
    finally:
        for app in apps:
            await app.state.limiter.close()
            await app.state.auth_limiter.close()


async def test_public_rate_limit_cannot_grow_memory_without_bound() -> None:
    limiter = RateLimiter(1)
    for i in range(10_001):
        await limiter.allow(f"client-{i}")
    assert len(limiter._buckets) <= 10_000
    assert not await limiter.allow("client-0")  # pressure must not reset existing budgets


@pytest.mark.parametrize("skills", [42, [{"name": "Skill", "tags": 42}]])
async def test_malformed_remote_card_is_skipped_not_a_startup_crash(
    settings: Settings, skills: Any
) -> None:
    settings.remote_discovery_attempts = 1
    card = {"name": "Synthetic agent", "description": "Synthetic description", "skills": skills}
    client = A2AClient(settings, httpx.MockTransport(lambda req: httpx.Response(200, json=card)))
    try:
        assert await discover_all(client, ["https://synthetic.test"]) == []
    finally:
        await client.close()


@pytest.mark.parametrize(
    "result",
    [
        {"kind": "message", "parts": 42},
        {"kind": "task", "artifacts": 42},
        {"kind": "task", "artifacts": [{"parts": 42}]},
        {"kind": "task", "status": {"message": {"parts": 42}}},
    ],
)
async def test_malformed_remote_reply_is_a_controlled_failure(
    settings: Settings, result: dict[str, Any]
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        envelope = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": envelope["id"], "result": result})

    client = A2AClient(settings, httpx.MockTransport(reply))
    spec = card_to_spec("https://synthetic.test", {"name": "Synthetic", "description": "Agent"})
    try:
        with pytest.raises(RemoteAgentError):
            await client.send(spec, "Synthetic question", "synthetic-context")
    finally:
        await client.close()


async def test_interrupted_bucket_download_does_not_poison_the_cache(tmp_path: Path) -> None:
    calls = 0

    def download(filename: str) -> None:
        nonlocal calls
        calls += 1
        Path(filename).write_bytes(b"partial" if calls == 1 else b"complete")
        if calls == 1:
            raise ConnectionError("synthetic interrupted download")

    blob = SimpleNamespace(download_to_filename=download)
    bucket = SimpleNamespace(blob=lambda name: blob)
    store = GcsMediaStore(
        "synthetic",
        timedelta(minutes=1),
        tmp_path,
        client=SimpleNamespace(bucket=lambda name: bucket),
    )
    with pytest.raises(ConnectionError):
        await store.local_file("acme/publication.jpg")
    assert not (tmp_path / "acme/publication.jpg").exists()
    cached = await store.local_file("acme/publication.jpg")
    assert cached is not None and cached.read_bytes() == b"complete"
    assert calls == 2


async def test_interrupted_local_copy_preserves_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import media_store

    root = tmp_path / "cache"
    target = root / "acme/publication.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous complete image")
    source = tmp_path / "source.jpg"
    source.write_bytes(b"new complete image")

    def broken_copy(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(b"partial")
        raise OSError("synthetic interrupted copy")

    monkeypatch.setattr(media_store.shutil, "copyfile", broken_copy)
    with pytest.raises(OSError):
        await LocalMediaStore(root).put("acme/publication.jpg", source, "image/jpeg")
    assert target.read_bytes() == b"previous complete image"


def test_a2a_replies_with_tenant_data_cannot_be_cached(
    settings: Settings, catalog: Catalog
) -> None:
    app = create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
    with TestClient(app) as client:
        response = client.post("/a2a", json={}, headers={"X-API-Key": "test-key"})
        assert response.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize(
    ("target", "result", "ok"),
    [
        ("", "skipped", True),
        ("render", "success", True),
        ("render", "skipped", False),
        ("render", "failure", False),
        ("render", "cancelled", False),
        ("typo", "success", False),
        ("vm", "success", True),
        ("gke", "success", True),
    ],
)
def test_deployment_summary_never_calls_a_skipped_job_deployed(
    target: str, result: str, ok: bool
) -> None:
    import runpy

    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/ci/deploy_summary.py"))
    message, passed = module["summarize"](
        target, {"RENDER_RESULT": result, "VM_RESULT": result, "GKE_RESULT": result}
    )
    assert passed is ok
    if not target or not ok:
        assert "NOT deployed" in message
    else:
        assert "completed successfully" in message


def test_public_responses_identify_the_running_commit(settings: Settings, catalog: Catalog) -> None:
    settings.build_revision = "abcdef0123456789"
    app = create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
    with TestClient(app) as client:
        for path in ["/healthz", "/readyz", "/app", "/static/consultorio.js"]:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["x-app-revision"] == settings.build_revision


async def test_a_long_run_does_not_lose_its_conversation_lease(orchestrator: Orchestrator) -> None:
    import asyncio

    from orchestrator.governance import ThreadBusyError

    orchestrator.leases.ttl = timedelta(milliseconds=600)
    async with orchestrator._lease("acme", "long-run"):
        await asyncio.sleep(1.4)
        with pytest.raises(ThreadBusyError):
            await orchestrator.leases.acquire("acme", "long-run", "another-replica")


async def test_a_long_stream_keeps_its_lease_while_waiting_for_the_model(
    orchestrator: Orchestrator, fake_llm: FakeLLM
) -> None:
    import asyncio

    from orchestrator.governance import ThreadBusyError

    orchestrator.leases.ttl = timedelta(milliseconds=600)
    fake_llm.latency_s = 1.8
    events = await orchestrator.chat_stream("deploy docker", tenant="acme", thread_id="long-stream")

    async def consume() -> None:
        async for _ in events:
            pass

    reader = asyncio.create_task(consume())
    try:
        await asyncio.sleep(1.4)
        assert not reader.done()
        with pytest.raises(ThreadBusyError):
            await orchestrator.leases.acquire("acme", "long-stream", "another-replica")
    finally:
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader


async def test_losing_a_lease_cancels_the_old_owner_without_deleting_the_new_one(
    orchestrator: Orchestrator,
) -> None:
    import asyncio

    from sqlalchemy import update

    from orchestrator.db import utcnow
    from orchestrator.governance import ThreadBusyError, thread_leases

    orchestrator.leases.ttl = timedelta(milliseconds=150)
    with pytest.raises(ThreadBusyError, match="lease lost"):
        async with orchestrator._lease("acme", "stolen-lease"):
            async with orchestrator.db.engine.begin() as connection:
                await connection.execute(
                    update(thread_leases)
                    .where(thread_leases.c.thread_id == "stolen-lease")
                    .values(holder="new-owner", expires_at=utcnow() + timedelta(minutes=1))
                )
            await asyncio.sleep(1)
    assert await orchestrator.leases.active("acme", "stolen-lease")
    await orchestrator.leases.release("acme", "stolen-lease", "new-owner")


async def test_recovery_does_not_reset_a_live_slow_review(orchestrator: Orchestrator) -> None:
    from sqlalchemy import update

    from orchestrator.db import utcnow
    from orchestrator.governance import reviews

    result = await orchestrator.chat(
        "hello", tenant="acme", thread_id="live-review", force_review=True
    )
    assert await orchestrator.reviews.claim("acme", result.thread_id, {"approved": True})
    async with orchestrator.db.engine.begin() as connection:
        await connection.execute(
            update(reviews)
            .where(reviews.c.thread_id == result.thread_id)
            .values(resolved_at=utcnow() - timedelta(hours=1))
        )
    async with orchestrator._lease("acme", result.thread_id):
        assert await orchestrator.reconcile_reviews() == 0
        review = await orchestrator.reviews.get("acme", result.thread_id)
        assert review is not None and review.status == "resolving"
    assert await orchestrator.reconcile_reviews() == 1


def test_ci_pins_uv_to_the_container_version() -> None:
    import re

    import yaml

    root = Path(__file__).parents[1]
    version = re.search(r"astral-sh/uv:([0-9.]+)@", (root / "Dockerfile").read_text())
    assert version is not None
    for path in (root / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "astral-sh/setup-uv@" in step.get("uses", ""):
                    assert step["with"]["version"] == version[1]
