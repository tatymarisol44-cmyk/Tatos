# ADR 0005: Durable conversation state with a Postgres checkpointer

**Status:** accepted

## Context

LangGraph saves the graph state after every node (a *checkpoint*) under the request's `thread_id`. Follow-up questions read it back, so a conversation keeps its history across single and team turns. Until now the checkpointer was `InMemorySaver`:

- a restart or a rolling deploy lost every conversation;
- with several replicas, a follow-up that hit a different pod started from zero. The Service used `sessionAffinity: ClientIP` as a stopgap, which breaks behind NAT or proxies and unbalances load.

The API must be stateless so the HPA can add and remove pods freely.

## Options

1. **Redis checkpointer.** Very fast, but durability depends on AOF/RDB configuration, and conversation history is data tenants expect to keep (and to be able to delete).
2. **Postgres checkpointer.** *Chosen.* Durable by default and transactional. It is the backend LangGraph maintains for production, and every cloud offers it managed (RDS, Cloud SQL, Azure Database).
3. **Keep memory + sticky sessions.** No new dependency, but it does not survive restarts and limits scaling.

## Decision

- A `Checkpointer` wrapper (`src/orchestrator/checkpoint.py`) picks the backend from `CHECKPOINTER_BACKEND=memory|postgres`. Memory stays the default so tests and local dev need no database.
- Postgres uses `AsyncPostgresSaver` over a `psycopg_pool.AsyncConnectionPool` (`POSTGRES_POOL_SIZE`). The pool is created closed and opened in `Orchestrator.start()`, which also runs `setup()`, an idempotent table migration. `Orchestrator.close()` releases it on shutdown (API lifespan, CLI).
- Connections use `autocommit=True` and `prepare_threshold=0`, so they also work behind PgBouncer in transaction mode.
- Thread keys stay `"{tenant}:{thread_id}"`, so tenants cannot read each other's threads even if they choose the same id.

## Consequences

- Any replica can continue any thread, and conversations survive restarts and deploys. A CI integration test proves it: a second `Orchestrator` instance, sharing only the database, sees the first one's history.
- Postgres becomes a runtime dependency in docker-compose and k8s (`POSTGRES_URL` via the secret). Startup fails fast if it is unreachable.
- Each node adds a write per checkpoint. That is milliseconds, against LLM calls that take seconds.
- Threads grow without bound. A retention job (for example `adelete_thread` for threads older than N days, or on tenant request for GDPR) is future work.
- Rate limiting was still per replica, so session affinity stayed until the limiter moved to Redis (the Kubernetes base now deploys it; only the dev overlay, with in-memory threads, keeps affinity).
