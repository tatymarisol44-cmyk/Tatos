# --- supply chain: images in the project, admitted only when attested (O4, O6) -----------
#
# deploy-gke.yml verifies the CI signature, copies each image by digest into this
# repository and attests the digest with the KMS key below. The cluster's Binary
# Authorization policy refuses any image without that attestation, including one set by
# hand with `kubectl set image`. Break-glass: docs/runbooks/break-glass.md.

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "images"
  format        = "DOCKER"
  description   = "Images admitted to the cluster, copied by digest by the deploy job"
  docker_config {
    immutable_tags = true
  }
  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "deployer_writes_images" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

# Autopilot nodes pull with the project's default compute service account.
resource "google_artifact_registry_repository_iam_member" "nodes_read_images" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
}

resource "google_kms_key_ring" "binauthz" {
  name       = "${local.name}-binauthz"
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "attestor" {
  name     = "attestor"
  key_ring = google_kms_key_ring.binauthz.id
  purpose  = "ASYMMETRIC_SIGN"
  version_template {
    algorithm = "EC_SIGN_P256_SHA256"
  }
  lifecycle {
    prevent_destroy = true
  }
}

data "google_kms_crypto_key_version" "attestor" {
  crypto_key = google_kms_crypto_key.attestor.id
}

resource "google_container_analysis_note" "attestor" {
  name = "${local.name}-deployed-by-ci"
  attestation_authority {
    hint {
      human_readable_name = "Signed by CI and copied by the deploy job of ${var.github_repository}"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_binary_authorization_attestor" "deploy" {
  name = "${local.name}-deploy"
  attestation_authority_note {
    note_reference = google_container_analysis_note.attestor.name
    public_keys {
      id = data.google_kms_crypto_key_version.attestor.id
      pkix_public_key {
        public_key_pem      = data.google_kms_crypto_key_version.attestor.public_key[0].pem
        signature_algorithm = data.google_kms_crypto_key_version.attestor.public_key[0].algorithm
      }
    }
  }
}

# Google's own system images (kube-system, Managed Prometheus...) pass the global policy;
# every other image needs our attestation.
resource "google_binary_authorization_policy" "this" {
  global_policy_evaluation_mode = "ENABLE"
  default_admission_rule {
    evaluation_mode         = "REQUIRE_ATTESTATION"
    enforcement_mode        = "ENFORCED_BLOCK_AND_AUDIT_LOG"
    require_attestations_by = [google_binary_authorization_attestor.deploy.name]
  }
  depends_on = [google_project_service.apis]
}

# What the deploy job needs to attest: sign with the key, attach to the note.
resource "google_kms_crypto_key_iam_member" "deployer_signs" {
  crypto_key_id = google_kms_crypto_key.attestor.id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_container_analysis_note_iam_member" "deployer_attaches" {
  note   = google_container_analysis_note.attestor.name
  role   = "roles/containeranalysis.notes.attacher"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "deployer_occurrences" {
  project = var.project_id
  role    = "roles/containeranalysis.occurrences.editor"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_binary_authorization_attestor_iam_member" "deployer_views_attestor" {
  attestor = google_binary_authorization_attestor.deploy.name
  role     = "roles/binaryauthorization.attestorsViewer"
  member   = "serviceAccount:${google_service_account.deployer.email}"
}
