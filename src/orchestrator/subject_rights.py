"""Data-subject rights (GDPR Art. 15/17/20, HIPAA access, LOPDP): export everything held
about a person, or erase it store by store and say what is retained and why.

Extracted from the orchestrator (audit 2026-10-08, item 7): its dependencies are explicit.
The conversation store is a port (`ThreadPort`); the orchestrator implements it, so this
module knows nothing about LangGraph checkpoints."""

from __future__ import annotations

from typing import Any, Protocol

from orchestrator.auth import PrincipalStore
from orchestrator.campaigns import CampaignService
from orchestrator.clinical_files import ClinicalFiles
from orchestrator.clinical_records import ClinicalRecords
from orchestrator.crm import CrmService
from orchestrator.governance import AuditLog, ConsentRegistry, ReviewQueue, SubjectThreads
from orchestrator.instruments import Instruments
from orchestrator.memory import SemanticMemory


class ThreadPort(Protocol):
    async def thread_state(self, tenant: str, thread_id: str) -> tuple[dict[str, Any], bool]: ...

    async def archive_if_clinical(self, tenant: str, thread_id: str, reason: str) -> bool: ...

    async def delete_thread(
        self, tenant: str, thread_id: str, *, reason: str = ..., archive: bool = ...
    ) -> bool: ...


class SubjectRights:
    def __init__(
        self,
        threads: ThreadPort,
        subject_threads: SubjectThreads,
        principals: PrincipalStore,
        consents: ConsentRegistry,
        memory: SemanticMemory,
        crm: CrmService,
        clinical: ClinicalRecords,
        instruments: Instruments,
        files: ClinicalFiles,
        campaigns: CampaignService,
        reviews: ReviewQueue,
        audit: AuditLog,
    ) -> None:
        self.threads = threads
        self.subject_threads = subject_threads
        self.principals = principals
        self.consents = consents
        self.memory = memory
        self.crm = crm
        self.clinical = clinical
        self.instruments = instruments
        self.files = files
        self.campaigns = campaigns
        self.reviews = reviews
        self.audit = audit

    async def _conversations(self, tenant: str, subject_id: str) -> list[dict[str, Any]]:
        out = []
        for thread_id in await self.subject_threads.threads(tenant, subject_id):
            values, paused = await self.threads.thread_state(tenant, thread_id)
            if not values:
                continue
            out.append(
                {
                    "thread_id": thread_id,
                    "messages": values.get("messages", []),
                    "paused_for_review": paused,
                }
            )
        return out

    async def export(self, tenant: str, subject_id: str, actor: str) -> dict[str, Any]:
        """Everything held about one subject, in a portable structure: every store listed
        in `inventory`, the audit history complete (read in pages, never truncated)."""
        access = [p for p in await self.principals.list(tenant) if p["subject_id"] == subject_id]
        data = {
            "subject_id": subject_id,
            "consents": await self.consents.get(tenant, subject_id),
            "memory": [f.to_dict() for f in await self.memory.export(tenant, subject_id)],
            "crm": await self.crm.export_subject(tenant, subject_id),
            "clinical_record": await self.clinical.export_subject(tenant, subject_id),
            "instrument_results": await self.instruments.export_subject(tenant, subject_id),
            "clinical_files": await self.files.export_subject(tenant, subject_id),
            "campaign_messages": await self.campaigns.export_subject(tenant, subject_id),
            "conversations": await self._conversations(tenant, subject_id),
            "reviews": [r.to_dict() for r in await self.reviews.for_subject(tenant, subject_id)],
            "access_keys": access,
            "audit": [e.to_dict() for e in await self.audit.all_for_subject(tenant, subject_id)],
        }
        data["inventory"] = {
            store: len(value) if isinstance(value, list | dict) else int(value is not None)
            for store, value in data.items()
            if store != "subject_id"
        }
        await self.audit.record(
            tenant,
            actor,
            "subject.exported",
            "subject",
            subject_id=subject_id,
            details={"inventory": data["inventory"]},
        )
        return data

    async def erase(self, tenant: str, subject_id: str, actor: str) -> dict[str, Any]:
        """Right to erasure, store by store. Conversations (including drafts waiting for a
        review), memory, consents, marketing history, contact data and access keys go.
        What stays is listed in `retained` with its basis: the clinical record (restricted,
        GDPR Art. 17(3)(b)/(c), HIPAA and local retention rules) and the audit trail that
        proves the erasure happened."""
        conversations = archived = 0
        for thread_id in await self.subject_threads.threads(tenant, subject_id):
            archived += await self.threads.archive_if_clinical(tenant, thread_id, "erasure_request")
            conversations += await self.threads.delete_thread(tenant, thread_id, archive=False)
        reviews = 0
        for review in await self.reviews.for_subject(tenant, subject_id):  # unlinked leftovers
            await self.reviews.delete_thread(tenant, review.thread_id)
            reviews += 1
        result: dict[str, Any] = {
            "patient_access_keys_revoked": await self.principals.revoke_subject(
                tenant, subject_id, actor
            ),
            "conversations": conversations,
            "clinical_conversations_archived": archived,
            "reviews": reviews,
            "memory_facts": await self.memory.erase(tenant, subject_id),
            "consents": await self.consents.erase(tenant, subject_id),
            "campaign_messages_anonymised": await self.campaigns.erase_subject(tenant, subject_id),
            "crm": await self.crm.erase_subject(tenant, subject_id),
        }
        result["retained"] = [
            {
                "store": "audit_trail",
                "basis": "proof of processing and of this erasure (GDPR Art. 5(2), "
                "HIPAA 164.316(b)(2): six years)",
            },
        ]
        if result["crm"].get("clinical_record") == "retained" or archived:
            result["retained"].append(
                {
                    "store": "clinical_record",
                    "basis": "legal retention of health records (GDPR Art. 17(3)(b)/(c), "
                    "LOPDP, local health law); restricted: no marketing, no updates. "
                    "Includes conversations with clinical content",
                }
            )
        await self.audit.record(
            tenant, actor, "subject.erased", "subject", subject_id=subject_id, details=result
        )
        return result
