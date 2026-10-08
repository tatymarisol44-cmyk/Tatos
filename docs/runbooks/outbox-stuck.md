# Campaign outbox not draining

**What it means:** campaign messages have waited 30 minutes and the queue is not going down.

1. **Read the logs:** `kubectl -n agency logs deploy/agency-orchestrator --since=30m | grep -i outbox`.
2. **Check the credentials.** The Telegram or WhatsApp credentials (`TELEGRAM_BOT_TOKEN`,
   `SOCIAL_SECRET_*`) may have expired or been revoked.
3. **Resolve uncertain deliveries by hand.** The system never re-sends a delivery marked
   *uncertain*, to avoid duplicates. Resolve each one with
   `POST /v1/campaigns/{id}/recipients/{patient}/delivery`.
