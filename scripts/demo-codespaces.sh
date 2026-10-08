#!/usr/bin/env bash
# One command for the MVP demo in GitHub Codespaces (or any Linux with Docker):
#   bash scripts/demo-codespaces.sh
# Postgres and Qdrant run in containers; the API runs from the locked environment (uv);
# a synthetic psychology practice is seeded and every flow walked through the real API.
#
# Everything optional comes from Codespaces secrets (Settings -> Codespaces -> Secrets);
# see docs/DEMO.md. Without them every flow still runs, as a rehearsal that sends nothing.
#   ANTHROPIC_API_KEY                         real answers from Claude
#   TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID     the campaign sent live, to you only
#   META_APP_SECRET + WHATSAPP_VERIFY_TOKEN   your Meta app (WhatsApp webhook)
#   WHATSAPP_PHONE_NUMBER_ID + SOCIAL_SECRET_WA_DEMO   WhatsApp live (warm replies, alerts)
#   INSTAGRAM_USER_ID + SOCIAL_SECRET_IG_DEMO          Instagram posts live
#   FACEBOOK_PAGE_ID + SOCIAL_SECRET_FB_DEMO           Facebook Page photos live
#   TIKTOK_ACCOUNT_ID + SOCIAL_SECRET_TT_DEMO          TikTok (private until TikTok's audit)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/4 databases (Postgres 17, Qdrant)"
docker compose up -d postgres qdrant
for _ in $(seq 1 60); do
  docker compose exec -T postgres pg_isready -U agency -d agency >/dev/null 2>&1 && break
  sleep 1
done

echo "== 2/4 configuration (demo only: synthetic data)"
STATE=.demo
mkdir -p "$STATE"
[ -f "$STATE/service-key" ] || python3 -c "import secrets;print('demo_'+secrets.token_urlsafe(24))" > "$STATE/service-key"
[ -f "$STATE/pseudonym-key" ] || python3 -c "import secrets;print(secrets.token_urlsafe(48))" > "$STATE/pseudonym-key"
set -a
APP_ENV=dev
API_KEYS="$(cat "$STATE/service-key"):demo"
TENANT_PACKS='{"demo": "ec-psychologist"}'
PSEUDONYM_KEY="$(cat "$STATE/pseudonym-key")"
# Your Meta app's secret and verify token if you set them; generated ones otherwise.
META_APP_SECRET="${META_APP_SECRET:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')}"
WHATSAPP_VERIFY_TOKEN="${WHATSAPP_VERIFY_TOKEN:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')}"
DATABASE_URL=postgresql+psycopg://agency:agency@127.0.0.1:5432/agency
POSTGRES_URL=postgresql://agency:agency@127.0.0.1:5432/agency
CHECKPOINTER_BACKEND=postgres
DB_AUTO_MIGRATE=true
VECTOR_BACKEND=qdrant
QDRANT_URL=http://127.0.0.1:6333
EMBEDDING_BACKEND=hashing
LLM_FALLBACK_MODELS=[]
RATE_LIMIT_PER_MINUTE=5000
OTEL_ENABLED=false
METRICS_PORT=0
MEDIA_DIR="$PWD/$STATE/media"
PRACTICE_DISPLAY_NAME="${PRACTICE_DISPLAY_NAME:-Consultorio Demo}"
set +a
# In a Codespace the API has a public https address: Meta fetches images from it and
# sends WhatsApp messages to it.
if [ -n "${CODESPACE_NAME:-}" ] && [ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]; then
  export PUBLIC_BASE_URL="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "   ANTHROPIC_API_KEY not set: using the offline fake model (flows work, answers are canned)"
  export LLM_BACKEND=fake
else
  export LLM_BACKEND=litellm
fi

echo "== 3/4 API on :8000"
uv sync --locked >/dev/null
uv run agency serve --host 0.0.0.0 --port 8000 > "$STATE/api.log" 2>&1 &
echo $! > "$STATE/api.pid"
for _ in $(seq 1 90); do
  curl -sf http://127.0.0.1:8000/readyz >/dev/null && break
  sleep 1
done
curl -sf http://127.0.0.1:8000/readyz >/dev/null || { echo "API did not start:"; tail -40 "$STATE/api.log"; exit 1; }

# Meta can only reach a public port. Opened only when a Meta channel is configured; every
# API route still needs a key, and the media URLs are signed and expire.
if [ -n "${CODESPACE_NAME:-}" ] && { [ -n "${WHATSAPP_PHONE_NUMBER_ID:-}" ] || [ -n "${INSTAGRAM_USER_ID:-}" ]; }; then
  if gh codespace ports visibility 8000:public -c "$CODESPACE_NAME" >/dev/null 2>&1; then
    echo "   port 8000 is public (needed by Meta)"
  else
    echo "   make port 8000 public by hand: Ports tab -> right click -> Port visibility -> Public"
  fi
fi

echo "== 4/4 synthetic practice and every flow"
args=(--url http://127.0.0.1:8000 --key "$(cat "$STATE/service-key")" --meta-app-secret "$META_APP_SECRET")
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  args+=(--telegram-chat-id "$TELEGRAM_CHAT_ID")
fi
if [ -n "${WHATSAPP_PHONE_NUMBER_ID:-}" ]; then args+=(--whatsapp-id "$WHATSAPP_PHONE_NUMBER_ID"); fi
if [ -n "${INSTAGRAM_USER_ID:-}" ]; then args+=(--instagram-id "$INSTAGRAM_USER_ID"); fi
if [ -n "${FACEBOOK_PAGE_ID:-}" ]; then args+=(--facebook-id "$FACEBOOK_PAGE_ID"); fi
if [ -n "${TIKTOK_ACCOUNT_ID:-}" ]; then args+=(--tiktok-id "$TIKTOK_ACCOUNT_ID"); fi
uv run agency demo "${args[@]}"

echo
if [ -n "${PUBLIC_BASE_URL:-}" ]; then
  echo "WhatsApp webhook for your Meta app (WhatsApp -> Configuration -> Webhook):"
  echo "    Callback URL: ${PUBLIC_BASE_URL}/v1/channels/whatsapp"
  echo "    Verify token: ${WHATSAPP_VERIFY_TOKEN}"
  echo "    Subscribe to: messages"
fi
echo "The API keeps running (log: $STATE/api.log). Stop it: kill \$(cat $STATE/api.pid)"
