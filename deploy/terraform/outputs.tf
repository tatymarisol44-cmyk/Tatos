# Values for the GitHub environment (Settings -> Environments -> staging / production).
output "GCP_PROJECT" {
  value = var.project_id
}

output "GKE_CLUSTER" {
  value = google_container_cluster.this.name
}

output "GKE_LOCATION" {
  value = google_container_cluster.this.location
}

output "GCP_WIF_PROVIDER" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "GCP_DEPLOYER_SA" {
  value = google_service_account.deployer.email
}

# For the Kubernetes Secret (DATABASE_URL / POSTGRES_URL, with sslmode=require).
output "db_private_ip" {
  value = google_sql_database_instance.this.private_ip_address
}

output "secrets" {
  description = "Secret Manager ids holding the generated pseudonym key and database password."
  value       = { for k, s in google_secret_manager_secret.secrets : k => s.secret_id }
}

output "buckets" {
  value = {
    audit_anchors = google_storage_bucket.audit_anchors.name
    backups       = google_storage_bucket.backups.name
    creatives     = google_storage_bucket.creatives.name
  }
}
