# ADR 0022: Free managed services for the MVP when no card and no server are available

**Status:** accepted (2026-10-12). The owner asked for production with no card and nothing kept on at home ("sin tarjeta", "sin que la computadora esté prendida 24/7"). The design is mine, under the owner's authority.

## Context

ADR 0021 puts the MVP on one free Oracle Cloud VM. Oracle, like AWS, Google Cloud, Azure (outside student offers) and Fly.io, asks for a card to sign up. A computer at the practice needs no card but must stay on.

What was checked on 2026-10-12:

- **Koyeb**: new sign-ups closed ("Koyeb is joining Mistral").
- **Hugging Face Spaces**: free Spaces sleep after 48 hours and lose their disk; Docker Spaces may need a paid plan.
- **Neon (Postgres)**: free plan, no card, 100 CU-hours a month per project; compute scales to zero after 5 idle minutes. The API's background worker (`outbox_worker`: campaigns, on-call escalation, reminders) reads the database every 30 seconds, so the compute never sleeps: about 180 CU-hours a month at the smallest size. The database would stop around day 16.
- **Render**: free web service, 512 MB, 0.1 CPU, 750 instance hours a month; it stops after 15 minutes without inbound requests. Sign-up takes a GitHub account; Render may ask a card for verification in some cases.
- **Supabase (Postgres)**: free plan, no card, compute always on, 500 MB; projects with low activity for 7 days are paused. Direct connections are IPv6; the session pooler offers IPv4.
- **Upstash (Redis)** and **Qdrant Cloud** (1 GB cluster): free plans, no card.

Measured on the owner's computer: the API started with 190 MB and stayed at 195 MB after 30 conversations (10 in team mode). Loading LiteLLM, the client of the real model, adds about 135 MB.

## Decision

1. **The API on a Render free web service**, from the same image CI builds and signs (`deploy/render/render.yaml`, a Blueprint). State in managed services, all in AWS us-east-1:
   - Postgres: **Supabase**, through the session pooler (IPv4, `sslmode=require`). Its **Data API is turned off**: it would publish our `public` tables under the project's public key.
   - Redis: **Upstash**, over verified TLS (`rediss://`).
   - Vectors: **Qdrant Cloud**, with an API key.
   - Neon is not used (see Context).
2. **Same production rules.** `APP_ENV=prod`, Postgres checkpoints, Qdrant, Redis, API keys, hashing embeddings. Nothing is relaxed: `tests/test_render_profile.py` builds the settings from the Blueprint and requires `production_problems() == []`, and every variable must be a real setting (unknown ones would be ignored silently).
3. **Migrations at startup** (`DB_AUTO_MIGRATE=true`): the free plan has no pre-deploy step, there is one instance, and the migrations take an advisory lock.
4. **Connection budget.** The session pooler lends one server connection per client (15 on the free plan): 3 + 2 overflow for the API, 3 for the checkpoints.
5. **Kept awake by UptimeRobot**, which calls `/healthz` every 5 minutes. It is also the down alert. `/healthz` carries no personal data, so UptimeRobot is not a sub-processor.
6. **Rehearsed on every push** (`deploy/render/rehearse.sh`, job `render`):
   - the image runs with 512 MB, no swap, 0.1 CPU and the Blueprint's environment;
   - stand-ins for the managed services are reached the way production reaches them: Postgres over TLS, Redis over verified TLS with a password, Qdrant with an API key;
   - every demo flow runs from another container;
   - memory budget: the API's peak plus LiteLLM must stay under 460 MB.
7. **Deploys by digest** (`deploy/render/deploy.sh`, job `production-render`, `DEPLOY_TARGET=render`):
   - the CI signature is verified first, with the identity of `deploy/single-node/deploy.sh`;
   - Render's API deploys the digest, and the deploy is followed until `live`;
   - Render moves traffic only once `/readyz` answers on the new instance, so a failed release never serves;
   - then `/readyz` is checked through the public address.

## Consequences

- **US$0, no card, nothing on at the practice.** Steps for the owner: `docs/DEPLOY-RENDER.md`.
- **Slow CPU.** The console feels slower than on a server. Chat latency is dominated by the model.
- **Four sub-processors instead of one server** (Render, Supabase, Upstash, Qdrant). Each needs its processing agreement before real patients (`docs/legal/subprocessors.md`).
- **Open before real patients:** an off-site encrypted nightly backup. Supabase Free has no downloadable backups.
- **Qdrant Free pauses a cluster unused for a week.** Knowledge documents and semantic memory live there; daily use keeps it active.
- **Free tiers carry no SLA and can change.** This profile is for the pilot. The same image moves to one server (ADR 0021) or to the providers' paid tiers with the first revenue.
