#!/usr/bin/env bash
# Create .env from .env.example, replacing every CHANGE_ME with a fresh random secret.
# An existing .env keeps all its values (secrets must stay stable across restarts); keys that
# were added to .env.example since it was written are appended, so upgrades keep working.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
rand() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }
render() {  # one .env.example line -> one .env line
  local line="$1"
  case "$line" in
    *=CHANGE_ME_BASE64_32*) echo "${line%%=*}=$(openssl rand -base64 32)" ;;
    *=CHANGE_ME_COOKIE*)    echo "${line%%=*}=$(openssl rand -base64 32 | tr -- '+/' '-_')" ;;
    *=CHANGE_ME*)           echo "${line%%=*}=$(rand 32)" ;;
    *)                      echo "$line" ;;
  esac | sed -E 's/[[:space:]]+#.*$//'
}
if [[ -f .env ]]; then
  added=0
  while IFS= read -r line; do
    [[ "$line" =~ ^[A-Z][A-Z0-9_]*= ]] || continue
    key="${line%%=*}"
    grep -q "^${key}=" .env && continue
    render "$line" >> .env
    echo "added $key to .env"; added=$((added + 1))
  done < .env.example
  [[ $added -eq 0 ]] && echo ".env exists — keeping existing secrets"
  exit 0
fi
while IFS= read -r line; do render "$line"; done < .env.example > .env
echo "wrote .env with generated secrets (mode 600)"
