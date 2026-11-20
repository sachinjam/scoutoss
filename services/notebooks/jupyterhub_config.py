"""JupyterHub for ScoutOSS: Keycloak login, notebooks run as the unprivileged hub user.

Every user's server gets SCOUTOSS_* environment variables pointing at the copilot API,
XNAT and the (read-only) OpenSearch endpoint. Production (Helm) swaps the simple local
spawner for KubeSpawner so each user gets an isolated pod.
"""

import os

c = get_config()  # type: ignore[name-defined]  # noqa: F821

public = os.environ.get("PUBLIC_APP_URL", "https://app.localhost:24443")
issuer = os.environ.get("KEYCLOAK_PUBLIC_ISSUER", "https://auth.localhost:24443/realms/scoutoss")
internal = os.environ.get("KEYCLOAK_INTERNAL_ISSUER", "http://keycloak:8080/realms/scoutoss")

c.JupyterHub.base_url = "/notebooks/"
c.JupyterHub.bind_url = "http://0.0.0.0:8000/notebooks/"
c.JupyterHub.db_url = "sqlite:////srv/jupyterhub/jupyterhub.sqlite"
c.JupyterHub.cookie_secret_file = "/srv/jupyterhub/jupyterhub_cookie_secret"  # noqa: S105 - a path

c.JupyterHub.authenticator_class = "generic-oauth"
c.GenericOAuthenticator.client_id = os.environ.get("OAUTH_CLIENT_ID", "scoutoss-notebooks")
c.GenericOAuthenticator.client_secret = os.environ.get("OAUTH_CLIENT_SECRET", "")
c.GenericOAuthenticator.oauth_callback_url = f"{public}/notebooks/hub/oauth_callback"
c.GenericOAuthenticator.authorize_url = f"{issuer}/protocol/openid-connect/auth"
c.GenericOAuthenticator.token_url = f"{internal}/protocol/openid-connect/token"
c.GenericOAuthenticator.userdata_url = f"{internal}/protocol/openid-connect/userinfo"
c.GenericOAuthenticator.scope = ["openid", "profile", "email"]
c.GenericOAuthenticator.username_claim = "preferred_username"
c.GenericOAuthenticator.claim_groups_key = "groups"
c.GenericOAuthenticator.allow_all = True
c.GenericOAuthenticator.enable_auth_state = False

c.JupyterHub.spawner_class = "simple"
c.Spawner.notebook_dir = "/home/jovyan"
c.Spawner.default_url = "/lab"
c.Spawner.environment = {
    "SCOUTOSS_API": os.environ.get("SCOUTOSS_API", "http://copilot:8000"),
    "SCOUTOSS_XNAT": os.environ.get("SCOUTOSS_XNAT", "http://xnat-web:8080"),
    "SCOUTOSS_OPENSEARCH": os.environ.get("SCOUTOSS_OPENSEARCH", "http://opensearch:9200"),
}
c.Spawner.args = ["--ServerApp.terminado_settings={'shell_command': ['/bin/bash']}"]
