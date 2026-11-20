// Overwritten at container start from environment (see docker-entrypoint.d/40-config.sh).
window.SCOUT_CONFIG = {
  keycloakUrl: "https://auth.localhost:24443",
  realm: "scoutoss",
  clientId: "scoutoss-ui",
  apiBase: "/api/copilot",
  dashboardsPath: "/dashboards/app/dashboards#/view/scoutoss-overview?embed=true&_g=(time:(from:now-15y,to:now))",
  project: "SCOUT_DEMO"
};
