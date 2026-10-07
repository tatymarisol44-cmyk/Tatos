# ADR 0015: Social channels per professional, with verified platform rules

**Status:** accepted for the structure (M1); publishing, inbound replies and creatives are later slices.

## Context

The first real customers are psychology and psychiatry practices in Ecuador. They want to publish on Instagram, Facebook and TikTok, answer WhatsApp, and run campaigns there. Today campaigns send direct messages over one Telegram bot whose token is global to the deployment. Each professional will connect their own accounts, so the accounts must belong to a tenant and, optionally, to one professional of that establishment.

Each platform has its own rules, and a rule that is assumed rather than read is how an integration gets an account banned. A practice is also bound by health-advertising and data-protection rules (ADR 0014 packs). The owner chose to generate ads, infographics and videos by code rather than with Adobe After Effects (not installed; the MCP server for it only edits local After Effects projects and publishes nothing).

## Decision

1. **Accounts** (`channel_accounts`): tenant, network, the platform's id and public handle, an optional `professional_id`, whether a TikTok client is audited, and `active`. Connecting and disabling are admin-only and audited in the hash-chained log.
2. **Credentials by reference.** An account stores `secret_ref`, the name of a secret; the value is read from `SOCIAL_SECRET_<name>`, which production fills from its secret store. The database, the API responses and the audit log never contain a token. The API only reports `secret_configured`.
3. **Platform rules as data** (`PLATFORM_RULES`), each with the official page and a status, like the legal references of the packs. Read on 2026-10-07:
   - WhatsApp: only approved templates outside the customer-service window, templates reviewed by Meta, opt-in before contact and every opt-out honoured, no health information where rules require stricter systems, a path to a human.
   - Instagram: professional accounts only, JPEG only, media on a public URL, 100 API posts per 24 hours.
   - TikTok: posts from an unaudited client are private; MP4 with H.264; `video.publish` scope.
   - Facebook: not read yet, so it cannot publish (`to_verify`).
4. **Pre-flight checks** (`check_publish`, `check_whatsapp_message`) are pure functions every adapter must call before talking to a platform. An unaudited TikTok client is not an error, but its visibility is forced to private.

### How this maps to a serverless WhatsApp bot

A common reference design is webhook → API Gateway → Lambda → Bedrock → DynamoDB, with Secrets Manager and CloudWatch. This system already has each piece, in a form that runs on any cloud: the FastAPI endpoint receives and authenticates webhooks (as the Telegram one does), the graph orchestrates, LiteLLM calls the model (Bedrock is one of its providers), Postgres keeps conversation state through the checkpointer, credentials come by reference from the secret store, and OpenTelemetry plus the Prometheus alerts monitor it. The same four risks apply and are covered: platform rate limits (pre-flight checks, the contact cap), model cost (usage ledger), errors and timeouts (outbox, uncertain deliveries are never re-sent), and security (webhook authentication, no tokens in code or data).

## Consequences

- One structure serves every professional of a practice, with their own accounts and nothing shared by accident.
- Platform rules are checked, versioned and visible at `GET /v1/social/rules`; when a platform changes its terms, one entry changes and the tests show what it affects.
- Not built yet, in order: **M2** creatives by code (infographics and video; needs an image library and `ffmpeg`, neither is installed), **M3** publishing adapters (dry run without a secret, like Telegram), **M4** inbound WhatsApp and comment replies (Meta webhook signature to be read from the official docs first; crisis wording goes to a person, never answered by the AI, ADR 0014), **M5** campaigns over the new channels with the existing holdout measurement.
- Health-advertising limits for Ecuadorian clinics are still a lawyer's question (H1); until answered, every publication goes through human approval, as campaigns already do.
