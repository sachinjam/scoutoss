export interface ScoutConfig {
  keycloakUrl: string;
  realm: string;
  clientId: string;
  apiBase: string;
  dashboardsPath: string;
  project: string;
  /** Send viewer links through XNAT's /scoutoss/open landing page (Keycloak sign-on). */
  viewerLanding: boolean;
}

declare global {
  interface Window {
    SCOUT_CONFIG?: Partial<ScoutConfig>;
  }
}

const DEFAULTS: ScoutConfig = {
  keycloakUrl: "https://auth.localhost:24443",
  realm: "scoutoss",
  clientId: "scoutoss-ui",
  apiBase: "/api/copilot",
  dashboardsPath:
    "/dashboards/app/dashboards#/view/scoutoss-overview?embed=true&_g=(time:(from:now-15y,to:now))",
  project: "SCOUT_DEMO",
  viewerLanding: true,
};

export function config(): ScoutConfig {
  return { ...DEFAULTS, ...(typeof window !== "undefined" ? window.SCOUT_CONFIG : {}) };
}
