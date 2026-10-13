# Database certificate authorities

Roots the API verifies its database servers against (`sslmode=verify-full`). The image
ships this folder at `/app/certs` (Dockerfile; `.dockerignore` lets only this part of
`deploy/` in). They are public certificates, not secrets.

## supabase-root-2021.crt

Supabase's own root: the session pooler (`*.pooler.supabase.com`) presents a chain
`*.pooler.supabase.com` ← Supabase Intermediate 2021 CA ← Supabase Root 2021 CA, which no
public trust store contains.

- SHA-256 fingerprint:
  `80:70:25:AD:50:D4:ED:21:9D:2C:9C:7D:29:9C:00:4F:82:4E:B0:0C:F7:F6:5A:FE:F6:07:D0:7B:72:E6:CA:FA`
- Valid until 2031-04-26.
- Provenance (2026-10-13): taken from the chain the live pooler presented, and checked
  byte for byte against a copy published independently months earlier (a public Hugging
  Face Space commit of Supabase's `prod-ca-2021.crt`). The same file is what Supabase's
  dashboard offers under Database → Settings → SSL Configuration → Download certificate.
- Checked live: the pooler connects with this root and `verify-full`, and is refused with
  public CAs only (`certificate verify failed`).
- `tests/test_render_setup.py` pins the fingerprint: replacing the file fails CI.

Before 2031, or if Supabase rotates its CA, replace the file from the dashboard and update
the fingerprint here and in the test.
