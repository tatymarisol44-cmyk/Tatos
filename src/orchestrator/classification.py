"""Data classification (audit 2026-10-08, item 2): one class per table and per clinical
document kind, and one policy per class saying which sinks the data may reach.

The policy is the single source of truth. `surfaces.HARD_EXCLUSIONS` is derived from it,
`packs.Document` validates against it, and `tests/test_classification.py` fails when a new
table or document kind has no class, so nothing is stored before someone decides how
sensitive it is.

A sink is a place that reuses data beyond the record it lives in:

- rag:          shared retrieval (the tenant's knowledge base, answers to any staff member)
- memory:       the preference memory that personalises later chats
- insights:     SQL aggregates and their narration
- campaigns:    marketing segments and messages
- models:       model calls, training or evaluation outside an approved clinical workflow
- audit_export: the data-subject export (right of access / portability)

The classes, from least to most restricted:

PUBLIC         published by the business on purpose (prices, opening hours, posts)
INTERNAL       the business's own working data, no person in it (FAQ, protocols, packs)
PERSONAL       identifies a person, not health (contact data, consents, opt-outs)
HEALTH         health data: appointments, treatments, the clinical record, alerts
PATIENT_ENTRY  written by the patient about themselves (diary, audios): theirs to export,
               never reused
PSYCHOTHERAPY  psychotherapy notes: the author's working notes, reused nowhere and kept
               out of the export until the lawyer answers question D2 (owner decision P8)
CREDENTIAL     key hashes and secret references: never leave the auth path
AUDIT          the hash-chained audit trail: export only, for the subject's own events
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

Sink = Literal["rag", "memory", "insights", "campaigns", "models", "audit_export"]
SINKS: tuple[Sink, ...] = ("rag", "memory", "insights", "campaigns", "models", "audit_export")


class DataClass(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PERSONAL = "personal"
    HEALTH = "health"
    PATIENT_ENTRY = "patient_entry"
    PSYCHOTHERAPY = "psychotherapy"
    CREDENTIAL = "credential"
    AUDIT = "audit"


# Where each class MAY go. Anything not listed is refused.
ALLOWED: dict[DataClass, frozenset[Sink]] = {
    DataClass.PUBLIC: frozenset(SINKS),
    DataClass.INTERNAL: frozenset({"rag", "insights", "models"}),
    # Contact data reaches campaigns only behind the marketing consent (campaigns.py);
    # memory holds a closed vocabulary of preferences, never free text (memory.py).
    DataClass.PERSONAL: frozenset({"memory", "insights", "campaigns", "audit_export"}),
    # Insights take counts, never text, and drop restricted patients (insights.py).
    DataClass.HEALTH: frozenset({"insights", "audit_export"}),
    DataClass.PATIENT_ENTRY: frozenset({"audit_export"}),
    DataClass.PSYCHOTHERAPY: frozenset(),
    DataClass.CREDENTIAL: frozenset(),
    DataClass.AUDIT: frozenset({"audit_export"}),
}


def allowed(data_class: DataClass, sink: Sink) -> bool:
    return sink in ALLOWED[data_class]


def excluded(data_class: DataClass) -> tuple[Sink, ...]:
    """Every sink this class is shut out of, in `SINKS` order."""
    return tuple(s for s in SINKS if s not in ALLOWED[data_class])


# Clinical document kinds (packs.DocumentKind). `None` is company content uploaded to the
# knowledge base without a kind: an FAQ, a price list.
DOCUMENT_CLASS: dict[str | None, DataClass] = {
    None: DataClass.INTERNAL,
    "consent": DataClass.HEALTH,
    "record": DataClass.HEALTH,
    "note": DataClass.HEALTH,
    "referral": DataClass.HEALTH,
    "report": DataClass.HEALTH,
    "certificate": DataClass.HEALTH,
    "prescription": DataClass.HEALTH,
    "controlled_prescription_worksheet": DataClass.HEALTH,
    "patient_entry": DataClass.PATIENT_ENTRY,
    "psychotherapy_note": DataClass.PSYCHOTHERAPY,
}


def document_class(kind: str | None) -> DataClass:
    """The class of a document kind; an unknown kind is treated as the strictest class,
    so a kind added to the packs before it is classified fails closed."""
    return DOCUMENT_CLASS.get(kind, DataClass.PSYCHOTHERAPY)


# Every table, by the most sensitive data a row may hold.
TABLE_CLASS: dict[str, DataClass] = {
    "alert_notifications": DataClass.HEALTH,  # who was told about a crisis alert
    "audit_events": DataClass.AUDIT,
    "audit_heads": DataClass.AUDIT,
    "campaign_recipients": DataClass.PERSONAL,
    "campaigns": DataClass.INTERNAL,
    "channel_accounts": DataClass.CREDENTIAL,  # secret_ref names a credential
    "channel_alerts": DataClass.HEALTH,  # crisis wording was detected
    "channel_auto_replies": DataClass.PERSONAL,  # pseudonymous number + time, no text
    "channel_optouts": DataClass.PERSONAL,
    "clinical_documents": DataClass.PSYCHOTHERAPY,  # holds psychotherapy notes among others
    "clinical_files": DataClass.PSYCHOTHERAPY,  # may hold an author-only document
    "consents": DataClass.PERSONAL,
    "contact_budget": DataClass.PERSONAL,
    "crm_appointments": DataClass.HEALTH,
    "crm_clinical_notes": DataClass.HEALTH,
    "crm_patients": DataClass.HEALTH,  # a patient of a clinic: the fact itself is health data
    "crm_treatments": DataClass.HEALTH,
    "inbound_events": DataClass.PERSONAL,  # pseudonymous sender, never the text
    "instrument_results": DataClass.HEALTH,  # a patient's answers and scores
    "instruments": DataClass.INTERNAL,  # definitions: items, scoring, no patient data
    "knowledge_documents": DataClass.INTERNAL,
    "knowledge_versions": DataClass.INTERNAL,
    "model_spend": DataClass.INTERNAL,  # cost per tenant and month
    "on_call_contacts": DataClass.PERSONAL,
    "principals": DataClass.CREDENTIAL,
    # A rights request or a breach names a clinic's patient: the fact is health data.
    "privacy_case_steps": DataClass.HEALTH,
    "privacy_cases": DataClass.HEALTH,
    "professionals": DataClass.PERSONAL,
    "professional_hours": DataClass.INTERNAL,  # working hours, no patient
    "publications": DataClass.PUBLIC,
    "reviews": DataClass.HEALTH,  # a held answer may contain clinical advice
    "subject_threads": DataClass.HEALTH,
    "thread_leases": DataClass.INTERNAL,
    "whatsapp_offers": DataClass.PERSONAL,  # pseudonymous number + offered times, no text
}
