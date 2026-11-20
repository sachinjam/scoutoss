import { describe, expect, it, vi } from "vitest";
import { ApiError, CopilotApi } from "./api";
import { config } from "./config";
import { SseParser } from "./sse";
import { newTraceparent, traceIdOf } from "./trace";

describe("traceparent", () => {
  it("is a valid W3C header", () => {
    const tp = newTraceparent();
    expect(tp).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
    expect(traceIdOf(tp)).toHaveLength(32);
    expect(newTraceparent()).not.toEqual(tp);
  });
});

describe("SseParser", () => {
  it("parses events split across chunks", () => {
    const p = new SseParser();
    expect(p.push('event: status\ndata: {"stage":"pl')).toEqual([]);
    const evs = p.push('anning"}\n\n: comment\nevent: token\ndata: {"text":"a"}\n\ndata: x\n\n');
    expect(evs).toEqual([
      { event: "status", data: '{"stage":"planning"}' },
      { event: "token", data: '{"text":"a"}' },
      { event: "message", data: "x" },
    ]);
  });
});

function sseResponse(body: string): Response {
  // Two chunks, to exercise events split across reads.
  const bytes = new TextEncoder().encode(body);
  const mid = Math.floor(bytes.length / 2);
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      if (bytes.length) {
        controller.enqueue(bytes.slice(0, mid));
        controller.enqueue(bytes.slice(mid));
      }
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("CopilotApi", () => {
  const token = async () => "tok";

  it("sends bearer token and traceparent", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ answer_text: "ok" }), { status: 200 }));
    const api = new CopilotApi(token, "/api", fetchMock as unknown as typeof fetch);
    await api.query("how many CT studies", "s1");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(url).toBe("/api/query");
    expect(headers.Authorization).toBe("Bearer tok");
    expect(headers.traceparent).toMatch(/^00-/);
    expect(JSON.parse(init.body as string)).toEqual({ question: "how many CT studies", session_id: "s1" });
  });

  it("raises ApiError with detail", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ detail: { reason: "unknown_index" } }), { status: 422 }));
    const api = new CopilotApi(token, "/api", fetchMock as unknown as typeof fetch);
    await expect(api.query("x y", "s")).rejects.toMatchObject({ status: 422, detail: { reason: "unknown_index" } });
    const plain = new CopilotApi(token, "/api", (async () => new Response("nope", { status: 500, statusText: "boom" })) as unknown as typeof fetch);
    await expect(plain.cohorts()).rejects.toBeInstanceOf(ApiError);
  });

  it("streams status, query, tokens and the result", async () => {
    const body =
      'event: status\ndata: {"stage":"planning"}\n\n' +
      'event: query\ndata: {"generated_query":{"steps":[]}}\n\n' +
      'event: token\ndata: {"text":"Found "}\n\n' +
      'event: result\ndata: {"answer_text":"Found 1","cohort_count":1}\n\n';
    const api = new CopilotApi(token, "/api", (async () => sseResponse(body)) as unknown as typeof fetch);
    const seen: string[] = [];
    const result = await api.queryStream("q", "s", {
      onStatus: (s) => seen.push(`status:${s}`),
      onQuery: () => seen.push("query"),
      onToken: (t) => seen.push(`token:${t}`),
    });
    expect(result.cohort_count).toBe(1);
    expect(seen).toEqual(["status:planning", "query", "token:Found "]);
  });

  it("surfaces stream errors", async () => {
    const api = new CopilotApi(token, "/api",
      (async () => sseResponse('event: error\ndata: {"status":422,"detail":"bad"}\n\n')) as unknown as typeof fetch);
    await expect(api.queryStream("q", "s", {})).rejects.toMatchObject({ status: 422 });
    const empty = new CopilotApi(token, "/api", (async () => sseResponse("")) as unknown as typeof fetch);
    await expect(empty.queryStream("q", "s", {})).rejects.toMatchObject({ status: 502 });
  });

  it("calls cohort and pipeline endpoints", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    const api = new CopilotApi(token, "/api", fetchMock as unknown as typeof fetch);
    await api.saveCohort("c 1", "name");
    await api.runPipeline("c1", "scoutoss-qc-session");
    await api.pipelines("SCOUT_DEMO");
    const urls = fetchMock.mock.calls.map((c) => (c as unknown as [string])[0]);
    expect(urls).toEqual(["/api/cohorts/c%201/save", "/api/cohorts/c1/pipeline", "/api/pipelines?project=SCOUT_DEMO"]);
  });
});

describe("config", () => {
  it("merges window overrides over defaults", () => {
    window.SCOUT_CONFIG = { project: "OTHER" };
    expect(config().project).toBe("OTHER");
    expect(config().realm).toBe("scoutoss");
  });
});
