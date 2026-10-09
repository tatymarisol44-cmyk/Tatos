# Production MVP on one free server

The single-node profile (ADR 0021): the whole platform on one Linux server with Docker, behind a Cloudflare Tunnel. It costs nothing on an Oracle Cloud "Always Free" Ampere VM. CI rehearses this exact installation on every push to `main` (`deploy.yml`, job `single-node`).

Real patients need the legal steps first: the lawyer, the DPO, and the processing agreements with Oracle, Cloudflare and the model providers (`docs/LAUNCH.md`, section 5). Until then, use synthetic data only.

## 1. Create the server (owner, about 20 minutes)

1. Sign up at **cloud.oracle.com** for the Free Tier.
   - Oracle asks for a card to verify identity. Always Free resources are not charged.
   - The **home region** cannot be changed later, and Always Free resources live only there. Choose one in the Americas, for example `us-ashburn-1` or `sa-saopaulo-1`.
2. Go to **Compute → Instances → Create instance**.
   - **Image:** Canonical Ubuntu 24.04.
   - **Shape:** Ampere → `VM.Standard.A1.Flex`, 4 OCPU, 24 GB. Look for the "Always Free-eligible" label.
     - "Out of capacity" is common. Try again later, or try another availability domain.
   - **SSH keys:** upload your public key, or let Oracle generate a pair and download the private key.
   - **Networking:** a public IPv4 address only for SSH. Leave the other ingress rules closed: the platform needs no inbound port.
3. Note the public IP.

## 2. Install (owner, about 10 minutes)

```bash
ssh ubuntu@<public IP>
git clone https://github.com/tatymarisol44-cmyk/Tatos.git && cd Tatos
sudo bash deploy/single-node/install.sh --tenant clinica --pack ec-psychologist
```

The installer does the following:

- installs Docker from Docker's signed repository;
- generates every secret once, into `/opt/agency/.env` (mode 600);
- creates the nightly backup timer;
- deploys the image CI built and signed from `main`, after checking its signature;
- prints the public `https://….trycloudflare.com` address.

The practice's service key is the `API_KEYS` line of `/opt/agency/.env`. With it, create each person's own key: `POST /v1/admin/staff` in `/docs`.

## 3. Operate

| Task | Command (on the server) |
|---|---|
| Deploy or roll back to an image | `sudo /opt/agency/deploy.sh ghcr.io/tatymarisol44-cmyk/agency-orchestrator@sha256:…` |
| State | `cd /opt/agency && sudo docker compose ps` |
| Logs | `sudo docker compose logs --tail 100 api` |
| Public address | `sudo docker compose logs tunnel-quick \| grep trycloudflare` |
| Backup now | `sudo /opt/agency/backup.sh` (also every night at 03:15 UTC) |
| Restore | `docs/runbooks/restore.md`: `pg_restore` into the `postgres` container, then `agency audit-verify --anchors` against the `.anchors.jsonl` |

## 4. Optional upgrades

- **A model that writes real answers.** In `.env`, set `ANTHROPIC_API_KEY=…` and `LLM_BACKEND=litellm`, then redeploy the same image. Sign Anthropic's DPA first.
- **A fixed address on your own domain.** Buy a domain (about US$10 a year; Cloudflare Registrar sells at cost). Then:
  1. In Zero Trust → Networks → Tunnels, create a tunnel that points to `http://api:8000`.
  2. In `.env`, set `COMPOSE_PROFILES=tunnel`, `TUNNEL_TOKEN=…` and `PUBLIC_BASE_URL=https://your.domain`.
  3. Redeploy.
- **Backups off the server.** Create a Cloudflare R2 bucket (10 GB free) and an API token. Then fill in `BACKUP_REMOTE`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in `.env` (see the header of `backup.sh`).
- **Continuous delivery to the server.** GitHub then deploys each green `main` after your approval:
  1. Create a deploy user with passwordless `sudo` for `/opt/agency/deploy.sh` and its own SSH key.
  2. In GitHub, set the repository variable `DEPLOY_TARGET=vm`.
  3. In the environment `production`, add the variables `VM_HOST` and `VM_USER`.
  4. Add the secrets `VM_SSH_KEY` (the private key) and `VM_KNOWN_HOSTS` (the output of `ssh-keyscan <IP>`).
