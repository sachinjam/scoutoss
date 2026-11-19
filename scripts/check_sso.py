#!/usr/bin/env python3
"""Scripted browser login: app.localhost/dashboards -> oauth2-proxy -> Keycloak -> back.

Proves the Keycloak OIDC flow that guards OpenSearch Dashboards (and Jaeger/Prometheus)
works end to end. Exits non-zero on failure.
"""

from __future__ import annotations

import re
import sys
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e import ENV, PORT, edge


def main() -> int:
    client = edge()
    client.follow_redirects = True
    page = client.get(f"https://app.localhost:{PORT}/dashboards/app/home")
    if "auth.localhost" not in str(page.url):
        print(f"expected Keycloak login, got {page.url}")
        return 1
    action = re.search(r'id="kc-form-login"[^>]*action="([^"]+)"', page.text) or re.search(
        r'action="([^"]+login-actions/authenticate[^"]+)"', page.text
    )
    if not action:
        print("login form not found")
        return 1
    resp = client.post(
        unescape(action.group(1)),
        data={"username": "viewer", "password": ENV["DEMO_USER_PASSWORD"]},
    )
    ok = resp.status_code == 200 and "/dashboards/" in str(resp.url) and "osd" in resp.text.lower()
    print({"final_url": str(resp.url).split("?")[0], "status": resp.status_code, "ok": ok})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
