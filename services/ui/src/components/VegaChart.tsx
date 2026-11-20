import { useEffect, useRef } from "react";
import type { Chart } from "../lib/api";

/** Renders a server-generated Vega-Lite spec (aggregate counts only). */
export function VegaChart({ chart }: { chart: Chart }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let finalize: (() => void) | undefined;
    let cancelled = false;
    void import("vega-embed").then(({ default: embed }) => {
      if (cancelled || !ref.current) return;
      void embed(ref.current, chart.spec as never, {
        actions: { export: true, source: false, compiled: false, editor: false },
        renderer: "svg",
        config: { background: "transparent", view: { stroke: null } },
      }).then((result) => {
        finalize = () => result.finalize();
      });
    });
    return () => {
      cancelled = true;
      finalize?.();
    };
  }, [chart]);
  return (
    <figure className="chart" aria-label={chart.title}>
      <div ref={ref} />
    </figure>
  );
}
