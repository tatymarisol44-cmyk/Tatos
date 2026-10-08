variable "project_id" {
  description = "GCP project of this environment (one project per environment)."
  type        = string
}

variable "env" {
  description = "staging or production."
  type        = string
  validation {
    condition     = contains(["staging", "production"], var.env)
    error_message = "env must be staging or production."
  }
}

variable "region" {
  description = "Region of the cluster and the database (owner decision O9: data residency)."
  type        = string
  default     = "us-east1"
}

variable "backup_region" {
  description = "Region of the disaster copy: different from `region`."
  type        = string
  default     = "us-central1"
  validation {
    condition     = length(var.backup_region) > 0
    error_message = "backup_region is required."
  }
}

variable "github_repository" {
  description = "OWNER/REPO allowed to deploy (Workload Identity Federation)."
  type        = string
}

variable "admin_cidrs" {
  description = "Networks allowed to reach the Kubernetes API (an office or a VPN)."
  type        = list(string)
  default     = []
}

variable "db_tier" {
  description = "Cloud SQL machine tier."
  type        = string
  default     = "db-custom-2-7680"
}

variable "anchor_retention_days" {
  description = "WORM retention of audit anchors (HIPAA keeps audit records six years)."
  type        = number
  default     = 2190
}

variable "backup_retention_days" {
  description = "Retention of the disaster copies."
  type        = number
  default     = 35
}

variable "lock_retention" {
  description = "Lock the bucket retention policies. IRREVERSIBLE: set true only once anchors flow."
  type        = bool
  default     = false
}
