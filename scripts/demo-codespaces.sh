#!/usr/bin/env bash
# One command for the MVP demo in GitHub Codespaces (or any Linux with Docker):
#   bash scripts/demo-codespaces.sh
# Postgres and Qdrant run in containers; the API runs from the locked environment (uv);
# a synthetic psychology practice is seeded and every flow walked through the real API.
# Secrets come from the environment (Codespaces: Settings -> Codespaces -> Secrets):
#   ANTHROPIC_API_KEY   (required for real answers; without it the fake backend is used)
#   TELEGRAM_BOT_TOKEN  (optional: the engagement campaign is sent live on Telegram)
#   TELEGRAM_CHAT_ID    (optional: your own chat id, the only real recipient)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/4 databases (Postgres 17, Qdrant)"
docker compose up -d postgres qdrant
for _ in $(seq 1 60); do
  docker compose exec -T postgres pg_isready -U agency -d agency >/dev/null 2>&1 && break
  sleep 1
done

echo "== 2/4 configuration (demo only: synthetic data, generated secrets)"
STATE=.demo
mkdir -p "$STATE"
[ -f "$STATE/service-key" ] || python3 -c "import secrets;print('demo_'+secrets.token_urlsafe(24))" > "$STATE/service-key"
[ -f "$STATE/secrets" ] || python3 - > "$STATE/secrets" <<'PY'
import secrets
for name in ("PSEUDONYM_KEY", "META_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"):
    print(f"{name}={secrets.token_urlsafe(32)}")
PY
set -a
# shellcheck disable=SC1091
source "$STATE/secrets"
APP_ENV=dev
API_KEYS="$(cat "$STATE/service-key"):demo"
TENANT_PACKS='{"demo": "ec-psychologist"}'
DATABASE_URL=postgresql+psycopg://agency:agency@127.0.0.1:5432/agency
POSTGRES_URL=postgresql://agency:agency@127.0.0.1:5432/agency
CHECKPOINTER_BACKEND=postgres
DB_AUTO_MIGRATE=true
VECTOR_BACKEND=qdrant
QDRANT_URL=http://127.0.0.1:6333
EMBEDDING_BACKEND=hashing
LLM_FALLBACK_MODELS=[]
RATE_LIMIT_PER_MINUTE=5000
CAMPAIGN_DEFAULT_HOLDOUT_PCT=20
OTEL_ENABLED=false
METRICS_PORT=0
MEDIA_DIR="$PWD/$STATE/media"
set +a
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

echo "== 4/4 synthetic practice and every flow"
args=(--url http://127.0.0.1:8000 --key "$(cat "$STATE/service-key")" --meta-app-secret "$META_APP_SECRET")
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  args+=(--telegram-chat-id "$TELEGRAM_CHAT_ID")
fi
uv run agency demo "${args[@]}"
echo
echo "The API keeps running (log: $STATE/api.log). Stop it: kill \$(cat $STATE/api.pid)"
