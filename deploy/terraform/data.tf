# --- Cloud SQL Postgres: private IP, TLS only, backups + point-in-time recovery ------------

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_database_instance" "this" {
  name                = local.name
  database_version    = "POSTGRES_17"
  region              = var.region
  deletion_protection = local.production
  depends_on          = [google_service_networking_connection.private_services]

  settings {
    tier              = var.db_tier
    edition           = "ENTERPRISE"
    availability_type = local.production ? "REGIONAL" : "ZONAL"
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    # RPO 5 minutes: daily backups plus the write-ahead log for point-in-time recovery.
    backup_configuration {
      enabled                        = true
      start_time                     = "07:00" # 02:00 in Ecuador
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 30
      }
    }

    maintenance_window {
      day  = 7 # Sunday
      hour = 8
    }

    insights_config {
      query_insights_enabled  = true # the slowest statements (docs/runbooks/api-latency.md)
      record_application_tags = false
      record_client_address   = false
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "1000"
    }
  }
}

resource "google_sql_database" "agency" {
  name     = "agency"
  instance = google_sql_database_instance.this.name
}

resource "google_sql_user" "agency" {
  name     = "agency"
  instance = google_sql_database_instance.this.name
  password = random_password.db.result
}

# --- buckets --------------------------------------------------------------------------------

# WORM anchors of the audit chain (threat T3). Lock the retention once anchors flow: a
# locked policy can never be shortened or removed.
resource "google_storage_bucket" "audit_anchors" {
  name                        = "${var.project_id}-audit-anchors"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
  retention_policy {
    retention_period = var.anchor_retention_days * 86400
    is_locked        = var.lock_retention
  }
  depends_on = [google_project_service.apis]
}

# The 24 h disaster copy (deploy/backup/backup.sh), in another region than the database.
resource "google_storage_bucket" "backups" {
  name                        = "${var.project_id}-db-backups"
  location                    = var.backup_region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
  retention_policy {
    retention_period = var.backup_retention_days * 86400
    is_locked        = var.lock_retention
  }
  lifecycle_rule {
    condition {
      age = var.backup_retention_days + 30
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "creatives" {
  name                        = "${var.project_id}-creatives"
  location                    = var.region
  uniform_bucket_level_access = true
  # Instagram fetches media from a URL: signed URLs are used, the bucket stays private.
  public_access_prevention = "enforced"
  depends_on               = [google_project_service.apis]
}

# --- secrets generated here, never in git or in the database ------------------------------

resource "random_password" "pseudonym_key" {
  length  = 64
  special = false
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(["pseudonym-key", "db-password"])
  secret_id = "${local.name}-${each.value}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "pseudonym_key" {
  secret      = google_secret_manager_secret.secrets["pseudonym-key"].id
  secret_data = random_password.pseudonym_key.result
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.secrets["db-password"].id
  secret_data = random_password.db.result
}
