# ADR 0023: One inbox for WhatsApp, Instagram and Facebook, with messages sealed at rest

**Status:** accepted (2026-10-14). The owner asked for WhatsApp, Instagram and Facebook in the app and for the AI to help answer ("sellemos todos esos huecos", requirements RF-MSG-03 to RF-MSG-05 of the SRS delivered on 2026-10-13). The design is mine, under the owner's authority. It changes a promise made in ADR 0015 and in the legal drafts, so the lawyer reviews it before real patients (see Consequences).

## Context

Until now only WhatsApp came in (ADR 0015 M4), and its text was never stored: each message was classified on arrival, a crisis or a request for a person opened an alert, and the text was dropped. That kept the database free of what patients write, but it left the professional with nothing to read: she saw "someone wrote", not what they wrote, and Instagram and Facebook messages did not arrive at all.

What was read on 2026-10-14 (Meta developer documentation):

- Messenger and Instagram Direct notifications come through the same Webhooks product as WhatsApp, signed the same way (`X-Hub-Signature-256`, HMAC-SHA256 of the raw body with the App Secret). The object is `page` (Messenger) or `instagram`; each `entry[].messaging[]` event has `sender.id`, `recipient.id` (our Page or professional account) and `message.mid` / `message.text`. Our own sends come back as echoes (`is_echo`).
- Replies: `POST /{PAGE-ID}/messages` on `graph.facebook.com` with `recipient.id`, `messaging_type: RESPONSE` and `message.text` (Messenger); `POST /{IG-ID}/messages` on `graph.instagram.com` (Instagram API with Instagram Login). A free-form reply is allowed within 24 hours of the person's last message.
- Instagram and Messenger ids are scoped numbers, not phone numbers.

## Decision

1. **One webhook, three products.** `/v1/channels/meta` (and the existing `/v1/channels/whatsapp`, same handler) accepts WhatsApp, Messenger and Instagram notifications, with the same signature check, deduplication by message id, classification (`crisis.classify`), STOP opt-out and alerts as WhatsApp. Echoes are skipped.
2. **Messages are kept, sealed.** Every message in and out goes into `inbox_messages`, encrypted with AES-256-GCM (`orchestrator/sealing.py`):
   - the key is derived with HKDF-SHA256 from `PSEUDONYM_KEY` and a purpose label, so no new secret is needed and the pseudonym key itself never encrypts; like that key, it lives outside the database and its backups;
   - the tenant is the associated data, so a sealed text moved to another practice's row does not open;
   - who wrote is kept sealed too (`address_sealed`, to reply), plus the keyed pseudonym that finds the conversation again (`address_key`, HMAC as for opt-outs). A database copy shows no text, no number and no platform id (`tests/test_inbox.py`).
3. **Need to know.** Every inbox route is for the clinician role (`require_clinician`): admin, reception and service keys get 403. Listing and opening are audited (`inbox.list`, `inbox.read`), without content.
4. **The AI drafts, a person sends.** `POST /v1/inbox/{id}/draft` asks the WhatsApp assistant's model (same prompt and the same deterministic checks, `unsafe_reason`: no clinical or therapeutic advice, no links) for a suggestion, stored as a draft. Nothing is sent until the professional sends it, as is or edited. No draft in a conversation flagged as a crisis or a request for a person; an unsafe suggestion is withheld. A patient is recognised by phone number on WhatsApp only: a social id is never compared with phone numbers.
5. **Automatic replies on Instagram and Facebook are the fixed texts only**: crisis containment with the emergency lines, "a person will write to you", and the STOP confirmation (`SOCIAL_AUTO_REPLY`, on by default). Everyday questions there wait for the professional, helped by a draft.
6. **Replies** go out only inside the 24-hour window counted from the person's last message, never to someone who wrote STOP, and record their outcome (`sent`, `dry_run` without a credential, `failed`, `uncertain` after a transport error, never retried). The audit has the outcome, not the text.
7. **Patient record and rights.** A conversation can be linked to a patient. An access request exports the linked conversations; an erasure request deletes them. Closing a conversation clears its crisis or person flag.
8. **Retention.** Messages older than `INBOX_RETENTION_DAYS` (90 by default) are deleted by `agency retention`, and conversations left empty go with them. Anything clinical worth keeping is written into the clinical record by the professional.

## Consequences

- The database now holds what patients write, sealed. The legal drafts said "the WhatsApp text is not stored"; they are updated to say what is kept, why, for how long and who reads it (`docs/legal/es/`). **The lawyer reviews this change before real patients** (`docs/LAUNCH.md`).
- Losing `PSEUDONYM_KEY` makes the inbox unreadable (and the opt-outs unmatchable). It must be backed up outside the database, as already required.
- Rotating the key needs a re-seal pass; values carry a version prefix (`v1:`) for that.
- Meta must grant the messaging permissions after App Review (`instagram_business_manage_messages`, `pages_messaging`) before Instagram and Facebook messages arrive in production; until then the code is ready and the tests use synthetic payloads.
- Drafts send the last message to the model provider, as the WhatsApp assistant does: the provider must be under a data processing agreement first (ADR 0020).
