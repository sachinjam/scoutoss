/** Minimal Server-Sent Events parser for fetch() response bodies. */
export interface SseEvent {
  event: string;
  data: string;
}

export class SseParser {
  private buffer = "";

  /** Feed a chunk of text; returns the complete events it closed. */
  push(chunk: string): SseEvent[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const events: SseEvent[] = [];
    let index: number;
    while ((index = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith(":")) continue;
        const [field, ...rest] = line.split(":");
        const value = rest.join(":").replace(/^ /, "");
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
      }
      if (data.length) events.push({ event, data: data.join("\n") });
    }
    return events;
  }
}
