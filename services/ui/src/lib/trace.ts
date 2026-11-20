/** W3C Trace Context: every API call starts a trace the copilot and indexer join. */
function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function newTraceparent(): string {
  let traceId = randomHex(16);
  while (/^0+$/.test(traceId)) traceId = randomHex(16);
  return `00-${traceId}-${randomHex(8)}-01`;
}

export function traceIdOf(traceparent: string): string {
  return traceparent.split("-")[1] ?? "";
}
