# Load test: orchestrator overhead, one vs two processes

**Date:** 2026-10-01 · **Commit:** the one adding this file · **Script:** [`loadtest/locustfile.py`](../loadtest/locustfile.py)

## What was measured

This test measures the cost of the orchestrator itself:
- the API, guardrails and routing;
- the LangGraph graph, with every checkpoint written to Postgres;
- the audit chain and the CRM SQL.

It does **not** measure a model provider. `LLM_BACKEND=fake` answers instantly, so in production each chat turn would add the provider's latency on top of these numbers. That provider latency is usually seconds and dominates. The test answers two questions:
- How much latency and capacity does our own code add?
- Does the service scale when it gets more processes?

## Setup

- **Machine:** one laptop, AMD Ryzen (Zen 3), 12 logical cores, Windows 11. Locust ran on the same machine and competed with the server for CPU.
- **Database:** Postgres 17.7, local (portable binaries).
  - `DATABASE_URL`: business data and audit chain.
  - `CHECKPOINTER_BACKEND=postgres`: conversations.
  - Schema migrated with Alembic.
- **Vectors and rate limit:** `VECTOR_BACKEND=memory` and `EMBEDDING_BACKEND=hashing`. The rate limit was set high enough never to trigger.
- **Server:** `agency serve --workers N`, uvicorn.
- **Load:** 40 concurrent users, ramping up at 10 per second, for 60 s, each waiting 0.5–2 s between requests. The task mix per user, with a new conversation on every chat:

| Request | Share of tasks |
|---|---|
| `POST /v1/chat` | 60 % |
| `GET /v1/insights/summary` | 20 % |
| `GET /v1/crm/appointments` | 10 % |
| `GET /v1/agents` | 10 % |

## Results

**Baseline, 1 user** (no queueing): chat p50 **81 ms**, insights 15 ms, appointments 4 ms.

**40 users:**

| Workers | Request | n | Failures | req/s | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| 1 | `POST /v1/chat` | 619 | 0 | 10.6 | 1 400 | 3 100 | 3 400 |
| 1 | `GET /v1/insights/summary` | 232 | 0 | 4.0 | 39 | 140 | 220 |
| 1 | `GET /v1/crm/appointments` | 92 | 0 | 1.6 | 7 | 70 | 140 |
| 1 | **all** | 1 051 | **0** | **18.0** | 760 | 2 500 | 3 400 |
| 2 | `POST /v1/chat` | 959 | 0 | 16.1 | 290 | 920 | 1 200 |
| 2 | `GET /v1/insights/summary` | 325 | 0 | 5.5 | 27 | 100 | 210 |
| 2 | `GET /v1/crm/appointments` | 143 | 0 | 2.4 | 7 | 62 | 1 100 |
| 2 | **all** | 1 571 | **0** | **26.5** | 130 | 830 | 1 100 |

**Integrity after the run:**
- `agency audit-verify` reports the tenant's chain `ok` across 1 748 events, written concurrently by two processes.
- 1 628 conversations are stored in Postgres.

## Reading the numbers

- **The bottleneck is CPU, not I/O.** A chat turn needs about 80 ms of Python CPU: guardrails, routing over 260 agents, graph steps and serialising checkpoints. One process saturates at about 10 chat turns per second. Beyond that, requests queue, which is the 1.4 s p50 with 1 worker.
- **More processes scale it.** With a second process (all else equal), throughput rose 47 % and the chat p95 fell from 3.1 s to 0.9 s. It is not 2× here, because Locust shared the same machine. Kubernetes scales the same way with more pods: the HPA, Redis for the shared rate limit, and Postgres for shared conversations.
- **Every pod needs to be able to answer any request.** The test ran without session affinity. Two processes served one tenant with no lost conversation and no break in the audit chain. The Postgres concurrency tests check the same property as invariants.

## Not covered yet

- **Real model latency.** The nightly eval with real models measures quality, not load. A short load test against a provider sandbox would show how chat concurrency interacts with the provider's own rate limits and timeouts.
- **Qdrant** instead of in-memory vectors, and **Redis** for the shared limiter under load.
- **A run on Kubernetes** with the HPA, measuring scale-out time.
- **Longer soak tests** to catch memory growth or connection-pool exhaustion.

## Reproduce

```bash
# server (Postgres reachable at DATABASE_URL / POSTGRES_URL)
LLM_BACKEND=fake EMBEDDING_BACKEND=hashing VECTOR_BACKEND=memory \
CHECKPOINTER_BACKEND=postgres DB_AUTO_MIGRATE=true API_KEYS=load-key:clinic \
RATE_LIMIT_PER_MINUTE=10000000 TENANT_PACKS='{"clinic": "dental"}' \
uv run agency serve --workers 2
# load
uv run --group load locust -f loadtest/locustfile.py --headless -u 40 -r 10 -t 60s \
  --host http://127.0.0.1:8000 --csv out/run
```
