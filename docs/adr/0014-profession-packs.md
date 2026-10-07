# ADR 0014: Profession packs for health (Ecuador first)

**Status:** proposed (Phase 0). Becomes accepted when the owner approves it and Phase 1 lands.

## Context

Today a pack (`packs.py`, `pack_data/*.yaml`) is only a *policy*: review rules, memory keys, CRM pipeline, campaign limits and consent pitches. The business now needs one exact application per kind of health professional: psychologist, psychiatrist, dentist, physician. What differs between them is not code but content: which documents they sign, which forms they fill, what the AI may touch, what marketing may say, how long records are kept. The research in [docs/packs/RESEARCH.md](../packs/RESEARCH.md) shows the differences are legal and structural, not cosmetic. The clearest example: the ACESS special prescription is a numbered government form that no software may issue, and a psychologist cannot prescribe at all.

One codebase per specialty would multiply every audit finding by N. So the rule stays "configuration, not code", and the pack grows from a policy into a **profession profile**.

## Decision

1. **Extend `Pack`, do not replace it.** Every new field is optional with a default, so `general`, `retail` and `dental` load unchanged. The existing tests stay green.
2. **New sections** (all declarative YAML, validated by Pydantic):
   - `jurisdiction`: country code (`EC`), legal references. Each reference has `status: read | secondary | to_verify`. `agency pack validate` prints the count of `to_verify` references and `--strict` fails on them in CI for packs marked `production: true`.
   - `profession`: `can_prescribe`, `can_use_controlled_prescription`, `registry` (what licence the professional must hold). A psychologist pack sets both to false.
   - `documents`: a catalog of templates. Each has `kind` (consent, prescription, certificate, report, referral, record, note), `legal_basis`, `signature` (none or professional), `validity_days`, `retention_years`, `access` and `excluded_from` (see rule 4).
   - `forms`: clinical forms (fields, official code such as an MSP form number, scales).
   - `safety`: crisis escalation, minors (guardian consent plus the minor's assent), emergency exceptions.
   - `marketing`: extends `campaigns` with `banned_topics`, `forbid_testimonials`, `forbid_before_after`, allowed content themes and channels.
   - `agents`: which catalog divisions or agent ids the pack may route to.
3. **Inheritance with `extends`.** `ec-psychologist` and `ec-psychiatrist` extend `ec-mental-health-base`; `ec-dentist` and `ec-general-practice` extend `ec-clinic-base`. Merge rules: scalars and lists in the child replace the parent's; dictionaries merge by key; `banned_claims` and `excluded_from` only ever *grow*, a child cannot remove a parent's prohibition (validator).
4. **Rules the pack can tighten but never loosen** (enforced in code, covered by inverted-probe tests):
   - A document with `kind: psychotherapy_note` is `access: author_only` and `excluded_from: [rag, memory, insights, campaigns, models, audit_export]`. A pack that declares it otherwise fails validation.
   - The AI never diagnoses, prescribes or decides treatment, internment or a crisis response. All such output goes to human review (existing `interrupt`).
   - Crisis signals escalate to a person; the AI does not handle the conversation.
   - A controlled-drug prescription is a **worksheet**: the system checks the ACESS-2022-0046 Art. 6 fields, stores the ACESS form number and never produces the legal form.
5. **Tenant model.** A tenant becomes an *establishment* with N *professionals*, each mapped to one pack. Prescription blocks, consumption reports and the legal representative live on the establishment (ACESS-0046 Arts. 8 and 12). **Decided by the owner on 2026-10-07.**
5b. **Signatures.** The professional signs outside the system; the system stores the signed PDF with its SHA-256. No signature provider is integrated in the first version. **Decided 2026-10-07.**
5c. **Patient-authored content.** A new document kind `patient_entry` (emotional diary, audios, exercise answers). Access: the patient and the treating professional only. `excluded_from: [rag, memory, insights, campaigns, models]`, and it never counts as an analytics signal, even with the analytics consent. Crisis wording is routed to the professional's queue and never answered by the AI. The patient sees that it is not monitored in real time. Included in the first version by the owner's decision of 2026-10-07; the legal questions are open (lawyer's questions I1 and I3). On a serious risk the system only alerts the treating professional and the establishment's on-duty contact. It never contacts third parties by itself; the professional decides (LOPDP Arts. 7(6), 26(c), 31(1) allow processing for vital interests only when the person cannot consent, so a blanket automatic action is not supported by the text).
5d. **Session transcription** is not part of this ADR. It stays off until the lawyer answers question I2.
6. **CLI**: `agency pack list | validate [--strict] | show <id> | install <tenant> <id>`.

## Consequences

- One exact application per profession without N code paths; adding a specialty is a YAML pack plus tests.
- Legal uncertainty becomes visible and testable: `to_verify` is a field, not a comment, and CI can refuse to call a pack production-ready while it has any.
- The merge rule makes prohibitions monotonic, which is what an auditor will probe first.
- Cost: the validator and merge logic are real code (Phase 1), and the tenant-model change touches `auth.py`, `service.py` and the database schema (needs an Alembic migration).
- Not decided here: electronic-signature provider, telehealth rules, court reports and scale licences. They are open questions in the research document.
