# ADR 0021: Single-node production for the MVP, Kubernetes for scale

**Status:** accepted (2026-10-09). The owner asked for a production MVP at no cost ("todo gratis"), with Cloudflare in front. The design is mine, under the owner's authority.

## Context

The GKE profile (ADRs 0016, A8, O4, O6) is the target for many clinics. It needs a billing account and costs roughly US$175–450 a month per environment, before any revenue. The MVP serves one practice. It must still be a real production profile, not a demo:

- `APP_ENV=prod` and its refusal rules;
- signed images;
- health-gated deploys with rollback;
- backups;
- no open ports;
- the same code and image as the Kubernetes profile.

What Cloudflare's free plan can and cannot do:

- It cannot run the backend. Workers run lightweight JavaScript or WASM with 10 ms of CPU, not Python, LangGraph or ffmpeg. Containers are a paid plan.
- It can provide the entrance: HTTPS, DDoS protection, a WAF, and a Tunnel that needs no inbound port. R2 can hold the off-server backups.

## Decision

1. **One server runs the whole stack with Docker Compose** (`deploy/single-node/compose.yaml`): API, Postgres 17, Qdrant, Redis and cloudflared.
   - **Server.** The reference is an Oracle Cloud "Always Free" Ampere VM: 4 Arm cores, 24 GB, US$0. Any Linux host with Docker works.
   - **Image.** CI publishes the API image for amd64 and arm64 from the same Dockerfile and lockfile.
2. **The same production rules as Kubernetes.**
   - `APP_ENV=prod`: Postgres checkpoints, Qdrant, Redis and API keys are all required (A33).
   - Containers run read-only, with every capability dropped and `no-new-privileges`.
   - Every image is pinned by digest.
   - Migrations run as a separate step before the API starts, like the init container in Kubernetes.
3. **No inbound port.** The API listens on 127.0.0.1. The public entrance is a Cloudflare Tunnel, an outbound connection:
   - `quick-tunnel`: a free random `*.trycloudflare.com` address, no account needed;
   - `tunnel`: a named tunnel on the owner's domain, once one exists.
4. **Deploys** (`deploy.sh`):
   - digest only;
   - the CI keyless signature is verified with cosign, run from its pinned image;
   - migrations, then `compose up --wait` on `/readyz`, then a smoke test with a real key;
   - any failure restores the previous image.

   The contract is the same as `deploy/scripts/rollout.sh` for Kubernetes. `tests/test_single_node.py` runs the script against a fake `docker` and `curl` for every failure mode.
5. **Backups** (`backup.sh`, systemd timer at 03:15 UTC):
   - a `pg_dump` with its SHA-256 and the audit-chain anchors, checked with `pg_restore --list`;
   - 14 days kept on the server, plus an optional off-server copy to R2 or another S3 store with rclone.
   - Qdrant is rebuilt from Postgres (`agency knowledge reindex`), so it is not backed up.
6. **CI rehearses all of it on every push to main** (`deploy.yml`, job `single-node`):
   - the real `install.sh` with the image CI just signed;
   - every flow of `agency demo` against it;
   - a backup and its checksum;
   - `compose down` and a redeploy that keep the data;
   - an unsigned release and a missing release, both refused while the running one keeps serving.
7. **Where production is: the repository variable `DEPLOY_TARGET`.**
   - `vm`: after the rehearsal, `production-vm` deploys over SSH (host key pinned) once the `production` environment's reviewer approves.
   - `gke`: the GKE pipeline.
   - Unset: nothing asks for approval.

## Consequences

| | Single node (now) | GKE (later) |
|---|---|---|
| Cost | US$0 (Oracle free tier + Cloudflare free) | about US$175–450 a month per environment |
| Availability | One server: a host failure is downtime until it is rebuilt from backup (RTO hours) | Several replicas, regional Cloud SQL (RTO 1 h, ADR O2) |
| RPO | 24 h (nightly dump); less with a more frequent timer | 5 min (Cloud SQL point-in-time recovery) |
| Admission control | Signature verified by `deploy.sh` | Binary Authorization in the cluster (O6) |
| Scale | Vertical: one API process, measured at ~9 chat turns/s per process (docs/load-test.md) | Horizontal: HPA |

- **When to move to GKE.** Any of these: a second clinic, an availability commitment above one server, more than about 5 chat turns/s sustained, or real patients' data volume that makes a 24 h RPO unacceptable to the clinic. The image, the configuration and the migrations are the same, so the move is a database restore plus the Terraform of `deploy/terraform`.
- **Sub-processors.** Cloudflare (TLS termination, so it sees the traffic) and Oracle (hosting) join the list in `docs/legal/subprocessors.md`. Both need their DPA signed before real patients (L8).
- **Quick-tunnel addresses change on restart.** A fixed address needs a domain (about US$10 a year) and a named tunnel.
- **Not changed.** The kind rehearsal and the GKE path stay in CI, so the scale path is always tested.
