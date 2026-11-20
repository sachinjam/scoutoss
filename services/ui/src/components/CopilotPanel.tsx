import { useEffect, useRef, useState } from "react";
import { ApiError, type CopilotApi, type Pipeline, type QueryResult } from "../lib/api";
import { config } from "../lib/config";
import { VegaChart } from "./VegaChart";

interface Turn {
  id: number;
  question: string;
  status: string;
  streamed: string;
  query?: Record<string, unknown>;
  result?: QueryResult;
  error?: string;
  notice?: string;
}

const EXAMPLES = [
  "how many CT studies",
  "patients under 85 with lung cancer AND CT chest",
  "female patients with MRI brain, by age",
  "studies per year by modality",
];

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { error?: string; reason?: string; detail?: string } | string;
    if (typeof d === "string") return `${err.status}: ${d}`;
    return `${err.status}: ${d?.reason ?? d?.error ?? "error"}${d?.detail ? ` — ${d.detail}` : ""}`;
  }
  return err instanceof Error ? err.message : String(err);
}

export function CopilotPanel({ api, canWrite }: { api: CopilotApi; canWrite: boolean }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const sessionId = useRef(crypto.randomUUID().replace(/-/g, ""));
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!canWrite) return;
    api
      .pipelines(config().project)
      .then((r) => setPipelines(r.pipelines))
      .catch(() => setPipelines([]));
  }, [api, canWrite]);

  useEffect(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), [turns]);

  const update = (id: number, patch: Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...patch } : t)));

  async function ask(q: string) {
    const text = q.trim();
    if (!text || busy) return;
    const id = Date.now();
    setTurns((ts) => [...ts, { id, question: text, status: "planning", streamed: "" }]);
    setQuestion("");
    setBusy(true);
    let streamed = "";
    try {
      const result = await api.queryStream(text, sessionId.current, {
        onStatus: (stage) => update(id, { status: stage }),
        onQuery: (query) => update(id, { query }),
        onToken: (t) => {
          streamed += t;
          update(id, { streamed });
        },
      });
      update(id, { result, status: "done" });
    } catch (err) {
      update(id, { error: errorText(err), status: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function save(turn: Turn) {
    if (!turn.result?.cohort_id) return;
    const name = window.prompt("Cohort name", turn.question.slice(0, 80));
    if (!name) return;
    try {
      const saved = await api.saveCohort(turn.result.cohort_id, name);
      update(turn.id, { notice: `Saved cohort “${saved.name}”.` });
    } catch (err) {
      update(turn.id, { notice: `Save failed: ${errorText(err)}` });
    }
  }

  async function runPipeline(turn: Turn, pipeline: string) {
    if (!turn.result?.cohort_id || !pipeline) return;
    try {
      const out = await api.runPipeline(turn.result.cohort_id, pipeline);
      update(turn.id, {
        notice: `Launched ${pipeline} on ${out.launched.length} session(s)` +
          (out.errors.length ? `; ${out.errors.length} failed` : "") + ". Results appear in XNAT as assessors.",
      });
    } catch (err) {
      update(turn.id, { notice: `Launch failed: ${errorText(err)}` });
    }
  }

  async function exportCsv(turn: Turn) {
    if (!turn.result?.cohort_id) return;
    try {
      const blob = await api.exportCohort(turn.result.cohort_id);
      const url = URL.createObjectURL(blob);
      const a = Object.assign(document.createElement("a"), { href: url, download: `cohort-${turn.result.cohort_id}.csv` });
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      update(turn.id, { notice: `Export failed: ${errorText(err)}` });
    }
  }

  return (
    <section className="copilot">
      <div className="transcript" aria-live="polite">
        {turns.length === 0 && (
          <div className="empty">
            <h2>Ask about the de-identified data lake</h2>
            <p>Answers are computed from OpenSearch; the generated query is always shown for review.</p>
            <div className="examples">
              {EXAMPLES.map((e) => (
                <button key={e} className="chip" onClick={() => void ask(e)}>{e}</button>
              ))}
            </div>
          </div>
        )}
        {turns.map((t) => (
          <article key={t.id} className="turn">
            <p className="question">{t.question}</p>
            {t.status !== "done" && t.status !== "error" && <p className="status">{t.status}…</p>}
            {t.error && <p className="error" role="alert">{t.error}</p>}
            {(t.result?.answer_text ?? t.streamed) && (
              <p className="answer">{t.result?.answer_text ?? t.streamed}</p>
            )}
            {t.result?.warnings.map((w) => <p key={w} className="warning">{w}</p>)}
            {t.result && (
              <>
                <div className="metrics">
                  <span><strong>{t.result.cohort_count}</strong> patients</span>
                  <span><strong>{t.result.study_count}</strong> studies</span>
                  <span className="muted">planner: {t.result.provider}</span>
                </div>
                <div className="charts">
                  {t.result.charts.map((c) => <VegaChart key={c.id} chart={c} />)}
                </div>
                {t.result.studies.length > 0 && (
                  <table className="studies">
                    <thead>
                      <tr><th>Patient</th><th>Date</th><th>Modality</th><th>Body part</th><th>Age</th><th>Sex</th><th>Viewer</th></tr>
                    </thead>
                    <tbody>
                      {t.result.studies.slice(0, 10).map((s) => (
                        <tr key={s.study_uid}>
                          <td className="mono">{s.patient_pseudonym.slice(0, 10)}…</td>
                          <td>{s.study_date ?? "—"}</td>
                          <td>{s.modalities?.join(", ")}</td>
                          <td>{s.body_parts?.join(", ")}</td>
                          <td>{s.patient_age ?? "—"}</td>
                          <td>{s.patient_sex ?? "—"}</td>
                          <td>{s.viewer_url ? <a href={s.viewer_url} target="_blank" rel="noreferrer">Open in OHIF</a> : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <details className="review">
                  <summary>Review Code — generated query plan</summary>
                  <pre>{JSON.stringify(t.result.generated_query, null, 2)}</pre>
                  <p className="muted">Executed OpenSearch requests</p>
                  <pre>{JSON.stringify(t.result.executed_queries, null, 2)}</pre>
                </details>
                {canWrite && t.result.cohort_id && (
                  <div className="actions">
                    <button onClick={() => void save(t)}>Save Cohort</button>
                    <button onClick={() => void exportCsv(t)}>Export CSV</button>
                    <select
                      aria-label="Run Pipeline on Cohort"
                      defaultValue=""
                      onChange={(e) => {
                        void runPipeline(t, e.target.value);
                        e.target.value = "";
                      }}
                    >
                      <option value="" disabled>Run Pipeline on Cohort…</option>
                      {pipelines.map((p) => <option key={p.id} value={p.name}>{p.label}</option>)}
                    </select>
                  </div>
                )}
                {t.notice && <p className="notice">{t.notice}</p>}
              </>
            )}
            {!t.result && t.query && (
              <details className="review" open>
                <summary>Review Code — generated query plan</summary>
                <pre>{JSON.stringify(t.query, null, 2)}</pre>
              </details>
            )}
          </article>
        ))}
        <div ref={bottom} />
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. patients under 85 with lung cancer AND CT chest"
          aria-label="Question"
          maxLength={2000}
        />
        <button type="submit" disabled={busy || !question.trim()}>{busy ? "Working…" : "Ask"}</button>
      </form>
      <p className="disclaimer">Do not type names, MRNs or dates of birth — identifiers are stripped before any model sees the question.</p>
    </section>
  );
}
