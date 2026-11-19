#!/usr/bin/env bash
# Start the FastAPI server AND a cloudflared tunnel together.
# The tunnel's public URL (which rotates on the free quick-tunnel) is printed so you can
# paste it into Meta -> WhatsApp -> Configuration. The tunnel is stopped when the server exits.
set -uo pipefail

PY="${PY:-.venv/bin/python}"
PORT="${PORT:-8000}"
CF="${CF:-./cloudflared}"
VERIFY_TOKEN="${SA_WA_VERIFY_TOKEN:-cn-verify-dev}"
CF_PID=""

cleanup() { [ -n "$CF_PID" ] && kill "$CF_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [ -x "$CF" ]; then
  CF_LOG="$(mktemp)"
  "$CF" tunnel --url "http://localhost:${PORT}" >"$CF_LOG" 2>&1 &
  CF_PID=$!
  URL=""
  for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" | head -1)"
    [ -n "$URL" ] && break
    sleep 0.5
  done
  if [ -n "$URL" ]; then
    printf '\n'
    echo "========================  META WEBHOOK  ========================"
    echo "  Callback URL (copy this line):"
    echo
    echo "    ${URL}/webhooks/whatsapp"
    echo
    echo "  Verify token:  ${VERIFY_TOKEN}"
    echo "  -> Meta > WhatsApp > Configuration > Verify and save"
    echo "==============================================================="
    printf '\n'
  else
    echo "WARN: tunnel URL not detected yet. Tail the log: ${CF_LOG}"
  fi
else
  echo "cloudflared not found at '${CF}' - starting server WITHOUT a tunnel."
  echo "Download it, or set CF=/path/to/cloudflared, to auto-tunnel."
fi

# Foreground server; on Ctrl+C the trap stops the tunnel too.
"$PY" -m uvicorn servicing_agent.platform.api.app:app --reload --port "$PORT"
