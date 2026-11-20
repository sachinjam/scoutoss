import Keycloak from "keycloak-js";
import { config } from "./config";

export interface Session {
  username: string;
  roles: string[];
  token: () => Promise<string>;
  logout: () => void;
}

/** Keycloak OIDC (authorization code + PKCE). Tokens stay in memory, never localStorage. */
export async function login(): Promise<Session> {
  const cfg = config();
  const kc = new Keycloak({ url: cfg.keycloakUrl, realm: cfg.realm, clientId: cfg.clientId });
  await kc.init({ onLoad: "login-required", pkceMethod: "S256", checkLoginIframe: false });
  return {
    username: kc.tokenParsed?.preferred_username ?? "unknown",
    roles: kc.tokenParsed?.realm_access?.roles ?? [],
    token: async () => {
      await kc.updateToken(30);
      return kc.token ?? "";
    },
    logout: () => void kc.logout({ redirectUri: window.location.origin }),
  };
}
