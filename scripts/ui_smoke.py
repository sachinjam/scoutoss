#!/usr/bin/env python3
"""Browser smoke test of the ScoutOSS UI against a running, seeded stack (called by e2e.sh).

Unit tests run the UI in jsdom; this drives a real Chromium through the edge proxy, so it
catches what only shows up in the built bundle in a real browser (a render crash that blanks
the page, a 502 in the SSO redirect chain, a hub that never starts).

Steps (each prints one JSON line; the first failure exits non-zero):
  login       app.localhost -> Keycloak login form -> back in the SPA as `demo`
  ask         the three example questions each render an answer, counts and the query plan;
              charts are drawn; the cross-modal question finds >= 1 patient
  save        "Save Cohort" stores the cohort and shows the confirmation
  dashboards  the Dashboards tab loads OpenSearch Dashboards through oauth2-proxy (SSO)
  notebooks   JupyterHub login via Keycloak spawns JupyterLab
  page        no uncaught page errors and no 5xx responses from the edge, at any point

Screenshots land in artifacts/ui-smoke/ (CI uploads them when the job fails).
Browser: Playwright's Chromium if installed (`uv run --group e2e playwright install chromium`),
otherwise a locally installed Google Chrome. The dev CA is self-signed, so certificate errors
are ignored for this run only.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e import PORT, ROOT

APP = f"https://app.localhost:{PORT}"
USER = "demo"  # dev-only login with the fixed password "demo" (infra/keycloak/realm.json)
OUT = ROOT / "artifacts" / "ui-smoke"
TIMEOUT_MS = 60_000

QUESTIONS = [
    # (question, assertion on the rendered turn)
    ("how many CT studies", "charts"),
    ("patients under 85 with lung cancer AND CT chest", "patients"),
    ("of those, how many are female?", "answer"),
]

page_errors: list[str] = []
server_errors: list[str] = []
current: dict[str, Page] = {}


def step(name: str, ok: bool, **fields: Any) -> None:
    print(json.dumps({"step": name, "ok": ok, **fields}), flush=True)
    if not ok:
        page = current.get("page")
        if page is not None:
            with contextlib.suppress(PlaywrightError):
                page.screenshot(path=OUT / f"FAILED-{name}.png", full_page=True)
        sys.exit(1)


def assert_clean(name: str) -> None:
    """Fail the step if the page threw or the edge answered 5xx since the last check."""
    if page_errors or server_errors:
        step(name, False, page_errors=page_errors[:5], server_errors=server_errors[:5])


def watch(page: Page) -> Page:
    page.set_default_timeout(TIMEOUT_MS)
    page.on("pageerror", lambda exc: page_errors.append(str(exc)[:300]))
    page.on(
        "response",
        lambda r: (
            r.status >= 500
            and "localhost:" in r.url
            and server_errors.append(f"{r.status} {r.url.split('?')[0]}")
        ),
    )
    return page


def launch(pw: Playwright) -> Browser:
    args = ["--ignore-certificate-errors"]
    try:
        return pw.chromium.launch(headless=True, args=args)
    except PlaywrightError:
        pass
    try:
        return pw.chromium.launch(channel="chrome", headless=True, args=args)
    except PlaywrightError:
        print(
            "No browser for the UI smoke test. Install one with:\n"
            "  uv run --group e2e playwright install chromium",
            file=sys.stderr,
        )
        raise


def keycloak_login(page: Page) -> None:
    page.fill("#username", USER)
    page.fill("#password", "demo")
    page.click("#kc-login")


def ask(page: Page, index: int, question: str, expect: str) -> None:
    page.fill("[aria-label=Question]", question)
    page.click("form.composer button[type=submit]")
    turn = page.locator("article.turn").nth(index)
    done = turn.locator(".metrics, .error")
    try:
        done.first.wait_for()
    except PlaywrightError:
        assert_clean("ask")
        step("ask", False, question=question, reason="no answer rendered")
    assert_clean("ask")
    error = turn.locator(".error")
    if error.count():
        step("ask", False, question=question, error=error.inner_text())
    answer = turn.locator(".answer").inner_text().strip()
    patients = int(turn.locator(".metrics strong").first.inner_text())
    has_plan = turn.locator("details.review summary").count() == 1
    charts = 0
    if expect == "charts":
        turn.locator("figure.chart svg").first.wait_for()
        charts = turn.locator("figure.chart svg").count()
    ok = bool(answer) and has_plan
    ok = ok and (expect != "charts" or charts > 0) and (expect != "patients" or patients >= 1)
    page.screenshot(path=OUT / f"ask-{index + 1}.png", full_page=True)
    step("ask", ok, question=question, patients=patients, charts=charts, answer=answer[:120])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = launch(pw)
        context = browser.new_context(
            ignore_https_errors=True, viewport={"width": 1400, "height": 1000}
        )
        page = watch(context.new_page())
        current["page"] = page

        page.goto(f"{APP}/")
        page.wait_for_selector("#username, [aria-label=Question]")
        at_keycloak = "auth.localhost" in page.url
        if at_keycloak:
            keycloak_login(page)
        page.wait_for_selector("[aria-label=Question]")
        signed_in = page.get_by_text(USER, exact=True).first.is_visible()
        page.screenshot(path=OUT / "home.png", full_page=True)
        assert_clean("login")
        step("login", at_keycloak and signed_in, keycloak=at_keycloak)

        for index, (question, expect) in enumerate(QUESTIONS):
            ask(page, index, question, expect)

        page.once("dialog", lambda d: d.accept("ui-smoke cohort"))
        cross_modal = page.locator("article.turn").nth(1)
        cross_modal.get_by_role("button", name="Save Cohort").click()
        notice = cross_modal.locator(".notice")
        notice.wait_for()
        assert_clean("save")
        step("save", "Saved cohort" in notice.inner_text(), notice=notice.inner_text())

        page.get_by_role("button", name="Dashboards").click()
        frame = page.frame_locator("iframe.dashboards")
        try:
            frame.get_by_text("Studies by modality").first.wait_for(timeout=120_000)
            rendered = True
        except PlaywrightError:
            rendered = False
        page.screenshot(path=OUT / "dashboards.png", full_page=True)
        assert_clean("dashboards")
        step("dashboards", rendered)

        hub = watch(context.new_page())
        current["page"] = hub
        hub.goto(f"{APP}/notebooks/hub/oauth_login")
        hub.wait_for_load_state()
        if hub.locator("#username").count():
            keycloak_login(hub)
        try:
            hub.wait_for_url("**/notebooks/user/**/lab**", timeout=180_000)
            hub.locator(".jp-Launcher").first.wait_for(timeout=120_000)
            lab = True
        except PlaywrightError:
            lab = False
        hub.screenshot(path=OUT / "notebooks.png")
        assert_clean("notebooks")
        step("notebooks", lab, url=hub.url.split("?")[0])

        assert_clean("page")
        step("page", True, page_errors=0, server_errors=0)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
