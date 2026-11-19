#!/usr/bin/env bash
# Download (or copy from $XNAT_PLUGIN_CACHE) the pinned XNAT plugin jars and verify them.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && . ./.env && set +a
DEST=xnat/plugins
mkdir -p "$DEST"
# name|url|sha256
PLUGINS=(
  "container-service-3.9.0-fat.jar|https://github.com/NrgXnat/container-service/releases/download/3.9.0/container-service-3.9.0-fat.jar|809042a18dfea8383b7bd5f53b367eb8ea05c2a8c3f35b519841aa17b93cd207"
  "ohif-viewer-3.7.1-fat.jar|${OHIF_PLUGIN_URL:-https://bitbucket.org/icrimaginginformatics/ohif-viewer-xnat-plugin/downloads/ohif-viewer-3.7.1-fat.jar}|1b8b79554b96b92b995cdb5f8dffdf959c11a51f892fd391421584fe86f1986c"
)
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
for entry in "${PLUGINS[@]}"; do
  IFS='|' read -r name url want <<<"$entry"
  target="$DEST/$name"
  if [[ -f "$target" && "$(sha "$target")" == "$want" ]]; then echo "ok   $name"; continue; fi
  if [[ -n "${XNAT_PLUGIN_CACHE:-}" && -f "${XNAT_PLUGIN_CACHE}/$name" ]]; then
    cp "${XNAT_PLUGIN_CACHE}/$name" "$target.tmp"
  elif ! curl -fsSL --retry 3 "$url" -o "$target.tmp"; then
    echo "FAIL $name: download failed. Put the jar in \$XNAT_PLUGIN_CACHE or set OHIF_PLUGIN_URL." >&2
    rm -f "$target.tmp"; exit 1
  fi
  got="$(sha "$target.tmp")"
  if [[ "$got" != "$want" ]]; then
    echo "FAIL $name: sha256 $got != pinned $want" >&2; rm -f "$target.tmp"; exit 1
  fi
  mv "$target.tmp" "$target"; echo "got  $name"
done
