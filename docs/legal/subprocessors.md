# Sub-processors

These are the companies the platform uses to provide the service and what each one receives. The list is Annex B of the processing agreement (`es/contrato-de-encargo.md`). Each clinic authorises it in writing, and is told 30 days before any change, so it can object (SPDP-2025-0006 Annex I, model clause x.4).

**Rule:** a provider receives patient data only if the product cannot work without it, and only under a processing agreement that passes the same obligations on.

| Provider | Service | What it receives | Where | Contract needed before real patients |
|---|---|---|---|---|
| Google Cloud | Kubernetes (GKE), Cloud SQL Postgres, Cloud Storage, Secret Manager, Managed Prometheus | Everything the platform stores, encrypted at rest. This includes clinical records, files, test results, the audit and backups. | `us-east1` (South Carolina, USA); backups in `us-central1` (Iowa, USA) | Google Cloud Data Processing Addendum (accepted in the console); access to Google Cloud's support logs restricted |
| Anthropic | Claude models: answers, routing, the WhatsApp assistant | The text of a turn: a staff or patient chat message, or an everyday WhatsApp message. Never stored by us after the answer; crisis messages never reach it (ADR 0020). | USA | Anthropic's data processing addendum; **zero data retention** requested for the API organisation |
| OpenAI | Embeddings (`text-embedding-3-small`) | The text to index: the practice's knowledge base (company content; personal identifiers are refused) and the chat message used to choose an agent. | USA | OpenAI's data processing addendum; zero data retention requested. Alternative: `EMBEDDING_BACKEND=hashing`, which keeps this text in our cluster at some loss in routing quality |
| Meta Platforms | WhatsApp Business Cloud API; Facebook and Instagram publishing | WhatsApp: the patient's number and the messages exchanged. Publishing: marketing creatives only, never patient data. | Meta's infrastructure | Meta's WhatsApp Business terms, under which Meta processes Cloud API messages on the business's behalf (**TO VERIFY**: the current text and Meta's role) |
| Telegram | Optional channel the patient links on their own | The patient's chat id and the non-clinical texts we send: reminders, staff notices. | Telegram's infrastructure | Telegram offers no processing agreement. So it is used only when the patient links it themself, and it carries **no clinical content** (reminders and on-call notices only). |
| TikTok | Publishing | Marketing videos only, never patient data | TikTok's infrastructure | Platform terms only (no personal data involved) |
| E-mail provider (to choose, decision P11) | On-call notices by e-mail | The on-call staff member's address and an alert reference; no clinical content (ADR 0017) | to choose | Its processing agreement |

## How the clinic is told about changes

- The platform keeps this file as the authoritative list. Each change is a commit, so its history is the record.
- The platform announces changes to the clinics by e-mail 30 days ahead.
- A clinic may object. If no alternative exists, it may end the agreement and receive its data back (agreement, clause 9).

## Why the servers are in the USA

- Hosting with a processor is not an international transfer under Ecuadorian law (SPDP-2026-0004 Art. 23). It is a processing contract (LOPDP Art. 34).
- `us-east1` was chosen for two reasons:
  - it offers every managed service the platform uses, including Cloud SQL point-in-time recovery and Managed Prometheus;
  - it is one of the regions with an always-free storage tier.
- Google Cloud has no region in an Andean Community country. Those countries are the only ones adequate by law (SPDP-2026-0004 Art. 59), so a closer region (Santiago, São Paulo) would not change the legal position.
- Patients are told, in the privacy notice, where their data is processed (LOPDP Art. 12, item 10).
