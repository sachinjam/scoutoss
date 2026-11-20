import { useMemo, useState } from "react";
import { CopilotPanel } from "./components/CopilotPanel";
import { CopilotApi } from "./lib/api";
import type { Session } from "./lib/auth";
import { config } from "./lib/config";

type Tab = "copilot" | "dashboards";

export function App({ session }: { session: Session }) {
  const [tab, setTab] = useState<Tab>("copilot");
  const api = useMemo(() => new CopilotApi(session.token), [session]);
  const canWrite = session.roles.includes("researcher") || session.roles.includes("admin");
  const xnatUrl = window.location.origin.replace("//app.", "//xnat.");

  return (
    <div className="shell">
      <header>
        <h1>
          <span className="logo" aria-hidden>◎</span> ScoutOSS
        </h1>
        <nav aria-label="Views">
          <button className={tab === "copilot" ? "active" : ""} onClick={() => setTab("copilot")}>Copilot</button>
          <button className={tab === "dashboards" ? "active" : ""} onClick={() => setTab("dashboards")}>Dashboards</button>
          <a href={xnatUrl} target="_blank" rel="noreferrer">XNAT / OHIF</a>
          <a href="/notebooks/" target="_blank" rel="noreferrer">Notebooks</a>
        </nav>
        <div className="user">
          <span>{session.username}</span>
          <span className="role">{canWrite ? "researcher" : "viewer"}</span>
          <button onClick={session.logout}>Sign out</button>
        </div>
      </header>
      <main>
        {tab === "copilot" ? (
          <CopilotPanel api={api} canWrite={canWrite} />
        ) : (
          <iframe className="dashboards" title="OpenSearch Dashboards" src={config().dashboardsPath} />
        )}
      </main>
    </div>
  );
}
