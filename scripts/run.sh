#!/usr/bin/env bash
# Start the FastAPI server AND a public tunnel together, then print the Meta webhook banner.
#
# Preferred: ngrok with a RESERVED STATIC DOMAIN -> the callback URL never changes, so you
# paste it into Meta > WhatsApp > Configuration ONCE and it survives every restart.
#   1. ngrok config add-authtoken <token>         (one-time, from dashboard.ngrok.com)
#   2. claim a free domain at dashboard.ngrok.com/domains, e.g. cn-sa.ngrok-free.app
#   3. export NGROK_DOMAIN=cn-sa.ngrok-free.app   (put it in .env or your shell)
#
# Fallback: if NGROK_DOMAIN/ngrok aren't set up, a cloudflared quick-tunnel is used instead
# (its URL rotates every run, so you must re-paste it into Meta each time).
#
# The tunnel is stopped when the server exits.
set -uo pipefail

PY="${PY:-.venv/bin/python}"
PORT="${PORT:-8000}"
CF="${CF:-./cloudflared}"

# Pull a single KEY from .env (last match), stripping optional surrounding quotes.
# .env is otherwise read by the Python app; the shell needs these two values too.
_envget() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//'; }

VERIFY_TOKEN="${SA_WA_VERIFY_TOKEN:-$(_envget SA_WA_VERIFY_TOKEN)}"
VERIFY_TOKEN="${VERIFY_TOKEN:-cn-verify-dev}"
NGROK_DOMAIN="${NGROK_DOMAIN:-$(_envget NGROK_DOMAIN)}"
TUN_PID=""

# ngrok binary: prefer ./ngrok, else whatever is on PATH.
if [ -x "./ngrok" ]; then NGROK="./ngrok"; else NGROK="$(command -v ngrok || true)"; fi

cleanup() { [ -n "$TUN_PID" ] && kill "$TUN_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

print_banner() {  # $1 = public base URL
  printf '\n'
  echo "========================  META WEBHOOK  ========================"
  echo "  Callback URL (copy this line):"
  echo
  echo "    ${1}/webhooks/whatsapp"
  echo
  echo "  Verify token:  ${VERIFY_TOKEN}"
  echo "  -> Meta > WhatsApp > Configuration > Verify and save"
  echo "==============================================================="
  printf '\n'
}

if [ -n "$NGROK" ] && [ -n "$NGROK_DOMAIN" ]; then
  # Static domain -> the public URL is known up front; no log scraping needed.
  "$NGROK" http "$PORT" --domain="$NGROK_DOMAIN" --log stdout >/tmp/ngrok.log 2>&1 &
  TUN_PID=$!
  sleep 1
  if ! kill -0 "$TUN_PID" 2>/dev/null; then
    echo "WARN: ngrok exited immediately. Check: ${NGROK} http ${PORT} --domain=${NGROK_DOMAIN}"
    echo "      (authtoken set? domain reserved to this account? log: /tmp/ngrok.log)"
  fi
  print_banner "https://${NGROK_DOMAIN}"
  echo "  (ngrok static domain - this URL is stable across restarts; inspector: http://localhost:4040)"
elif [ -x "$CF" ]; then
  echo "NOTE: NGROK_DOMAIN not set - using cloudflared quick-tunnel (URL ROTATES every run)."
  echo "      Set a reserved ngrok domain (see header of this script) to stop re-pasting into Meta."
  CF_LOG="$(mktemp)"
  "$CF" tunnel --url "http://localhost:${PORT}" >"$CF_LOG" 2>&1 &
  TUN_PID=$!
  URL=""
  for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" | head -1)"
    [ -n "$URL" ] && break
    sleep 0.5
  done
  if [ -n "$URL" ]; then
    print_banner "$URL"
  else
    echo "WARN: tunnel URL not detected yet. Tail the log: ${CF_LOG}"
  fi
else
  echo "No tunnel: set NGROK_DOMAIN (+ ngrok) for a stable URL, or put cloudflared at '${CF}'."
  echo "Starting server WITHOUT a tunnel."
fi

# Foreground server; on Ctrl+C the trap stops the tunnel too.
# RELOAD=0 for demos: --reload restarts the server on every code change, dropping in-flight work and sessions.
RELOAD_FLAG=$([ "${RELOAD:-1}" = "0" ] && echo "" || echo "--reload")
"$PY" -m uvicorn servicing_agent.platform.api.app:app $RELOAD_FLAG --port "$PORT"
