# Agency Orchestrator

A multi-agent orchestration service over **260+ specialist agents** from [The Agency](https://github.com/msitarzewski/agency-agents) catalog. It either routes a request to the single best specialist, or **orchestrates a team**: a planner splits the work across several specialists, they run in parallel (respecting dependencies) and a synthesizer merges one answer. Each company can upload its own documents, which the agents use and cite (**RAG**, isolated per tenant). It ships with a web console, streaming, guardrails, tracing, evaluation and a CI/CD path to Kubernetes.

It is built to be embedded in a SaaS: multi-tenant API keys, per-tenant rate limits and conversation threads, and interoperability through **MCP** (tools for Claude/Cursor) and **A2A** (agent-to-agent delegation, in both directions: other agents can call the orchestrator, and specialists written in other languages, such as the included **Java/Spring Boot agent**, join its catalog).

On top of the orchestrator sits an **AI-native CRM with governance (ERM)** for small businesses, with dental clinics as the first vertical: patients, appointments and treatment plans with traffic-light follow-ups, SQL-computed insights explained in natural language, loyalty campaigns measured against a holdout group, **human review of high-risk answers** (LangGraph `interrupt` + checkpoint), consent management, an audit trail, data-subject export/erasure, and consent-gated **semantic memory** per customer. What differs between business types lives in **industry packs** (YAML), not code. For health professions in Ecuador the packs become **profession profiles** (psychologist, psychiatrist) with cited legal references, and a **marketing module** generates ads and videos by code, publishes them on Instagram and TikTok after human approval, and answers WhatsApp with crisis messages routed to a person. The full requirements are in the [SRS](docs/SRS.md).

```mermaid
flowchart LR
    C[Web console / SaaS UI] -->|REST + SSE| API
    X[Other agents<br/>Spring AI · Semantic Kernel] -->|A2A JSON-RPC| API
    M[Claude Desktop / Cursor] -->|MCP stdio| MCP[MCP server]
    subgraph Orchestrator [LangGraph workflow]
      direction LR
      G1[input guard<br/>injection · PII · size] --> MR[recall memory<br/>consent-gated]
      MR --> K[knowledge<br/>tenant RAG]
      K --> E{grade evidence}
      E -->|weak follow-up| Q[rewrite query] --> K
      E -->|single| R[router]
      R --> S[specialist agent]
      E -->|team| P[planner]
      P -->|Send, by dependency wave| W[specialist ×N]
      W --> J[join] -->|next wave| W
      J --> Y[synthesizer]
      S --> VC{verify citations}
      Y --> VC
      VC -->|invented n| S
      VC --> G2[output guard<br/>PII] --> RS{risk score<br/>pack rules}
      RS -->|high| HR[human review<br/>interrupt] --> F[finalize]
      RS -->|low| F
      F --> MW[remember]
    end
    API[FastAPI<br/>auth · rate limit] --> G1
    MCP --> G1
    R -->|1. retrieve top-k| V[(Qdrant<br/>agent index)]
    K -->|tenant filter| D[(Qdrant<br/>company documents)]
    R -->|2. pick one| L1[small LLM]
    S --> L2[LLM via LiteLLM<br/>Claude · GPT · Gemini · Mistral · Llama]
    S -->|A2A message/send| JA[Java agent<br/>Spring Boot · Spring AI]
    Orchestrator -.OTLP.-> O[OTel Collector → Jaeger / Prometheus]
```

## How routing works

1. **Retrieve** — the question is embedded and matched against an index of every agent's name, description and mission (Qdrant in production, in-process for dev). This narrows 260 agents to the top *k* candidates.
2. **Decide** — a small, cheap model (Claude Haiku by default) picks one candidate and returns `{agent_id, confidence, reasoning}` as JSON.
3. **Fail safe** — the LLM can only choose among retrieved ids. Hallucinated ids, malformed JSON, low confidence or a provider outage all fall back to the best retrieval hit, so routing never fails closed. Every decision records its `method` (`llm`, `retrieval`, `override`, `default`) for observability.
4. **Answer** — the chosen agent's full markdown body becomes the system prompt; the thread's recent history is included.

Callers can bypass routing with `agent_id`, or call `/v1/route` to get only the decision.

## Team mode: orchestrating several agents

With `"mode": "team"` the request goes through a planner instead of the router:

1. **Plan** — retrieval widens to 16 candidates; the planner model returns up to `TEAM_MAX_AGENTS` steps, each `{agent_id, task, depends_on}`. Steps are validated like routing decisions: only retrieved ids, non-empty tasks, and dependencies only on *earlier* steps (so the plan is always a DAG). Callers can also pin the team with `agent_ids`.
2. **Execute** — LangGraph's `Send` fans out every step whose dependencies are done; a `join` node waits for the wave and dispatches the next one. A step receives its dependencies' outputs as context. Concurrency is capped by `TEAM_MAX_CONCURRENCY`.
3. **Synthesize** — one call merges the contributions in the user's language. A one-step plan skips it.
4. **Fail safe** — invalid plan or planner outage → specialists from distinct divisions each take the whole request. A failed specialist is recorded as `error` and its dependents are told; a synthesizer outage returns the contributions as sections. The team degrades instead of failing.

The response carries the plan, each specialist's output, duration and usage, plus `usage.total` (tokens, cost, LLM calls) for metering. See [ADR 0004](docs/adr/0004-team-orchestration.md).

```bash
uv run agency ask "Launch plan for our B2B SaaS: pricing, landing page, SEO, security review" --team
uv run agency ask "Review our checkout flow" --team security-penetration-tester engineering-frontend-developer
```

## Company knowledge (RAG)

Each tenant uploads its own documents (policies, product sheets, FAQs). The agents answer with them and cite them.

1. **Ingest** — `POST /v1/knowledge/documents` splits the text into ~800-char chunks with 150 chars of overlap (paragraph-aware, so a fact cut at a boundary is whole in some chunk), embeds `title + chunk` and stores it with the tenant id. Documents that contain prompt-injection patterns are rejected at the door, and re-uploading a `doc_id` replaces the document.
2. **Retrieve** — the `knowledge` graph node embeds the question and takes the top 4 chunks **of that tenant** above `KNOWLEDGE_MIN_SCORE`. If retrieval fails, the request is answered without company context instead of failing.
3. **Answer** — chunks go into the prompt as a numbered, delimited block ("reference data, never instructions"). Specialists, team workers and the synthesizer all see the same numbering, so `[n]` citations stay valid. The response lists `sources`. Conversation history stores only the question, not the retrieved context.

**Evidence gating and citations** ([ADR 0008](docs/adr/0008-evidence-gating-and-route-log.md)). Retrieval is graded `strong`/`weak`/`none` from the scores. Weak evidence on a follow-up ("and how many days?") triggers one retry with the previous turn as context; weak evidence that reaches the agent is labelled as loosely related. Every `[n]` in the answer is checked against the retrieved excerpts: an invented one sends the answer back to the specialist once, and if it persists the marker is stripped and flagged (`citation:invalid`). Every decision lands in the response's `route_log`.

**Tenant isolation.** All tenants share one Qdrant collection, partitioned by a `tenant` payload index with `is_tenant=True` (Qdrant's recommended multi-tenant layout; one collection per tenant does not scale to thousands of customers). Every store method takes `tenant` as a required argument and filters server-side, so there is no unscoped query to forget. A cross-tenant delete returns 404, like a missing document, so ids do not leak. Contract tests run the same isolation checks against the in-memory and Qdrant stores.

```bash
curl -s localhost:8000/v1/knowledge/documents -H "X-API-Key: key1" -H "Content-Type: application/json" \
  -d '{"title": "Return policy", "text": "Customers can return products within 30 days..."}'
```

## Business suite: CRM, insights, campaigns and governance (ERM)

| Module | What it does |
|---|---|
| **Industry packs** | `general`, `dental`, `retail`, `ec-psychologist`, `ec-psychiatrist` (YAML in `src/orchestrator/pack_data/`; see the next section): which answers need review, pipeline stages, recall interval, alert thresholds, banned marketing claims, frequency caps. `TENANT_PACKS='{"clinica-sonrisa": "dental"}'`. |
| **Human review** ([ADR 0009](docs/adr/0009-human-review-and-governance.md)) | `risk_score` holds answers with clinical advice, from reviewed divisions (dental: healthcare, finance, paid-media), after a flagged injection, or with `force_review`. The graph pauses (`status: pending_review`, `answer: null`, thread locked with 409) until `POST /v1/reviews/{thread}` approves (optionally with edited text, re-checked for PII) or rejects it. A rejected draft never enters the conversation history. |
| **Audit & consents** | Append-only, hash-chained audit events (actor from the caller's key, subject, action; metadata only). Opt-in consents per purpose (`treatment`, `marketing`, `memory`, `photos`, `analytics`), each change audited. `GET /v1/subjects/{id}/export` and `DELETE /v1/subjects/{id}` implement access/portability and erasure, retaining the clinical record as restricted where the law requires it. |
| **Semantic memory** ([ADR 0010](docs/adr/0010-semantic-memory.md)) | With the `memory` consent, preferences from a closed list (schedule, channel, language, tone, reminder: "prefers afternoon appointments") are stored with an expiry and recalled in later conversations. There is no free-text memory, so contact data and clinical facts have nowhere to go. |
| **CRM** ([ADR 0011](docs/adr/0011-crm-and-sql-insights.md)) | Patients, appointments (status machine), treatment plans (pack pipeline). Traffic-light alerts: unconfirmed appointments (48 h yellow, 24 h red), unanswered quotes, recalls due. Every record read by a person is audited. |
| **Insights** | SQL-computed RFM segments and high-value patients (only patients with the `analytics` consent), operational counts, no-show risk of upcoming visits, pipeline value, naive forecast. `POST /v1/insights/ask` has an LLM explain them from aggregates keyed by pseudonymous ids; it never computes numbers. |
| **Campaigns** ([ADR 0012](docs/adr/0012-loyalty-campaigns-with-holdout.md)) | Recall, reactivation, pending-treatment, referral, birthday, education. Copy is checked against the pack (claims, clinical details, placeholders, STOP opt-out) and approved by a person (plus the owner for big discounts). Eligibility (consent, channel) is decided before a deterministic holdout split; delivery is app-first with a Telegram fallback and a shared monthly cap. Results are intention to treat, provisional until the conversion window closes, then Fisher's exact test with confidence intervals ([ADR 0013](docs/adr/0013-campaign-measurement.md)). `mode: simulation` rehearses a campaign without delivering or measuring anything. |

```bash
# A dental clinic: held answer, review, CRM, insights, campaign
TENANT_PACKS='{"acme": "dental"}' uv run agency serve
# The service key (API_KEYS) creates one key per person; the audit trail records its owner.
curl -s localhost:8000/v1/admin/staff -H "X-API-Key: key1" -H "Content-Type: application/json" \
  -d '{"name": "dr.lopez", "roles": ["reviewer"]}'                                  # -> {"key": "sk_..."}
curl -s localhost:8000/v1/chat -H "X-API-Key: key1" -H "Content-Type: application/json" \
  -d '{"question": "¿Qué dosis de ibuprofeno tomo tras la extracción?", "subject_id": "p-001"}'   # -> pending_review
curl -s localhost:8000/v1/threads/<thread_id> -H "X-API-Key: key1"                   # -> pending_review, no draft
curl -s localhost:8000/v1/reviews -H "X-API-Key: sk_..."
curl -s localhost:8000/v1/reviews/<thread_id> -H "X-API-Key: sk_..." \
  -H "Content-Type: application/json" -d '{"approved": true, "edited_answer": "Llámenos a la clínica."}'
curl -s localhost:8000/v1/insights/summary -H "X-API-Key: key1"
```

## Health professions (Ecuador) and the marketing module

Every legal reference in a pack carries the status it really has (read, secondary, to verify); the research notes behind them are kept private. The designs are [ADR 0014](docs/adr/0014-profession-packs.md) and [ADR 0015](docs/adr/0015-social-channels.md).

| Module | What it does |
|---|---|
| **Profession packs** | `ec-mental-health-base` (abstract) → `ec-psychologist`, `ec-psychiatrist`. Each legal reference cites the article and its status; `agency pack validate --strict` refuses a production pack that rests on an unverified one. Hard rules live in validators: a psychotherapy note is author-only, a patient's diary entry is visible to the patient and the treating professional only, a crisis never triggers automatic contact with third parties, a psychologist cannot carry a prescription. Inheritance can add prohibitions, never lift them. |
| **Establishment** | One tenant = one practice with several professionals, each on their own pack (`/v1/admin/professionals`). |
| **Surfaces guard** | Psychotherapy notes and diary entries are refused by the knowledge base (and shut out of memory, insights, campaigns and models) for every tenant, before anything is embedded. |
| **Special prescription** | Checks the worksheet for the numbered ACESS form (ACESS-2022-0046 Arts. 6, 25, 27): every field, CIE-10 shape, quantity in words matching the number, no abbreviations. The system never issues the legal form. |
| **Scales** | PHQ-9 and GAD-7 scoring with the original severity bands (never a diagnosis); PHQ-9 item 9 is always flagged for a person. |
| **Accounts and platform rules** | Each professional connects their own WhatsApp, Instagram, TikTok, Facebook or Telegram account. Tokens never touch the database (the account names a secret). Platform rules are data with their official source, and every publication passes pre-flight checks (Instagram JPEG + public URL + 100/day; TikTok private until audited; Facebook refused until its rules are read). |
| **Creatives by code** | `agency creative infographic` or `video`: JPEG infographics (Pillow) and MP4/H.264 slideshow videos (ffmpeg), after the copy passes the pack's rules. Ships Atkinson Hyperlegible (OFL) so Spanish accents render. |
| **Publishing** | Approval by a person (and the owner above the discount cap), published once, `uncertain` and never retried after a transport error, dry run without a credential. Media go to Google Cloud Storage with V4 signed URLs that expire. |
| **Incoming WhatsApp** | Meta verification and signature check, deduplication, STOP opt-outs (pseudonymous), and alerts for staff on crisis wording or "I want to talk to a person". Message text is never stored; the AI never answers a crisis; staff reply inside the 24-hour window. |

```bash
uv run agency pack validate --strict
uv run agency creative infographic --pack ec-psychologist --title "Cuidar tu mente también es salud" \
  --point "Hablarlo ayuda." --cta "Agenda tu cita" --practice "Consultorio Demo" --out ad.jpg
```

## Polyglot specialists over A2A

Specialists do not have to be prompts in this repo. Any service that speaks [A2A](https://a2a-protocol.org) can join the catalog. [`agents/jvm-specialist`](agents/jvm-specialist) is a **Java 21 / Spring Boot 4** JVM performance agent (memory, GC, container sizing, startup, threads, profiling):

1. **Discover** — at startup the orchestrator fetches the Agent Card of every URL in `REMOTE_AGENTS` and turns it into a catalog entry (division `remote`); its description and skills are embedded into the routing index like any local agent. The catalog version includes remote agents, so the immutable index is rebuilt when one changes. Agents that start late are retried with backoff; unreachable ones are skipped without blocking startup.
2. **Route** — the router and the planner choose it like any other specialist. In team mode, Python and Java agents work in the same plan.
3. **Delegate** — the graph calls `message/send` instead of the LLM. The `contextId` is a UUIDv5 of *(tenant thread, agent)*: stable, so the remote agent keeps multi-turn context, but opaque, so tenant names never leave the orchestrator.
4. **Degrade** — if the agent is down or replies with garbage, the LLM answers in its role (from the card) and the response says so: `routing.remote = {"status": "fallback"}`.

**Trust boundary.** Cards and replies are untrusted input: the JSON-RPC URL must stay on the card's own origin, redirects are not followed, responses are size-capped while streaming, and replies go through the same output guard (PII redaction) as local answers. Tenant documents are not sent to remote agents unless `REMOTE_SHARE_KNOWLEDGE=true`.

The Java agent works with no API key: a deterministic rule engine answers from a vetted playbook. With `AGENT_LLM_PROVIDER=anthropic` it uses Spring AI, grounded in that playbook and with per-`contextId` memory, and it falls back to the rules if the model fails. CI builds it, runs its 34 JUnit tests, then starts it and runs a **Python ↔ Java contract test** over real HTTP. See [ADR 0007](docs/adr/0007-polyglot-agents-over-a2a.md).

```bash
make java-run                                         # Java agent on :8080 (needs JDK 21)
REMOTE_AGENTS='["http://localhost:8080"]' uv run agency ask "Our pods get OOMKilled, how do we size the JVM heap?"
```

## Quick start

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is pinned and fetched by uv.

```bash
git clone --recurse-submodules <this repo> && cd agency-orchestrator
uv sync --frozen
cp .env.example .env            # add ANTHROPIC_API_KEY (or any provider)

uv run agency route "Our React bundle is 4MB, how do we speed it up?"
uv run agency ask   "Set up CI/CD with GitHub Actions and Kubernetes"
uv run agency serve             # console at http://127.0.0.1:8000 · API docs at /docs
```

No API key? `LLM_BACKEND=fake` runs the whole pipeline offline with a deterministic model.

**Full stack** (API + Java A2A agent + Qdrant + Postgres + OpenTelemetry Collector + Jaeger + Prometheus):

```bash
docker compose up --build
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Web console: agent catalog, single/team mode, live team progress, held answers shown as "waiting for review" (polled until decided), and a Reviews tab to approve, edit or reject. |
| `POST /v1/chat` | Answer. Body: `question`, `mode` (`single`\|`team`), optional `thread_id`, `agent_id` (single) or `agent_ids` (team), `subject_id`, `force_review`. Returns `status` (`completed`, `blocked`, `pending_review`, `rejected`), `route_log`, `evidence`, `citations`, `decision_record`. 409 while the thread waits for review. |
| `POST /v1/chat/stream` | Same, as Server-Sent Events: `start`, `guardrails`, `knowledge`, `evidence`, `routing` or `plan`, one `step` per specialist, `review` (if held), `done`. |
| `GET /v1/reviews[?state=]` · `GET/POST /v1/reviews/{thread_id}` | Human-review queue; approve (optionally editing) or reject, and the workflow resumes. |
| `GET /v1/threads/{thread_id}` | Where a conversation stands (`pending_review`, `completed` with the approved answer, `rejected`, `blocked`); never the draft. |
| `PUT/GET /v1/subjects/{id}/consents[/{purpose}]` | Opt-in consents per purpose. |
| `GET /v1/subjects/{id}/export` · `DELETE /v1/subjects/{id}` | Data-subject access/portability and erasure. |
| `GET /v1/audit[?subject_id=]` | Audit trail of the caller's tenant. |
| `/v1/crm/patients`, `/v1/crm/appointments`, `/v1/crm/treatments`, `GET /v1/crm/alerts` | CRM records and traffic-light alerts. |
| `GET /v1/insights/summary` · `/segments` · `POST /v1/insights/ask` | SQL metrics, segments, natural-language questions over them. |
| `/v1/campaigns` (`POST`, `GET`, `/{id}`, `/template`, `/approve`, `/send`, `/cancel`, `/results`) | Loyalty campaigns with approval, holdout and lift. |
| `POST /v1/route` | Routing decision only, with candidates and scores. |
| `GET /v1/agents?division=` | Catalog listing. |
| `POST /v1/knowledge/documents` | Add or replace (`doc_id`) a company document. |
| `GET /v1/knowledge/documents` · `DELETE …/{doc_id}` | List or delete the caller's documents. |
| `POST /v1/knowledge/search` | Debug retrieval: which chunks a question would use. |
| `GET /.well-known/agent-card.json` | A2A Agent Card (skills = divisions). |
| `POST /a2a` | A2A JSON-RPC `message/send`; `contextId` ↔ `thread_id`; message metadata `{"mode": "team"}` for a team. |
| `GET /healthz`, `/readyz` | Liveness / readiness (index built). |

Auth is `X-API-Key`, mapped to a tenant via `API_KEYS="key1:tenant-a,key2:tenant-b"`. Threads and every business record are namespaced per tenant; another tenant's id answers 404 like a missing one. People get their own keys (`sk_` staff with roles via `POST /v1/admin/staff`, `pk_` patients); the audit trail records the key's owner and `X-Actor` is ignored.

```bash
curl -s localhost:8000/v1/chat -H "X-API-Key: key1" -H "Content-Type: application/json" \
  -d '{"question": "Necesito optimizar el SEO de mi sitio"}'
```

**Web console**: open `/`, paste an API key, pick *Single specialist* or *Team*. Clicking agents in the catalog pins them (one in single mode, a hand-picked team in team mode). The *Knowledge* tab uploads and manages the company's documents; answers show the sources they used. The page is static HTML/JS served by the API — no build step, no third-party origins, strict CSP.

**MCP**: `uv run agency mcp` exposes `list_agents`, `route_question`, `ask`, `ask_team`, `search_knowledge`, `list_reviews`, `resolve_review`, `crm_alerts` and `insights` over stdio, so held answers can be reviewed from the editor. Claude Desktop config:

```json
{ "mcpServers": { "agency": { "command": "uv", "args": ["--directory", "/path/to/agency-orchestrator", "run", "agency", "mcp"] } } }
```

## Engineering practices

| Concern | Implementation |
|---|---|
| **Reproducibility** | `uv.lock` with `--frozen` everywhere (local, CI, Docker). Agent catalog pinned as a git submodule and baked into the image. Vector collections are named `<name>_<catalog-hash>_<embedder>`, so an index is immutable and tied to exactly one catalog + embedding model. LiteLLM uses its bundled pricing map instead of fetching one at runtime. |
| **Provider portability** | LiteLLM behind a small `LLMClient` protocol. Model, router model and fallback chain are env vars. Retries, timeouts and fallbacks are configured centrally. |
| **Guardrails** | Input: size limit, prompt-injection heuristics (EN/ES), PII redaction (email, phone, SSN, Luhn-validated cards) *before* anything reaches a model. Output: PII redaction. Documents: injection check at ingestion, delimited as data in prompts. Router and planner output are schema- and allow-list-validated; plans must be DAGs. |
| **Evaluation** | *Routing:* `evals/routing.jsonl` (EN + ES, multiple acceptable agents per question) → top-1 accuracy, recall@k, per-language accuracy and latency. CI runs it offline as a regression gate; `nightly-eval.yml` runs it against real models with stricter thresholds. *Answers:* an LLM judge grades end-to-end answers (single, team, RAG) on relevance, faithfulness to the tenant's documents and completeness (`evals/answers.jsonl`). The judge itself is calibrated nightly against hand-labelled good/bad answers (`evals/judge_calibration.jsonl`), gating on agreement and false passes. See [ADR 0006](docs/adr/0006-llm-as-judge-evals.md). |
| **Observability** | OpenTelemetry traces per graph node, team step (`team.plan`, `team.worker`, `team.synthesize`) and LLM call, with GenAI semantic-convention attributes (model, input/output tokens). Metrics: routed count by agent and method, guardrail blocks, latency histogram, token usage. The collector strips prompt/completion text before export. |
| **Testing** | 440+ Python tests, ~96% coverage (gate: 80%). Offline by default: fake LLM, in-memory and embedded Qdrant, SQLite, mocked LiteLLM and Telegram. With `TEST_POSTGRES_URL` (always in CI, in a non-UTC session zone) they also run on Postgres: the full CRM/governance SQL, migrations from the previous schema with data, and concurrency across two replicas (monthly cap, one send per campaign with two outbox workers, appointment races, gap-free audit chain, one review decision). A Playwright test drives the console's review flow in Chromium. Every external-audit finding has a regression test that fails on the code before the fix. 34 JUnit tests cover the Java agent. |
| **Code quality** | Ruff (lint + format, incl. security rules), mypy `--strict`, pre-commit hooks. |
| **CI/CD** | GitHub Actions (actions pinned by SHA): lint/types → tests on Postgres (py3.12 + 3.13) → eval gate → Java agent + live A2A contract → Trivy (deps, secrets, IaC) → kubeconform and promtool → images built, scanned, and only then pushed to GHCR with SBOM + provenance. Dependabot keeps actions, base-image digests and dependencies current. |
| **Deployment** | Kustomize base + dev overlay, images pinned by digest: non-root, read-only rootfs, dropped capabilities, probes, HPA, PDB, topology spread. Namespace default-deny NetworkPolicy with scoped egress. Redis for the shared rate limit. An init container migrates the schema (Alembic, advisory lock); the API refuses an outdated schema or a non-durable prod profile. CronJobs for retention and audit-chain anchors, with Prometheus alerts. Secrets are created out-of-band, never committed. |

## Current eval results

Offline baseline (lexical hashing embedder, no LLM — what CI gates on):

| Metric | Value |
|---|---|
| Top-1 accuracy | 0.575 (EN 0.647 · ES 0.167) |
| Recall@8 | 0.875 |
| p50 routing latency | ~27 ms |

The offline embedder is lexical, so it cannot bridge Spanish questions to the English catalog; that is what the LLM router and multilingual embeddings (`EMBEDDING_BACKEND=litellm`) are for. Recall@k is the number that matters for the two-stage design — the LLM can only pick an agent that retrieval surfaced. Run `nightly-eval.yml` with API keys to get production numbers.

## Project layout

```
src/orchestrator/
  catalog.py      markdown agents -> AgentSpec, catalog version hash
  embeddings.py   offline hashing embedder, LiteLLM embedder
  vectorstore.py  in-memory and Qdrant stores
  router.py       retrieve -> LLM pick -> fallback
  team.py         planner, plan validation, worker/synthesis prompts
  knowledge.py    tenant RAG: chunking, stores (memory/Qdrant), retrieval
  graph.py        LangGraph workflow: memory, evidence gating, single/team, citations, risk, review
  checkpoint.py   checkpointer backends: in-memory (dev) or Postgres (durable, shared)
  guardrails.py   injection, PII, size limits
  evidence.py     evidence grading, contextual query rewrite, citation checks
  risk.py         review rules (divisions, clinical advice) and marketing-copy compliance
  packs.py        industry packs (pack_data/*.yaml): general, dental, retail
  db.py           SQLAlchemy async engine (SQLite dev/tests, Postgres prod)
  governance.py   audit trail, review queue, consents
  memory.py       consent-gated semantic memory per data subject
  crm.py          patients, appointments, treatment plans, traffic-light alerts
  insights.py     SQL-computed segments, no-show risk, pipeline, forecast; LLM narration
  campaigns.py    loyalty campaigns: compliance, approval, holdout, Telegram, lift
  llm.py          LiteLLM client, deterministic fake
  service.py      composition root shared by API, A2A, MCP, CLI
  remote.py       A2A client: discover remote agents, message/send, trust checks
  api/            FastAPI app, SSE, auth/rate limit, A2A
  web/            static web console (HTML/CSS/JS, no build)
  mcp_server.py   MCP tools
  judge.py        LLM-as-judge rubric, prompt and fail-closed verdict parsing
  answer_eval.py  end-to-end answer eval and judge calibration
  evals.py, cli.py
evals/            routing.jsonl, answers.jsonl, judge_calibration.jsonl
agents/jvm-specialist/  Java 21 / Spring Boot 4 A2A agent (rules + Spring AI), JUnit tests
deploy/           otel-collector, prometheus, k8s (kustomize)
docs/adr/         architecture decision records
```

## Known limitations & roadmap

- **Production deploy needs a cluster.** `deploy.yml` rehearses every green `main` on a kind cluster (healthy rollout, then a broken image rolled back automatically); the production job is manual behind the `production` environment and needs its `KUBECONFIG` secret. Load and backup/restore evidence: [docs/load-test.md](docs/load-test.md), `deploy/backup/`.
- **No ERP ledger yet.** Revenue comes from visit prices and accepted treatment plans; invoicing, payments and inventory with batches and expiry dates are the next module.
- **Channels:** Telegram campaigns; Instagram and TikTok publishing; incoming WhatsApp with staff replies. None has reached a real platform yet (no accounts or app review); the adapters follow the official docs and are tested against a simulated network. WhatsApp templates and campaigns, Facebook, comment replies and paid ads are next.
- **Legal confirmation pending.** Several Ecuadorian references are `secondary` or `to_verify`, so no health pack is marked production until a lawyer confirms them.
- **Identity:** per-person API keys (`sk_` staff keys with roles, `pk_` patient keys bound to one subject); `X-Actor` is ignored. SSO/OIDC is future work.
- **Streaming is per step, not per token.** `/v1/chat/stream` emits an event as each node or specialist finishes. Next: token streaming of the final answer and A2A `message/stream`.
- **Documents are plain text.** The console reads text files in the browser; PDF/DOCX need a server-side extractor.
- **Retrieval is dense-only.** Hybrid search (BM25 + vectors) and a reranker would help with exact terms like SKUs.
- **Guardrails are heuristic.** For regulated tenants, add an LLM-based classifier (e.g. Llama Guard) as an extra graph node.
- **The answer eval set is small** (14 questions, 16 calibration answers). It catches regressions but is not statistically tight; grow it from production traces (sampled, PII-redacted) and add plan-level metrics for team mode.
- **Remote agents are discovered once, at startup.** Adding or changing one needs a restart (or a rolling deploy). Next: periodic card refresh with an index rebuild, per-agent API keys, and a .NET (Semantic Kernel) agent next to the Java one.
- **The Java agent's LLM memory is in-process**, so it runs as one replica; the rule-based mode is stateless. Scaling it out means moving memory to Redis.

## License

MIT. Agent content © The Agency contributors (MIT), included as a submodule.
