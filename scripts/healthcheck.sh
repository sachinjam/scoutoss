#!/usr/bin/env bash
# Report the health of every long-running service (docker healthchecks + HTTP probes).
set -uo pipefail
cd "$(dirname "$0")/.."
PORT="$(grep -E '^HTTPS_PORT=' .env 2>/dev/null | cut -d= -f2)"; PORT="${PORT:-24443}"
CA=infra/nginx/certs/ca.crt
fail=0
ONESHOT=" ${BOOTSTRAP_JOBS:-pipeline-images xnat-setup opensearch-setup mirth-setup} "
# All containers, not only running ones: a crash-looping service shows up as "restarting".
while IFS='|' read -r name state health code; do
  if [[ "$ONESHOT" == *" $name "* ]]; then
    if [[ "$state" == exited && "$code" == 0 ]]; then printf 'ok   %-24s %s\n' "$name" "completed"
    else printf 'FAIL %-24s %s\n' "$name" "$state (exit $code)"; fail=1; fi
  elif [[ "$state" == running && ( "$health" == healthy || -z "$health" ) ]]; then
    printf 'ok   %-24s %s\n' "$name" "${health:-running}"
  else
    printf 'FAIL %-24s %s\n' "$name" "$state${health:+ / $health}"; fail=1
  fi
done < <(docker compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}')
probe() {
  local name="$1" url="$2"
  local hostport="${url#https://}"; hostport="${hostport%%/*}"
  # macOS curl does not resolve *.localhost; pin it to loopback explicitly.
  code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$CA" --resolve "${hostport}:127.0.0.1" "$url")" || code=000
  if [[ "$code" =~ ^(200|204|301|302|401)$ ]]; then printf 'ok   %-24s %s\n' "$name" "HTTP $code"
  else printf 'FAIL %-24s %s\n' "$name" "HTTP $code"; fail=1; fi
}
probe "edge:app"   "https://app.localhost:${PORT}/"
probe "edge:xnat"  "https://xnat.localhost:${PORT}/"
probe "edge:notebooks"  "https://app.localhost:${PORT}/notebooks/hub/health"
probe "edge:grafana"    "https://app.localhost:${PORT}/grafana/api/health"
probe "edge:auth"  "https://auth.localhost:${PORT}/realms/scoutoss/.well-known/openid-configuration"
exit $fail
