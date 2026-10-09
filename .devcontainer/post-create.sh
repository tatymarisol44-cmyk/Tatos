#!/usr/bin/env bash
# Runs once when the codespace is created.
set -euo pipefail

# The agent catalog is a submodule; codespaces do not clone submodules by default.
git submodule update --init --recursive

# docker compose reads .env; never overwrite one the user already filled in.
[ -f .env ] || cp .env.example .env

pip install --quiet uv
uv sync

# Video creatives (TikTok/Reels) are rendered with ffmpeg.
sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg >/dev/null

echo
echo "Ready. MVP demo (synthetic psychology practice, every flow):"
echo "  bash scripts/demo-codespaces.sh"
echo
echo "Other commands:"
echo "  uv run pytest                       # Python suite"
echo "  (cd agents/jvm-specialist && mvn -q verify)   # Java suite"
echo "  docker compose up --build           # full stack -> port 8000"
echo "Add an LLM key to .env (or set LLM_BACKEND=fake) before 'docker compose up'."
