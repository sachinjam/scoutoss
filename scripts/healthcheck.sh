#!/usr/bin/env bash
# Report the health of every long-running service (docker healthchecks + HTTP probes).
set -uo pipefail
cd "$(dirname "$0")/.."
PORT="$(grep -E '^HTTPS_PORT=' .env 2>/dev/null | cut -d= -f2)"; PORT="${PORT:-24443}"
CA=infra/nginx/certs/ca.crt
fail=0
while read -r name health; do
  case "$health" in
    healthy|"") printf 'ok   %-24s %s\n' "$name" "${health:-running}" ;;
    *) printf 'FAIL %-24s %s\n' "$name" "$health"; fail=1 ;;
  esac
done < <(docker compose ps --format '{{.Service}} {{.Health}}' --status running)
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
probe "edge:auth"  "https://auth.localhost:${PORT}/realms/scoutoss/.well-known/openid-configuration"
exit $fail
