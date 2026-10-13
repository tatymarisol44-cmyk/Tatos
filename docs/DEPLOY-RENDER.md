# Production MVP on free managed services (no card, no server)

The free managed profile (ADR 0022): the API on a Render free web service, its state in Supabase (Postgres), Upstash (Redis) and Qdrant Cloud, and UptimeRobot keeping it awake. Nothing runs on your computer and no card is needed. CI rehearses this profile on every push to `main` with the free instance's limits (512 MB, 0.1 CPU): `deploy.yml`, job `render`.

Real patients need the legal steps first: the lawyer, the DPO, and the processing agreements with each provider below (`docs/LAUNCH.md`, section 5; `docs/legal/subprocessors.md`). Until then, use synthetic data only.

Put every service in **AWS us-east-1 (North Virginia)**: the API talks to all of them on every request.

**Never paste a password or key into a chat, an issue or a commit.** Each one goes straight from the provider's page into Render's dashboard, or into the private file below.

## Automatic setup (recommended)

`deploy/render/setup.py` does sections 4 and 5 for you, from one private file outside the repository:

1. Do by hand what only the provider's console can do:
   - section 1, items 1–3 (the Supabase project, Data API off, SSL enforced);
   - section 2, item 1 (the Upstash database);
   - section 3, items 1–2 (the Qdrant cluster and its key);
   - a Render API key (Account Settings → API Keys).
2. Fill in the private file, for example `C:\Users\<you>\agency-render.env`. The keys are listed in the header of `setup.py`; paste Supabase's session-pooler string as shown, `[YOUR-PASSWORD]` included.
3. Check the three services from your computer, changing nothing:

   ```
   python deploy/render/setup.py C:\Users\<you>\agency-render.env --check-only
   ```

4. Create the service (or update it: a re-run keeps the secrets Render generated):

   ```
   python deploy/render/setup.py C:\Users\<you>\agency-render.env
   ```

   It builds the environment from `render.yaml`, creates the free service, follows the first start until it is live, and checks `/readyz`. The practice's service key is generated once and written into the file as `SERVICE_KEY`. Nothing secret is printed.
5. Then section 6 (UptimeRobot).

## 1. Supabase: the database (about 10 minutes)

1. **New project**: name `agency-produccion`, region **East US (North Virginia)**.
   - **Database password**: use *Generate a password* (letters and digits only: it goes into a URL). Keep it in your password manager.
2. **Turn the Data API off.** Project Settings → Data API → disable it.
   - **Why it matters:** Supabase publishes the tables of the `public` schema over its REST API, reachable with the project's *public* key. Our API creates its tables there and protects them itself, not with Supabase's row-level security. With the Data API on, anyone holding that public key could read the tables.
3. **Enforce SSL.** Database → Settings → SSL Configuration → *Enforce SSL on incoming connections*.
4. **Connection string.** Click **Connect** → **Session pooler** (port **5432**; IPv4, which Render needs). It looks like:

   ```
   postgresql://postgres.<ref>:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```

   Do not use the *Transaction pooler* (port 6543): the conversation checkpoints need a session. From it you build two values for Render (step 5):

   | Render variable | Value |
   |---|---|
   | `DATABASE_URL` | `postgresql+psycopg://postgres.<ref>:<password>@<host>:5432/postgres?sslmode=verify-full&sslrootcert=/app/certs/supabase-root-2021.crt` |
   | `POSTGRES_URL` | `postgresql://postgres.<ref>:<password>@<host>:5432/postgres?sslmode=verify-full&sslrootcert=/app/certs/supabase-root-2021.crt` |

## 2. Upstash: the shared rate limit (about 3 minutes)

1. **Create Database** → Redis, region **us-east-1**, TLS on (the default).
2. From the database page, build `REDIS_URL` from the endpoint and the password:

   ```
   rediss://default:<password>@<endpoint>.upstash.io:6379
   ```

   Note the double `s` in `rediss`: the connection is encrypted and the certificate is checked.

## 3. Qdrant Cloud: knowledge and the routing index (about 5 minutes)

1. **Create a Free cluster**: AWS, **us-east-1**.
2. **API Keys** → create a key for the cluster.
3. Values for Render:

   | Render variable | Value |
   |---|---|
   | `QDRANT_URL` | the cluster URL with the port, `https://<id>.us-east-1-0.aws.cloud.qdrant.io:6333` |
   | `QDRANT_API_KEY` | the key |

## 4. The practice's service key (1 minute, on your computer)

This key is the practice's administrator: with it you create each person's own key later. In PowerShell:

```powershell
$b = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$k = 'svc_' + -join ($b | ForEach-Object { $_.ToString('x2') })
"${k}:consultorio" | Set-Clipboard   # now on the clipboard; never shown on screen
```

`consultorio` is the practice's id: letters, digits, `_ . -`. Save the key in your password manager too.

## 5. Render: the API (about 10 minutes, then the first start)

1. **New → Blueprint** → connect the GitHub repository → **Blueprint path**: `deploy/render/render.yaml`.
   - If your Render account offers no Blueprint path, create **New → Web Service → Existing image** (`ghcr.io/tatymarisol44-cmyk/agency-orchestrator:main`, plan Free, region Virginia, health check `/readyz`) and add the variables of `render.yaml` by hand.
2. Render asks for the values marked `sync: false`:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL`, `POSTGRES_URL` | step 1 |
   | `REDIS_URL` | step 2 |
   | `QDRANT_URL`, `QDRANT_API_KEY` | step 3 |
   | `API_KEYS` | step 4 (paste) |
   | `TENANT_PACKS` | `{"consultorio":"ec-psychologist"}` |
   | `PRACTICE_DISPLAY_NAME` | the practice's name, as patients see it |
   | `PUBLIC_BASE_URL` | `https://agency-api.onrender.com` (correct it after step 3 if Render adds a suffix) |
   | `LLM_BACKEND` | `fake` (fixed texts) until Anthropic's DPA is signed |

   `PSEUDONYM_KEY`, `META_APP_SECRET` and `WHATSAPP_VERIFY_TOKEN` are created by Render.
3. **Apply.** The first start migrates the database and indexes the catalog: a few minutes at 0.1 CPU. The service turns *Live* when `/readyz` answers.
4. Check: open `https://<service>.onrender.com/readyz` (`"status": "ready"`), then the console at `https://<service>.onrender.com/` with the service key.

## 6. UptimeRobot: awake and watched (2 minutes)

Render stops a free service after 15 minutes without requests, and appointment reminders and on-call escalations run inside it. One monitor keeps it awake and tells you when it is down:

- **New monitor** → HTTP(s), URL `https://<service>.onrender.com/healthz`, interval **5 minutes**, alert contact: your e-mail.

`/healthz` carries no personal data. 750 free instance hours a month cover one service always on.

## 7. Continuous delivery (optional, 5 minutes)

Each green `main` is then deployed after your approval, by digest, with its CI signature checked (`deploy/render/deploy.sh`):

1. Render → Account Settings → **API Keys** → create one.
2. The service id is in the service's URL: `srv-…`.
3. GitHub → Settings → Secrets and variables → Actions:
   - repository variable `DEPLOY_TARGET` = `render`;
   - environment `production`: variables `RENDER_SERVICE_ID` (`srv-…`) and `RENDER_PUBLIC_URL` (`https://<service>.onrender.com`); secret `RENDER_API_KEY`.

To roll back: Actions → Deploy → *Run workflow* with an earlier digest.

## 8. Limits of the free profile

| Limit | What it means | When it matters |
|---|---|---|
| 0.1 CPU, 512 MB | The console is slower than on a server; chat answers barely notice (the model's time dominates). CI checks the memory budget on every push. | Several practices, or heavy use: move up (section 9). |
| No downloadable backups on Supabase Free | A provider incident or a mistake cannot be undone from our side. | **Before real patients**: an off-site, encrypted nightly backup (pending, ADR 0022). |
| Qdrant Free pauses a cluster unused for a week, and deletes it after four | Knowledge documents and semantic memory live there. Daily use keeps it active; if it pauses, resume it in the Qdrant console. | A practice that stops using the chat. |
| Free tiers have no SLA, and their terms change | A provider may pause or end the free plan. | Any paying customer: plan the move to a paid tier or a server. |

## 9. Moving up

The same image runs on every profile. With the first revenue:

- one server (ADR 0021, `docs/DEPLOY-SINGLE-NODE.md`): `pg_dump` from Supabase, `pg_restore` into the server's Postgres, re-upload the knowledge documents; or
- the paid tiers of the same providers: nothing to move.
