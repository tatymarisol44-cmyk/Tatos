# ADR 0010: Long-term semantic memory per data subject

**Status:** accepted

## Context

A clinic or shop talks to the same customers many times. Short durable facts, such as "prefers afternoon appointments", "reminders by Telegram" or "anxious about treatment, explain each step", make later conversations better, but conversation threads are per session and purged after the retention period. Storing facts about people is personal data processing, and in health it can become special-category data (GDPR Art. 9, HIPAA, Ecuador LOPDP).

## Decision

- `memory.py`: facts are embedded and stored per `(tenant, subject_id)` in a vector store with the same layout as the knowledge base: one Qdrant collection partitioned by tenant (`is_tenant`), with a subject index and a numeric expiry filtered server-side. Ockerman et al.'s Qdrant study (arXiv:2509.12384) found that sharding across workers only pays off above tens of GB; per-tenant memory is orders of magnitude smaller, so a single node with payload partitioning is the right shape.
- Graph: `recall_memory` runs before retrieval and `remember` after `finalize`. Both run only for requests with a `subject_id` whose `memory` consent is granted, never after a rejected review, and neither can fail the request.
- Extraction uses the small router model with a prompt that excludes health data, payment and contact details. A deterministic filter drops, whatever the model said, facts with injection patterns, facts with PII (contact data belongs in the CRM) and clinical facts unless the pack allows them. The per-turn cap is applied after that filter, so inadmissible facts cannot crowd out a valid one.
- A near-duplicate fact (similarity ≥ `MEMORY_DEDUPE_SCORE`) replaces the previous version and keeps its id.
- Every fact records its source conversation and expires after `MEMORY_TTL_DAYS`; `agency purge-threads` also purges expired facts. Subject export and erasure include memory.
- Recalled facts enter the prompt as delimited, untrusted data, like retrieved documents.

## Consequences

- One extra small-model call per turn for consenting subjects.
- A subject id is pseudonymous by contract (a patient number); names and e-mails must not be used as ids. This is documented in the API schema.
- Clinical memory for treatment purposes, with clinician-only access, would need role-based access control, which is future work.
