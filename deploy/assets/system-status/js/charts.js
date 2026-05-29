// charts.js — tiny inline-SVG charts: sparkline + per-core CPU bars.
//
// Tone is the single colour knob. A chart sets `--tone` on its root and
// the CSS (app.css --status-*) reads it.

import { svg, h } from "./dom.js";
import { toneForPercent } from "./format.js";

function toneVar(tone) {
  return `var(--status-${tone})`;
}

// Sparkline over `values`. `opts.min`/`opts.max` pin the vertical scale
// (the dashboard fixes each metric's range — memory to total RAM, load to
// core count, etc.); omit them to auto-scale. `opts.tone` colours the
// stroke/fill; `opts.fill` draws the area beneath the line.
export function sparkline(values, opts = {}) {
  const W = 100, H = 32;
  const root = svg("svg.sparkline", {
    viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: "none",
    "aria-hidden": "true",
  });
  root.style.setProperty("--tone", toneVar(opts.tone || "ok"));
  if (!values || !values.length) return root;

  const min = opts.min != null ? opts.min : Math.min(...values);
  let max = opts.max != null ? opts.max : Math.max(...values);
  if (max - min < 1e-6) max = min + 1;
  const n = values.length;
  const x = (i) => (i / Math.max(1, n - 1)) * W;
  const y = (v) => H - ((v - min) / (max - min)) * H;

  let d = "";
  for (let i = 0; i < n; i++) {
    d += `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(values[i]).toFixed(2)} `;
  }
  if (opts.fill) {
    root.appendChild(svg("path.sparkline__area", { d: `${d}L${W},${H} L0,${H} Z` }));
  }
  root.appendChild(svg("path.sparkline__line", { d: d.trim() }));
  return root;
}

// Per-core CPU bars — one column per logical CPU, coloured by load.
// Thresholds (warn 75%, danger 90%) match the previous page.
export function cpuBars(values) {
  return h("div.cpu-bars", null,
    (values || []).map((v) => {
      const pct = Math.min(100, Math.max(0, v || 0));
      const tone = toneForPercent(pct, 75, 90);
      const fill = h("div.cpu-bars__fill", {
        style: { height: pct.toFixed(1) + "%", "--tone": toneVar(tone) },
      });
      return h("div.cpu-bars__col", null,
        h("div.cpu-bars__track", null, fill),
        h("span.cpu-bars__label", null, Math.round(pct) + "%"),
      );
    }),
  );
}
