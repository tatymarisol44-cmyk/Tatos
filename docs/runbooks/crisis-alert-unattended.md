# A crisis alert has nobody on it (SLO-3): patient safety

**What it means:** a message with crisis wording arrived on WhatsApp or Telegram. No
professional has acknowledged it for 15 minutes, even though the on-call escalation has
notified every level.

This is not a technical problem first. **Call the clinic's on-call professional by phone now.**

1. **Which tenant?** Use `GET /v1/social/alerts` with that clinic's admin key, or look in the
   audit for `channel_alert.opened` and `channel_alert.unattended`.
2. **Were the notices delivered?** Check `GET /v1/social/alerts/{id}/notifications`. A
   `failed` or `skipped` notice means a channel is misconfigured (bot token, WhatsApp
   template or SMTP).
3. **The system never contacts third parties or emergency services by itself** (ADR 0017).
   The professional decides; the system only makes sure a person knows.
4. **Afterwards,** fix the clinic's on-call list (`/v1/admin/on-call`) so every level has a
   reachable person.
