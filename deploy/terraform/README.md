# Production infrastructure on Google Cloud (Terraform)

One environment per GCP project. Run it once for `staging`, once for `production` (owner decision O3).

| What | Why |
|---|---|
| VPC with private service access | Cloud SQL without a public IP |
| GKE Autopilot, private nodes, Workload Identity, Managed Prometheus | the `gke` overlay; no node keys; metrics and the `ClusterRules` work out of the box |
| Cloud SQL Postgres 17, TLS only, automated backups + point-in-time recovery (7 days), regional HA in production, deletion protection | RPO 5 min, RTO 1 h (`docs/runbooks/restore.md`) |
| Bucket `audit-anchors` with a retention policy (lockable) and versioning | WORM anchors of the audit chain (threat T3) |
| Bucket `backups` in another region, with a retention policy | the 24 h disaster copy of `deploy/backup/backup.sh` |
| Bucket `creatives` | media for publishing (decision A3) |
| Secret Manager: `pseudonym-key`, `db-password` | generated here, never in the database or in git |
| Workload Identity Federation for GitHub, restricted to this repository and `main` | keyless `deploy-gke.yml` |

## Use

```bash
cd deploy/terraform
terraform init                      # add a GCS backend for shared state first (see below)
terraform plan  -var project_id=agency-prod-123 -var github_repository=OWNER/REPO -var env=production
terraform apply -var project_id=agency-prod-123 -var github_repository=OWNER/REPO -var env=production
terraform output                    # values for the GitHub environment variables
```

Then:

1. Copy the outputs into GitHub: Settings → Environments → `production` (or `staging`). They are `GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA`, `GKE_CLUSTER`, `GKE_LOCATION` and `GCP_PROJECT`.
2. Create the Kubernetes Secret from Secret Manager. Use `deploy/k8s/base/deployment.yaml` (header) for the list of keys; `DATABASE_URL` goes through the Cloud SQL private IP with `sslmode=require`.
3. **Lock the audit-anchors retention** only after the first anchors are flowing (`-var lock_retention=true`). **A locked policy can never be shortened or removed: this is the point of WORM.**

## State

Keep the state in a GCS bucket with versioning, in the same project. Create it once by hand, then add:

```hcl
terraform { backend "gcs" { bucket = "<project>-tfstate" prefix = "agency" } }
```

## Validation

CI runs `terraform fmt -check` and `terraform validate` (job `manifests`). Applying the configuration needs the owner's GCP credentials and billing (decisions A3, O3, O9).
