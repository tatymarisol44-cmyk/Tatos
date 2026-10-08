# ADR 0017: Telling the on-call person, and escalating

**Status:** accepted (2026-10-07, owner's decision P5).

## Context

Incoming WhatsApp messages with crisis wording, or asking for a person, open an alert (ADR 0015, M4), but nobody was told: someone had to be looking at the console. In a crisis every minute counts. The owner chose to notify on all three channels at once (Telegram, WhatsApp and e-mail) and to escalate after 5 minutes.

## Decision

1. **An on-call list per establishment, by level** (`on_call_contacts`): 1 is whoever is on duty, 2 the backup, up to 5. Each contact has any of Telegram chat id, WhatsApp number, e-mail. Managed by admins, readable by care staff, every change audited.
2. **A new alert notifies level 1 at once**, from the webhook itself; every channel the contact has, concurrently.
3. **A crisis alert nobody acknowledges within `ALERT_ESCALATION_MINUTES` (5) goes to the next level**, and so on; an empty level is skipped at once; reaching the end unattended is logged and audited. A request to talk to a person notifies level 1 only. `POST /v1/social/alerts/{id}/ack` ("I am on it") or resolving stops it.
4. **Once, on any number of replicas.** Each step is claimed by a conditional UPDATE on (level, due time) before anything is sent; the periodic worker runs on every replica and retries a step left due by a replica that died.
5. **No patient data in a notice**: the kind of alert, a reference and the console address. Not the message (never stored) and not the caller's number (shown in the console, where reading it is audited).
6. **Channels.** Telegram uses the existing bot (dry run without a token). WhatsApp to staff is outside any customer-service window, so it needs an approved template whose one body parameter is the alert reference (Meta's template guide, read on 2026-10-07); without `WHATSAPP_ALERT_TEMPLATE` it is skipped, and it uses the establishment's connected WhatsApp account. E-mail goes over SMTP with STARTTLS, never in clear text; without `SMTP_HOST` it runs dry. Each outcome (sent, dry_run, skipped, failed, uncertain, none) is recorded per contact and channel (`alert_notifications`); a failed channel does not stop the others.

## Consequences

- A crisis reaches a person within seconds, and a second person within minutes if the first does not answer.
- Outside the system's control: phone reachability, Meta's template approval (A4), the SMTP provider, and whether staff read Telegram at night. The list should hold at least two levels.
- Not built: a voice call or SMS channel, on-call schedules (shifts that change the list automatically), and an alerts view in the web console.
