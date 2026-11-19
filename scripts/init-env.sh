#!/usr/bin/env bash
# Create .env from .env.example, replacing every CHANGE_ME with a fresh random secret.
# Existing .env files are left alone (secrets must stay stable across restarts).
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  echo ".env exists — keeping existing secrets"
  exit 0
fi
umask 077
rand() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }
while IFS= read -r line; do
  case "$line" in
    *=CHANGE_ME_BASE64_32*) echo "${line%%=*}=$(openssl rand -base64 32)" ;;
    *=CHANGE_ME_COOKIE*)    echo "${line%%=*}=$(openssl rand -base64 32 | tr -- '+/' '-_')" ;;
    *=CHANGE_ME*)           echo "${line%%=*}=$(rand 32)" ;;
    *)                      echo "$line" ;;
  esac
done < .env.example | sed -E 's/[[:space:]]+#.*$//' > .env
echo "wrote .env with generated secrets (mode 600)"
