"""Isolated SaaS benchmark: loopback only, synthetic tenants, fake model, no credentials.

Run with the project Python: python loadtest/audit_local.py --output results.json
This measures a single process with SQLite, not production or multi-replica capacity.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import math
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-local-audit-key"


def resources() -> dict[str, float]:
    rss = 0.0
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in [
                    "peak_rss",
                    "rss",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                ]
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        counts = Counters()
        counts.cb = ctypes.sizeof(counts)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counts), counts.cb):
            rss = counts.rss / 1024 / 1024
    else:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
            1024 if sys.platform != "darwin" else 1024 * 1024
        )
    return {"rss_mb": round(rss, 2), "cpu_seconds": time.process_time()}


def serve(port: int) -> None:
    import uvicorn

    from orchestrator.api.app import create_app
    from orchestrator.config import Settings

    settings = Settings(
        _env_file=None,
        app_env="test",
        api_keys=f"{KEY}:synthetic-audit",
        agents_dir=ROOT / "vendor/agency-agents",
        llm_backend="fake",
        fake_llm_latency_ms=50,
        embedding_backend="hashing",
        vector_backend="memory",
        checkpointer_backend="memory",
        database_url="sqlite+aiosqlite:///:memory:",
        rate_limit_backend="memory",
        rate_limit_per_minute=100_000,
        outbox_interval_seconds=0,
        remote_agents=[],
        gcs_bucket=None,
        otel_enabled=False,
        metrics_port=0,
        router_use_llm=False,
    )
    app = create_app(settings)

    @app.get("/_audit_resources", include_in_schema=False)
    async def sample() -> dict[str, Any]:
        return {**resources(), "agents": len(app.state.orchestrator.catalog)}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error", access_log=False)


def percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return (
        {
            f"p{n}_ms": round(ordered[max(0, math.ceil(len(ordered) * n / 100) - 1)], 2)
            for n in [50, 95, 99]
        }
        if ordered
        else {}
    )


async def phase(base: str, concurrency: int, seconds: float) -> dict[str, Any]:
    timings: dict[str, list[float]] = {}
    statuses: Counter[str] = Counter()
    async with httpx.AsyncClient(
        base_url=base,
        timeout=30,
        headers={"X-API-Key": KEY},
        limits=httpx.Limits(max_connections=64),
    ) as client:
        before = (await client.get("/_audit_resources")).json()
        started = time.perf_counter()
        deadline = started + seconds

        async def user(index: int) -> None:
            sequence = index
            while time.perf_counter() < deadline:
                kind = sequence % 10
                path = (
                    "/v1/chat"
                    if kind < 6
                    else "/v1/insights/summary"
                    if kind < 8
                    else "/v1/crm/appointments"
                    if kind == 8
                    else "/v1/agents"
                )
                sequence += 1
                began = time.perf_counter()
                try:
                    if path == "/v1/chat":
                        response = await client.post(
                            path,
                            json={
                                "question": "Improve a clinic website",
                                "thread_id": f"audit-{uuid.uuid4().hex}",
                            },
                        )
                    else:
                        response = await client.get(path)
                    statuses[str(response.status_code)] += 1
                except httpx.HTTPError as exc:
                    statuses[type(exc).__name__] += 1
                timings.setdefault(path, []).append((time.perf_counter() - began) * 1000)

        await asyncio.gather(*(user(i) for i in range(concurrency)))
        elapsed = time.perf_counter() - started
        after = (await client.get("/_audit_resources")).json()
        chain = (await client.get("/v1/audit/verify")).json()
    all_times = [v for group in timings.values() for v in group]
    return {
        "concurrency": concurrency,
        "elapsed_seconds": round(elapsed, 3),
        "requests": len(all_times),
        "statuses": dict(statuses),
        "requests_per_second": round(len(all_times) / elapsed, 2),
        **percentiles(all_times),
        "rss_mb": after["rss_mb"],
        "cpu_one_core_percent": round(
            (after["cpu_seconds"] - before["cpu_seconds"]) / elapsed * 100, 2
        ),
        "agents": after["agents"],
        "audit_chain": chain,
        "by_endpoint": {k: {"requests": len(v), **percentiles(v)} for k, v in timings.items()},
    }


def source_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--concurrency", default="1,8,32")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/audit-load-2026-10-19.json")
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
        return 0
    levels = [int(n) for n in args.concurrency.split(",")]
    if (
        not 1 <= args.seconds <= 60
        or not levels
        or len(levels) > 4
        or any(not 1 <= n <= 64 for n in levels)
    ):
        parser.error("Use 1-60 seconds and up to four concurrency levels of 1-64.")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    report: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "logical_cpus": os.cpu_count(),
        "source_sha256": source_hash(),
        "method": (
            "loopback HTTP, one uvicorn process, full catalog, SQLite in memory, "
            "fake LLM 50ms, shared host with load generator"
        ),
        "phases": [],
    }
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as logs:
        child = subprocess.Popen(  # noqa: S603 - this script, fixed interpreter and loopback port
            [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)],
            stdout=logs,
            stderr=logs,
            cwd=ROOT,
        )
        try:
            for _ in range(120):
                if child.poll() is not None:
                    logs.seek(0)
                    raise RuntimeError("Synthetic server failed: " + logs.read()[-3000:])
                try:
                    if httpx.get(base + "/readyz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("Synthetic server did not become ready")
            for level in levels:
                result = asyncio.run(phase(base, level, args.seconds))
                report["phases"].append(result)
                print(json.dumps(result), flush=True)
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return (
        0
        if all(
            set(p["statuses"]) <= {"200"} and p["audit_chain"].get("ok") is True
            for p in report["phases"]
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
