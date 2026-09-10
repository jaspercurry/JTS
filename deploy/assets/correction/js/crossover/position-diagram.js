// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// A simple per-position picture for the crossover walk (#3629, #1941 R11):
// the speaker at the mark, the mic's target spot, and an arrow from the mark
// to it. Pure geometry -- `degrees`/`verticalDeg` are the SAME signed fields
// the walk prompt's own wire payload already carries
// (jasper.active_speaker.crossover_v2.position_gate's `_pending`), so this
// needs no new backend data.
//
// Sign convention matches the prompt copy itself
// (capture_plan.py's "Horizontal bearing convention" / "Elevation
// convention"): negative degrees is LEFT of the design axis, positive is
// RIGHT; negative verticalDeg is BELOW mark height, positive is ABOVE.

import { svg } from "/assets/shared/js/dom.js";

const W = 160;
const H = 96;
const ORIGIN_X = W / 2;
const ORIGIN_Y = 74;
// The real walk never asks for more than a desk-scale move (capture_plan.py's
// widest prompted row is 60 cm at ~1.2 m), so this is a display clamp, not a
// measurement bound -- it keeps a wide prompt readable rather than running
// the dot off the picture.
const MAX_DEG = 30;
const SPREAD_PX = 55;
const RISE_PX = 26;
const ARROWHEAD_PX = 6;
// The mic target's y baseline (at vertical_deg 0) sits above the mark's own
// y, so an on-axis target still reads as a distinct dot rather than
// overlapping the mark.
const BASE_RISE_PX = 20;

function clampDeg(deg) {
  const n = Number(deg) || 0;
  return Math.max(-MAX_DEG, Math.min(MAX_DEG, n));
}

function arrowheadPoints(x1, y1, x2, y2) {
  const angle = Math.atan2(y2 - y1, x2 - x1);
  const spread = Math.PI * 0.85;
  const p1x = x2 + ARROWHEAD_PX * Math.cos(angle + spread);
  const p1y = y2 + ARROWHEAD_PX * Math.sin(angle + spread);
  const p2x = x2 + ARROWHEAD_PX * Math.cos(angle - spread);
  const p2y = y2 + ARROWHEAD_PX * Math.sin(angle - spread);
  return `${x2.toFixed(1)},${y2.toFixed(1)} ${p1x.toFixed(1)},${p1y.toFixed(1)} ${p2x.toFixed(1)},${p2y.toFixed(1)}`;
}

// The picture's own short caption -- distinct from the full prompt sentence
// (walkHeadline/walkDetail), which keeps naming the exact distance; this
// names only the direction, the way a glance at the picture would.
export function positionCaption(degrees, verticalDeg) {
  const deg = Number(degrees) || 0;
  const vDeg = Number(verticalDeg) || 0;
  const parts = [];
  if (deg > 0) parts.push("right of the mark");
  else if (deg < 0) parts.push("left of the mark");
  if (vDeg > 0) parts.push("above mark height");
  else if (vDeg < 0) parts.push("below mark height");
  if (!parts.length) return "On the mark";
  return "Move " + parts.join(" and ");
}

export function positionDiagram(degrees, verticalDeg) {
  const deg = clampDeg(degrees);
  const vDeg = clampDeg(verticalDeg);
  const targetX = ORIGIN_X + (deg / MAX_DEG) * SPREAD_PX;
  const targetY = ORIGIN_Y - BASE_RISE_PX - (vDeg / MAX_DEG) * RISE_PX;
  const moved = deg !== 0 || vDeg !== 0;

  const root = svg("svg.position-diagram", {
    viewBox: `0 0 ${W} ${H}`,
    role: "img",
    "aria-label": positionCaption(degrees, verticalDeg),
  });
  root.appendChild(
    svg("rect.position-diagram__speaker", { x: ORIGIN_X - 9, y: 6, width: 18, height: 11, rx: 2 }),
  );
  root.appendChild(
    svg("circle.position-diagram__mark", { cx: ORIGIN_X, cy: ORIGIN_Y, r: 3 }),
  );
  if (moved) {
    root.appendChild(
      svg("line.position-diagram__arrow", {
        x1: ORIGIN_X, y1: ORIGIN_Y, x2: targetX, y2: targetY,
      }),
    );
    root.appendChild(
      svg("polygon.position-diagram__arrowhead", {
        points: arrowheadPoints(ORIGIN_X, ORIGIN_Y, targetX, targetY),
      }),
    );
  }
  root.appendChild(
    svg("circle.position-diagram__mic", { cx: targetX, cy: targetY, r: 5 }),
  );
  return root;
}
