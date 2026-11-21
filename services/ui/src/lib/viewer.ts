/**
 * Viewer links point at XNAT, which has its own session. XNAT signs a visitor in through
 * Keycloak but then shows its home page instead of the page they asked for, so links go to a
 * small landing page on XNAT's origin (`/scoutoss/open`, see infra/nginx/html/xnat-open.html)
 * that signs in if needed and continues to the viewer. The destination travels in the URL
 * fragment, which is never sent to a server.
 */
export function viewerLink(viewerUrl: string, useLanding = true): string {
  let u: URL;
  try {
    u = new URL(viewerUrl);
  } catch {
    return viewerUrl; // relative path: leave it alone
  }
  // the value comes from the search index; never turn e.g. a javascript: URL into a link
  if (u.protocol !== "https:" && u.protocol !== "http:") return "about:blank";
  return useLanding ? `${u.origin}/scoutoss/open#${u.pathname}${u.search}` : viewerUrl;
}
