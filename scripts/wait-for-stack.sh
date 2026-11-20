#!/usr/bin/env bash
# Wait for the one-shot bootstrap jobs to finish successfully, then run the health check.
set -euo pipefail
cd "$(dirname "$0")/.."
JOBS=(${BOOTSTRAP_JOBS:-pipeline-images xnat-setup opensearch-setup mirth-setup})
deadline=$(( $(date +%s) + ${WAIT_TIMEOUT:-1800} ))
for job in "${JOBS[@]}"; do
  cid="$(docker compose ps -a -q "$job" 2>/dev/null || true)"
  [[ -z "$cid" ]] && { echo "skip $job (not defined)"; continue; }
  while :; do
    state="$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$cid")"
    case "$state" in
      "exited 0") echo "ok   $job"; break ;;
      exited*)    echo "FAIL $job ($state)"; docker compose logs --tail=40 "$job"; exit 1 ;;
    esac
    (( $(date +%s) > deadline )) && { echo "TIMEOUT waiting for $job"; exit 1; }
    sleep 5
  done
done
# then every long-running service with a healthcheck must report healthy
while :; do
  # long-running services: anything restarting/exited, or with a healthcheck that is not healthy yet
  unhealthy="$(docker compose ps -a --format '{{.Service}} {{.State}} {{.Health}}' \
    | awk -v jobs=" ${JOBS[*]} " 'index(jobs, " " $1 " ") == 0 && ($2 != "running" || ($3 != "" && $3 != "healthy")) {print $1 "(" $2 ")"}')"
  [[ -z "$unhealthy" ]] && break
  (( $(date +%s) > deadline )) && { echo "TIMEOUT: not healthy: $unhealthy"; exit 1; }
  sleep 5
done
scripts/healthcheck.sh
