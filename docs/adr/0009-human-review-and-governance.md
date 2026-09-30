# ADR 0009: Human review (interrupt + checkpoint) and the governance store

**Status:** accepted

## Context

The product now serves regulated businesses, starting with dental clinics. Some answers must not reach a customer without a person's approval: clinical advice, answers from healthcare or finance specialists, or anything produced after a prompt-injection flag. Pearson et al. (arXiv:2607.19297) call human-in-the-loop review "the clearest case for LangGraph": an `interrupt()` is a durable workflow boundary, and the checkpointer that already persists threads (ADR 0005) lets a paused answer wait for hours and survive restarts.

GDPR Art. 22 also restricts decisions with significant effects taken solely by automated means, and HIPAA requires audit controls (45 CFR 164.312(b)).

## Decision

- **Industry packs** (`src/orchestrator/pack_data/*.yaml`) say what needs review: catalog divisions (`dental`: healthcare, finance, paid-media) and whether clinical advice does. Tenants map to packs with `TENANT_PACKS`. What changes between business types is configuration, not code.
- **`risk_score`** (after the output guard) applies deterministic rules (`risk.py`): pack divisions, clinical patterns in EN/ES, a flagged prompt injection, or `force_review`. The reasons are returned verbatim, so a reviewer or an auditor knows why an answer was held.
- **`review`** calls `interrupt()` with the draft, risk, sources and evidence. The response comes back with `status: "pending_review"` and `answer: null`: nothing reaches the end user before a decision. The thread refuses new messages (HTTP 409), because a new input would start a fresh run and orphan the paused one.
- **Resume** (`POST /v1/reviews/{thread_id}`, MCP `resolve_review`) claims the review atomically (so it is applied once), then resumes with `Command(resume=decision)`. If resuming fails, the review is re-opened. An approval can carry an edited answer, which goes through the same output guard as model text. A rejection returns a neutral "withheld" message, and the conversation history keeps what the user was shown, never the rejected draft.
- **Governance store** (`db.py`, `governance.py`): SQLAlchemy async Core with one metadata and engine, on SQLite in memory for dev/tests and Postgres (TLS required in prod) otherwise. Tables: `audit_events` (append-only: who, what, which subject; metadata only, never conversation text), `reviews` and `consents`. Audit records are kept apart from conversations, which are purged after `THREAD_RETENTION_DAYS`.
- The acting person is the `X-Actor` header, declared by the calling application (which authenticates its own users) and recorded in every audit event.

## Consequences

- Reviews add latency measured in human time, by design. The latency metric still measures machine time.
- The same review queue is reachable from REST, MCP (from the developer's editor) and A2A (a remote caller gets a neutral "under review" reply).
- `X-Actor` is trusted as declared. Per-user SSO/OIDC in the orchestrator itself is future work; until then, the API key identifies the application and the application vouches for the person.
- Schema changes rely on `create_all` (idempotent). A migration tool (Alembic) is needed before the first breaking schema change in production.
