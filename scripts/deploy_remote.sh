#!/usr/bin/env bash
# Funnel Copilot — VPS deploy, phase 1 (app only; tunnel wiring is phase 2).
# Run FROM YOUR PC:  Get-Content C:\dev\funnel-copilot\scripts\deploy_remote.sh -Raw | ssh vps "bash -s"
# Idempotent: safe to re-run. Never prints secret values.
set -u

APP_DIR="$HOME/apps/funnel-copilot"
REPO_URL="https://github.com/gorkenvm/Funnel-text2SQL-copilot.git"

step() { printf '\n=== %s ===\n' "$*"; }

step "1/6 Prerequisites"
docker --version || { echo "FATAL: docker missing"; exit 1; }
docker compose version || { echo "FATAL: docker compose plugin missing"; exit 1; }

step "2/6 Clone or update repo -> $APP_DIR"
mkdir -p "$HOME/apps"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only || { echo "FATAL: git pull failed"; exit 1; }
else
  git clone "$REPO_URL" "$APP_DIR" || { echo "FATAL: git clone failed"; exit 1; }
fi
git -C "$APP_DIR" log -1 --format='HEAD: %h %s'

step "3/6 .env check (values are never printed)"
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<'EOF'
# Fill these two lines, then re-run the same deploy command.
OPENAI_API_KEY=PUT_YOUR_KEY_HERE
DEMO_PASSPHRASE=PUT_THE_PASSPHRASE_HERE
AGENT_DB=duckdb
EOF
  chmod 600 "$APP_DIR/.env"
  echo "CREATED $APP_DIR/.env with placeholders."
  echo "ACTION NEEDED: edit it (e.g.:  ssh vps  then  nano $APP_DIR/.env ), fill the"
  echo "two values, save, then re-run this same deploy command from your PC."
  exit 2
fi
if grep -q "PUT_YOUR_KEY_HERE\|PUT_THE_PASSPHRASE_HERE" "$APP_DIR/.env"; then
  echo "ACTION NEEDED: $APP_DIR/.env still has placeholders — fill it, then re-run."
  exit 2
fi
echo ".env present, placeholders filled. Perms: $(stat -c '%a' "$APP_DIR/.env")"

step "4/6 Generate synthetic data (skipped if already present)"
cd "$APP_DIR"
if [ -f data/web_events.parquet ]; then
  echo "data/*.parquet already present — skipping datagen."
else
  docker compose --profile datagen run --rm datagen || { echo "FATAL: datagen failed"; exit 1; }
fi
ls -la data/ | sed -n '1,8p'

step "5/6 Build and start the app"
docker compose up -d --build || { echo "FATAL: compose up failed"; exit 1; }
sleep 6
echo "--- /health ---"
curl -sS --max-time 20 http://127.0.0.1:8000/health || { echo "FATAL: health check failed"; docker compose logs --tail 40; exit 1; }
echo

step "6/6 Tunnel diagnostics (paste ALL output below back to the manager)"
echo "--- containers ---"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Networks}}' 2>/dev/null || docker ps
echo "--- cloudflared details ---"
docker inspect cloudflared --format 'Networks: {{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null || echo "no container named cloudflared"
docker inspect cloudflared --format 'Mounts: {{range .Mounts}}{{.Source}} -> {{.Destination}} | {{end}}' 2>/dev/null
# A token-managed tunnel carries its credential in argv, so this line is
# redacted before it is printed: the whole point of this step is that the
# output gets pasted somewhere else, and a tunnel token is enough to run
# traffic for the entire zone. Only the run mode matters for diagnostics.
docker inspect cloudflared --format 'Cmd: {{join .Config.Cmd " "}}' 2>/dev/null \
  | sed -E 's/(--token[= ])[A-Za-z0-9._-]+/\1<REDACTED>/g'
echo
echo "PHASE 1 DONE: app is up on 127.0.0.1:8000 (locked: check the health JSON above)."
echo "Paste this whole output back; phase 2 (funnel.vmgorken.com wiring) depends on it."
