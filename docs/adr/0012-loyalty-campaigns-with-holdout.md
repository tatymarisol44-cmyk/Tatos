# ADR 0012: Loyalty campaigns with compliance checks, approval and a holdout group

**Status:** accepted

## Context

The retention stage of the e-CRM cycle (acquire, retain, extend) is where small businesses lose the most value: dormant customers, unfinished treatment plans, missed recalls. AI helps with segmentation, personalisation and send time (Cáceres 2023), but health marketing is regulated: HIPAA marketing rules, GDPR/LOPDP consent for special-category data, and advertising codes that forbid promised results. Measuring impact as "messages sent" or "opens" says nothing about whether the campaign changed behaviour.

## Decision

- Lifecycle: `draft` (non-compliant copy) → `pending_approval` → `approved` (a person; discounts above the pack's cap also need `owner_approval`) → `sent`, or `cancelled`. Approval and sending claim their state atomically, so a campaign is sent once.
- Copy checks (`risk.check_copy` plus campaign rules): the pack's banned claims, clinical details for health packs, `{first_name}` as the only placeholder, and a mandatory "reply STOP" opt-out. Drafts from the LLM copywriter get the opt-out appended if it is missing.
- Recipients are fixed at creation from an insights segment (or `recall_due` / `pending_treatment` alerts), excluding restricted records. At send time each one needs the `marketing` consent (checked again then, because it can be withdrawn), an address on the channel and room under the pack's monthly cap.
- **Holdout:** the control arm is chosen by `sha256(campaign, subject) % 100 < holdout_pct`, which is reproducible and independent of order. Control members receive nothing but get the same reference time.
- **Results:** booking rate per arm within `CAMPAIGN_CONVERSION_WINDOW_DAYS`, absolute and relative lift, and a two-proportion z-test. With fewer than 30 per arm the conclusion reads "inconclusive" instead of reporting an effect.
- Channel: Telegram Bot API. Without `TELEGRAM_BOT_TOKEN` it runs dry (outcome `dry_run`). Delivery errors are recorded by exception type only; the bot token (which is part of the URL) is never logged. Batch queries load contacts, consents and caps once per campaign, not once per recipient.

## Consequences

- Campaign impact is measured as causal lift, not activity, which is the metric a clinic owner can act on.
- Eligibility filters apply only to the treatment arm, which slightly favours the control arm, so the measured lift is conservative. This is documented in the results docstring.
- WhatsApp Business, e-mail, Facebook and Instagram are not yet channels. Meta's APIs need app review, and health ads cannot target health conditions. Paid ads would go through the same approval queue before any spend.
- Send-time optimisation per subject is future work: it needs response-time history, which the channel adapters do not collect yet.
