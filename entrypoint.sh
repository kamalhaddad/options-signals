#!/usr/bin/env bash
# Run the ThetaData Terminal + the Discord bot in one container.
# The Terminal binds 127.0.0.1:25510 (its default) so the co-located bot reaches it at
# localhost. If EITHER process exits, we exit non-zero so Docker (restart: unless-stopped)
# recreates the container — re-auth from the same droplet IP is fine (IP-lock is per-IP).
set -uo pipefail

: "${THETA_EMAIL:?THETA_EMAIL not set}"
: "${THETA_PASSWORD:?THETA_PASSWORD not set}"
JAR="${THETA_JAR:-/opt/ThetaTerminal.jar}"

if [[ ! -f "$JAR" ]]; then
  echo "FATAL: ThetaTerminal.jar not found at $JAR (mount it via docker-compose)" >&2
  exit 1
fi

echo "[entrypoint] starting ThetaData Terminal…"
java -jar "$JAR" "$THETA_EMAIL" "$THETA_PASSWORD" &
TERM_PID=$!

echo "[entrypoint] waiting for the Terminal to connect (127.0.0.1:25510)…"
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:25510/v2/system/mdds/status >/dev/null 2>&1; then
    echo "[entrypoint] Terminal up."
    break
  fi
  if ! kill -0 "$TERM_PID" 2>/dev/null; then
    echo "FATAL: Terminal exited during startup (check creds/subscription)" >&2
    exit 1
  fi
  sleep 2
done

echo "[entrypoint] starting bot…"
python -u bot.py &
BOT_PID=$!

# Exit as soon as either process dies; let Docker restart the whole container.
wait -n "$TERM_PID" "$BOT_PID"
echo "[entrypoint] a process exited — shutting down so Docker restarts us." >&2
kill "$TERM_PID" "$BOT_PID" 2>/dev/null || true
exit 1
