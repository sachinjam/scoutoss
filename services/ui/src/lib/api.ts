import { config } from "./config";
import { SseParser } from "./sse";
import { newTraceparent } from "./trace";

export interface Chart {
  id: string;
  type: string;
  title: string;
  spec: Record<string, unknown>;
}

export interface Study {
  study_uid: string;
  patient_pseudonym: string;
  study_date?: string;
  modalities?: string[];
  body_parts?: string[];
  patient_age?: number;
  patient_sex?: string;
  study_description?: string;
  viewer_url?: string;
  xnat_experiment_id?: string;
}

export interface QueryResult {
  answer_text: string;
  cohort_count: number;
  study_count: number;
  charts: Chart[];
  generated_query: Record<string, unknown>;
  executed_queries: Record<string, unknown>[];
  cohort_id: string | null;
  studies: Study[];
  warnings: string[];
  provider: string;
  session_id: string;
}

export interface Pipeline {
  id: number;
  name: string;
  label: string;
  description: string;
}

export interface StreamHandlers {
  onStatus?: (stage: string) => void;
  onQuery?: (query: Record<string, unknown>) => void;
  onToken?: (text: string) => void;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : `request failed (${status})`);
  }
}

type TokenProvider = () => Promise<string>;

export class CopilotApi {
  constructor(
    private token: TokenProvider,
    private base: string = config().apiBase,
    private fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

  private async headers(extra: Record<string, string> = {}): Promise<Record<string, string>> {
    return {
      Authorization: `Bearer ${await this.token()}`,
      traceparent: newTraceparent(),
      ...extra,
    };
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = await this.headers({ "Content-Type": "application/json" });
    const resp = await this.fetchImpl(`${this.base}${path}`, { ...init, headers });
    if (!resp.ok) {
      let detail: unknown = resp.statusText;
      try {
        detail = (await resp.json()).detail;
      } catch {
        /* non-JSON error */
      }
      throw new ApiError(resp.status, detail);
    }
    return (await resp.json()) as T;
  }

  query(question: string, sessionId: string): Promise<QueryResult> {
    return this.request("/query", {
      method: "POST",
      body: JSON.stringify({ question, session_id: sessionId }),
    });
  }

  async queryStream(question: string, sessionId: string, handlers: StreamHandlers): Promise<QueryResult> {
    const headers = await this.headers({ "Content-Type": "application/json", Accept: "text/event-stream" });
    const resp = await this.fetchImpl(`${this.base}/query/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify({ question, session_id: sessionId }),
    });
    if (!resp.ok || !resp.body) throw new ApiError(resp.status, resp.statusText);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const ev of parser.push(decoder.decode(value, { stream: true }))) {
        const data = JSON.parse(ev.data);
        if (ev.event === "status") handlers.onStatus?.(data.stage);
        else if (ev.event === "query") handlers.onQuery?.(data.generated_query);
        else if (ev.event === "token") handlers.onToken?.(data.text);
        else if (ev.event === "error") throw new ApiError(data.status, data.detail);
        else if (ev.event === "result") return data as QueryResult;
      }
    }
    throw new ApiError(502, "stream ended without a result");
  }

  saveCohort(cohortId: string, name: string): Promise<{ id: string; name: string; saved: boolean }> {
    return this.request(`/cohorts/${encodeURIComponent(cohortId)}/save`, {
      method: "POST",
      body: JSON.stringify({ name }),
    });
  }

  pipelines(project: string): Promise<{ pipelines: Pipeline[] }> {
    return this.request(`/pipelines?project=${encodeURIComponent(project)}`);
  }

  runPipeline(cohortId: string, pipeline: string): Promise<{ launched: unknown[]; errors: unknown[] }> {
    return this.request(`/cohorts/${encodeURIComponent(cohortId)}/pipeline`, {
      method: "POST",
      body: JSON.stringify({ pipeline }),
    });
  }

  cohorts(): Promise<{ cohorts: { id: string; name: string; member_count: number; saved: boolean; created_at: string }[] }> {
    return this.request("/cohorts");
  }

  async exportCohort(cohortId: string): Promise<Blob> {
    const resp = await this.fetchImpl(`${this.base}/cohorts/${encodeURIComponent(cohortId)}/export`, {
      headers: await this.headers(),
    });
    if (!resp.ok) throw new ApiError(resp.status, resp.statusText);
    return resp.blob();
  }
}
