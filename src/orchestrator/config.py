"""Runtime configuration. Every knob is an environment variable (12-factor)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_TENANT = re.compile(r"[\w.-]{1,64}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # --- Agent catalog -----------------------------------------------------
    agents_dir: Path = Path("vendor/agency-agents")

    # --- LLM (any LiteLLM model string: anthropic/..., openai/..., gemini/...,
    # mistral/..., ollama/llama3.1). "fake" is a deterministic offline backend.
    llm_backend: Literal["litellm", "fake"] = "litellm"
    llm_model: str = "anthropic/claude-sonnet-5-5"
    router_model: str = "anthropic/claude-haiku-4-5-20251001"
    llm_fallback_models: list[str] = Field(default_factory=lambda: ["openai/gpt-4o-mini"])
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2048
    llm_timeout_s: float = 60.0
    llm_num_retries: int = 2

    # --- Retrieval ---------------------------------------------------------
    embedding_backend: Literal["hashing", "litellm"] = "hashing"
    embedding_model: str = "openai/text-embedding-3-small"
    vector_backend: Literal["memory", "qdrant"] = "memory"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "agency_agents"

    # --- Routing -----------------------------------------------------------
    router_top_k: int = 8
    router_use_llm: bool = True
    router_min_confidence: float = 0.35
    default_agent_id: str | None = None
    history_max_messages: int = 10

    # --- Team orchestration (planner -> parallel specialists -> synthesizer) --
    planner_model: str = "anthropic/claude-sonnet-5-5"
    team_max_agents: int = 4
    team_candidates_k: int = 16
    team_max_concurrency: int = 4
    team_context_chars: int = 6000

    # --- Company knowledge base (RAG) --------------------------------------
    knowledge_enabled: bool = True
    knowledge_collection: str = "knowledge"
    knowledge_top_k: int = 4
    # Chunks scoring below this are not relevant enough to put in the prompt. Tune it per
    # embedder: lexical hashing scores are lower than semantic cosine similarities.
    knowledge_min_score: float = 0.1
    knowledge_chunk_chars: int = 800
    knowledge_chunk_overlap: int = 150
    knowledge_max_doc_chars: int = 200_000
    # Evidence gating: the best chunk must reach this score for the evidence to count as
    # "strong"; below it the agents are told the excerpts are only loosely related.
    knowledge_strong_score: float = 0.25
    # Retrieval attempts per turn (the retry rewrites a follow-up with the previous turn).
    knowledge_max_attempts: int = Field(default=2, ge=1, le=3)
    # A single-agent answer citing sources that do not exist is regenerated this many times
    # before the bad markers are stripped.
    citation_max_retries: int = Field(default=1, ge=0, le=2)

    # --- Governance: databases, audit, reviews, consents ---------------------
    # SQLAlchemy async URL for the governance/CRM tables. SQLite in memory for dev and
    # tests; postgresql+psycopg://... (sslmode=require) in prod.
    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///:memory:")
    # Industry packs: tenant -> pack id (JSON), e.g. {"clinica-sonrisa": "dental"}.
    tenant_packs: dict[str, str] = Field(default_factory=dict)
    default_pack: str = "general"
    # Human review of high-risk answers (LangGraph interrupt + checkpoint).
    review_enabled: bool = True
    # Patient access keys (the /v1/me link sent to a patient) expire after this many days.
    patient_access_ttl_days: int = Field(default=90, ge=1, le=730)
    # One run per conversation: a lease held for at most this long (a crashed replica
    # frees the thread after it). Also how old a half-resolved review must be before
    # startup reconciles it.
    thread_lease_seconds: int = Field(default=300, ge=10, le=3600)

    # --- Semantic memory (long-term, per data subject) -----------------------
    memory_enabled: bool = True
    memory_collection: str = "memory"
    memory_ttl_days: int = Field(default=365, ge=1)

    # --- Campaigns and channels ----------------------------------------------
    # Without a token the Telegram channel runs dry: messages are recorded, not sent.
    telegram_bot_token: SecretStr | None = None
    telegram_api_base: str = "https://api.telegram.org"
    campaign_default_holdout_pct: int = Field(default=20, ge=0, le=50)
    # A booking this many days after a message counts as a conversion.
    campaign_conversion_window_days: int = Field(default=30, ge=1)
    # Offers appear in the patient's app first; unseen after this many hours, they go
    # out by Telegram (0: never fall back).
    campaign_fallback_hours: int = Field(default=48, ge=0, le=720)
    # Outbox worker: pass interval (0 disables the in-process worker, e.g. when a
    # separate worker or CronJob runs it), and the age at which a claim is presumed dead.
    outbox_interval_seconds: int = Field(default=30, ge=0)
    outbox_stale_seconds: int = Field(default=300, ge=30)
    # Telegram sends it in X-Telegram-Bot-Api-Secret-Token on every webhook call
    # (setWebhook secret_token). Without it the inbound endpoint is disabled.
    telegram_webhook_secret: SecretStr | None = None

    # --- Guardrails --------------------------------------------------------
    max_input_chars: int = 8000
    # Whole request body, enforced as it arrives (413). Document upload gets more room:
    # KNOWLEDGE_MAX_DOC_CHARS of text can take up to ~4 bytes per character in JSON.
    max_body_bytes: int = Field(default=256 * 1024, ge=1024)
    max_document_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    injection_action: Literal["block", "flag"] = "block"
    redact_pii: bool = True

    # --- Remote A2A agents (any language: Java/Spring, .NET, ...) -------------
    # Base URLs whose Agent Card is served at /.well-known/agent-card.json. Operator
    # config only: never taken from requests.
    remote_agents: list[str] = Field(default_factory=list)
    remote_agents_api_key: SecretStr | None = None
    remote_agent_timeout_s: float = 30.0
    remote_agent_max_chars: int = 20_000
    # Tenant documents leave our trust boundary only if this is on.
    remote_share_knowledge: bool = False
    # Startup discovery retries unreachable agents (they may boot slower than we do).
    remote_discovery_attempts: int = Field(default=5, ge=1)
    remote_discovery_backoff_s: float = 2.0

    # --- Answer-quality evals (LLM-as-judge) -------------------------------
    # A stronger model than the one answering; ideally another family (self-preference).
    judge_model: str = "anthropic/claude-opus-5-5"
    # An answer passes when every rubric criterion (1-5) reaches this score.
    judge_min_score: int = Field(default=4, ge=1, le=5)

    # --- Conversation state (LangGraph checkpointer) -----------------------
    # "postgres" makes threads survive restarts and be shared by every replica.
    checkpointer_backend: Literal["memory", "postgres"] = "memory"
    postgres_url: SecretStr | None = None
    postgres_pool_size: int = 10
    # Only for a private network that is already encrypted (e.g. a service mesh with mTLS).
    postgres_allow_insecure: bool = False
    # Conversations inactive for longer are deleted by `agency purge-threads` (CronJob).
    thread_retention_days: int = Field(default=90, ge=1)

    # --- API / multi-tenancy ----------------------------------------------
    # Comma-separated "key:tenant" pairs. Empty in dev means anonymous access.
    api_keys: SecretStr = SecretStr("")
    rate_limit_per_minute: int = 60
    # "redis" shares each tenant's budget across replicas (required with >1 replica).
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    redis_url: SecretStr | None = None
    # If Redis is unreachable (A30): "local" falls back to a per-replica bucket (degraded:
    # at most N replicas x the budget, never unlimited and never an outage), "open" lets
    # everything through, "closed" refuses requests (429) until Redis is back.
    rate_limit_on_outage: Literal["local", "open", "closed"] = "local"
    public_base_url: str = "http://localhost:8000"

    # --- Observability -----------------------------------------------------
    otel_enabled: bool = False
    otel_service_name: str = "agency-orchestrator"
    otel_exporter_otlp_endpoint: str | None = None

    # APP_ENV=prod refuses to start with state kept in process memory (A33), unless the
    # operator explicitly accepts it for a throwaway demo.
    prod_allow_ephemeral: bool = False

    def production_problems(self) -> list[str]:
        """Why this configuration is not a durable, multi-replica production profile."""
        if self.app_env != "prod":
            return []
        problems = []
        if not self.api_keys.get_secret_value().strip():
            problems.append("API_KEYS is empty: no tenant can authenticate")
        if self.database_url.get_secret_value().startswith("sqlite"):
            problems.append("DATABASE_URL is SQLite: use Postgres")
        if self.checkpointer_backend == "memory":
            problems.append(
                "CHECKPOINTER_BACKEND=memory: conversations are lost on restart and not "
                "shared between replicas (use postgres)"
            )
        if self.vector_backend == "memory":
            problems.append(
                "VECTOR_BACKEND=memory: documents, semantic memory and the routing index "
                "live in one process (use qdrant)"
            )
        if self.rate_limit_backend == "memory":
            problems.append(
                "RATE_LIMIT_BACKEND=memory: every replica gives each tenant its own budget "
                "(use redis)"
            )
        return problems

    def tenant_keys(self) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for raw in self.api_keys.get_secret_value().split(","):
            raw = raw.strip()
            if not raw:
                continue
            key, _, tenant = raw.partition(":")
            tenant = tenant or "default"
            # Tenants prefix thread keys ("tenant:thread"): a ":" in a name would let one
            # tenant's prefix match another's (erasure of "acme" hitting "acme:beta").
            if not _TENANT.fullmatch(tenant):
                raise ValueError(f"invalid tenant name {tenant!r}: use letters, digits, _ . -")
            pairs[key] = tenant
        return pairs


@lru_cache
def get_settings() -> Settings:
    return Settings()
