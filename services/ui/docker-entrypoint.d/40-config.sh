#!/bin/sh
# Render runtime configuration (no rebuild needed per environment).
set -eu
cat > /tmp/config.js <<JS
window.SCOUT_CONFIG = {
  keycloakUrl: "${KEYCLOAK_PUBLIC_URL:-https://auth.localhost:24443}",
  realm: "${KEYCLOAK_REALM:-scoutoss}",
  clientId: "${KEYCLOAK_CLIENT_ID:-scoutoss-ui}",
  apiBase: "${API_BASE:-/api/copilot}",
  dashboardsPath: "${DASHBOARDS_PATH:-/dashboards/app/dashboards#/view/scoutoss-overview?embed=true&_g=(time:(from:now-15y,to:now))}",
  project: "${DEFAULT_PROJECT:-SCOUT_DEMO}"
};
JS
