// SPDX-FileCopyrightText: 2026 Jakub Antalik
// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: MIT

// orbs.js — the nine animated "thinking orb" states, as a shared canvas
// component for the canonical (app.css) pages.
//
// PROVENANCE. The drawing engine below is a port of thinking-orbs
// (https://github.com/Jakubantalik/thinking-orbs), MIT, © 2026 Jakub
// Antalik, taken from src/engine/*.ts + src/presets.ts at upstream commit
// e94f207ea122f8cca0aaa6409ab7fe82d55c38f1. The full notice lives at
// LICENSES/ThinkingOrbs-MIT.txt and the row in LICENSE-third-party.md; keep
// both with any redistributed image. This whole file therefore stays MIT
// rather than the project's Apache-2.0 — it is a derivative of MIT code, and
// MIT is compatible with the rest of the distribution.
//
// WHAT CHANGED FROM UPSTREAM, exhaustively:
//   1. TypeScript annotations stripped; the ES module graph flattened into
//      this one file. No draw math was altered — a future upstream diff
//      still applies almost verbatim.
//   2. React removed. Upstream ships a 164-line <ThinkingOrb> wrapper (a
//      canvas ref + a rAF loop + theme detection); JTS has no build step and
//      no npm in the web layer, so createOrb() below replaces it. The
//      engine itself never imported React.
//   3. The ink ramp is CSS-token driven instead of grayscale. Upstream
//      paints rgba(g,g,g,a) where g comes from a `dark` boolean; here every
//      dot interpolates between --orb-ink and --orb-paper (app.css). Same
//      value ramp, same depth structure — only the two endpoints moved.
//      That also removes upstream's `dark` flag: a dark surface is expressed
//      by the token pair itself, so there is no second code path.
//   Mode painters keep upstream's argument ORDER and arity; the 4th argument
//   is a resolved ramp rather than a boolean.
//
// WHY A DOTTED SPHERE IS CHEAP. These look 3D because they are — dots on a
// sphere, rotated and tilted through a real projection, depth-sorted every
// frame, with depth carried by dot size and ink weight. But the canvas calls
// are only arc() and fill(): no ctx.filter, no SVG filters, no WebGL. That is
// upstream's deliberate constraint and it is why this renders identically in
// Chrome, Safari and Firefox — and why it costs the Pi nothing but the bytes.
//
// USAGE
//
//     import { createOrb } from '/assets/shared/js/orbs.js';
//     const orb = createOrb(canvas, { state: 'breathing', size: 64 });
//     orb.setState('listening');
//     orb.destroy();                       // always, when the node goes away
//
// Colours come from the element's own cascade, so a container can restyle an
// orb without touching this file:
//
//     .curtain { --orb-paper: var(--card); }
//
// One shared rAF loop drives every live orb, so ten orbs cost one loop. Orbs
// scrolled offscreen or on a hidden tab stop painting; under
// prefers-reduced-motion they hold a single representative frame.

/* ===================================================================
   Ink ramp — the JTS layer. Resolves --orb-ink / --orb-paper against a
   real element so var(), color-mix() and oklch() all evaluate in that
   element's cascade, then rasterises through a 1x1 canvas so any colour
   syntax the browser understands becomes plain sRGB bytes.
   =================================================================== */

const INK_PROP = '--orb-ink';
const PAPER_PROP = '--orb-paper';
// Fallbacks matter: this module must still render if it is used on a page
// that does not link app.css.
const INK_EXPR = `var(${INK_PROP}, var(--foreground, #1e2d20))`;
const PAPER_EXPR = `var(${PAPER_PROP}, var(--background, #f7f1e8))`;

const LUT_STEPS = 64;
const DEFAULT_RAMP = makeRamp([30, 45, 32], [247, 241, 232]);

let probeEl = null;
let probeCtx = null;

function rasterize(colorText, fallback) {
  if (!probeCtx) {
    const c = document.createElement('canvas');
    c.width = 1;
    c.height = 1;
    probeCtx = c.getContext('2d', { willReadFrequently: true });
  }
  if (!probeCtx) return fallback;
  probeCtx.clearRect(0, 0, 1, 1);
  // Assigning an unparseable colour leaves fillStyle at its previous value, so
  // seed a sentinel and check whether the assignment took. The sentinel is
  // magenta rather than black: a colour space this canvas cannot parse (a
  // future getComputedStyle returning color(display-p3 …), say) would
  // otherwise be indistinguishable from a legitimately black token.
  probeCtx.fillStyle = '#ff00ff';
  probeCtx.fillStyle = colorText;
  if (probeCtx.fillStyle === '#ff00ff') return fallback;
  probeCtx.fillRect(0, 0, 1, 1);
  const d = probeCtx.getImageData(0, 0, 1, 1).data;
  return [d[0], d[1], d[2]];
}

function resolveToken(host, expr, fallback) {
  if (!host || !host.ownerDocument) return fallback;
  if (!probeEl) {
    probeEl = document.createElement('span');
    probeEl.setAttribute('aria-hidden', 'true');
    probeEl.style.cssText =
      'position:absolute;width:0;height:0;overflow:hidden;visibility:hidden;pointer-events:none';
  }
  const parent = host.parentNode || host;
  parent.appendChild(probeEl);
  probeEl.style.color = '';
  probeEl.style.color = expr;
  const computed = getComputedStyle(probeEl).color;
  probeEl.remove();
  if (!computed) return fallback;
  return rasterize(computed, fallback);
}

function makeRamp(ink, paper) {
  // Precomputed "r,g,b" strings: ~2,000 dots a frame would otherwise redo the
  // same lerp + round + concat for a handful of distinct depths.
  const lut = new Array(LUT_STEPS);
  for (let i = 0; i < LUT_STEPS; i++) {
    const f = i / (LUT_STEPS - 1);
    lut[i] =
      Math.round(ink[0] + (paper[0] - ink[0]) * f) + ',' +
      Math.round(ink[1] + (paper[1] - ink[1]) * f) + ',' +
      Math.round(ink[2] + (paper[2] - ink[2]) * f);
  }
  return { ink, paper, lut };
}

function resolveRamp(host) {
  return makeRamp(
    resolveToken(host, INK_EXPR, DEFAULT_RAMP.ink),
    resolveToken(host, PAPER_EXPR, DEFAULT_RAMP.paper)
  );
}

/** "r,g,b" for an upstream ink value (0 = nearest/densest, 1 = dissolved). */
function inkAt(ramp, white) {
  let w = white;
  if (!(w >= 0)) w = 0;
  else if (w > 1) w = 1;
  return ramp.lut[(w * (LUT_STEPS - 1) + 0.5) | 0];
}

/* ===================================================================
   engine/core.ts — shared primitives. Ported verbatim.
   =================================================================== */

function lerp(a, b, f) { return a + (b - a) * f; }

function frac(x) { return x - Math.floor(x); }

/** Deterministic hash in [0, 1). */
function hashD(a, b) {
  const v = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return v - Math.floor(v);
}

/** Value noise on a 2D lattice — smooth, deterministic, cheap. */
function vnoise(x, y) {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  let fx = x - xi;
  let fy = y - yi;
  fx = fx * fx * (3 - 2 * fx);
  fy = fy * fy * (3 - 2 * fy);
  const a = hashD(xi, yi);
  const b = hashD(xi + 1, yi);
  const c = hashD(xi, yi + 1);
  const d = hashD(xi + 1, yi + 1);
  return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy;
}

/** Stable directions on a unit sphere (Fibonacci lattice). */
function fibDir(i, n) {
  const golden = Math.PI * (3 - Math.sqrt(5));
  const y = 1 - (2 * (i + 0.5)) / n;
  const rad = Math.sqrt(1 - y * y);
  const a = i * golden;
  return [rad * Math.cos(a), y, rad * Math.sin(a)];
}

/** Shortest signed angular distance, wrapped to (-pi, pi]. */
function angleDelta(a, b) {
  return Math.atan2(Math.sin(a - b), Math.cos(a - b));
}

/** Shared spin + tilt + orthographic projection. */
function makeProj(yaw, tilt, cx, cy, scale) {
  const st = Math.sin(tilt);
  const ct = Math.cos(tilt);
  const sy = Math.sin(yaw);
  const cyw = Math.cos(yaw);
  return (x, y, z) => {
    const x1 = x * cyw + z * sy;
    const z1 = -x * sy + z * cyw;
    const y1 = y * ct - z1 * st;
    const z2 = y * st + z1 * ct;
    return [cx + x1 * scale, cy - y1 * scale, z2];
  };
}

function rampOf(maybe) {
  // Upstream passes a `dark` boolean here. Tolerating that keeps a freshly
  // pasted upstream mode from silently painting every dot the same colour.
  return maybe && maybe.lut ? maybe : DEFAULT_RAMP;
}

/** Painter: z-sort far to near, then fill matte dots along the ink ramp. */
function paint(ctx, dots, ramp, rMin) {
  const R = rampOf(ramp);
  const floor = rMin == null ? 0.3 : rMin;
  dots.sort((a, b) => a.z - b.z);
  for (let i = 0; i < dots.length; i++) {
    const d = dots[i];
    const alpha = d.a == null ? 1 : d.a;
    if (alpha < 0.02) continue;
    ctx.fillStyle = 'rgba(' + inkAt(R, d.white) + ',' + alpha + ')';
    ctx.beginPath();
    ctx.arc(d.x, d.y, Math.max(floor, d.r), 0, Math.PI * 2);
    ctx.fill();
  }
}

/** Stroke pass for edge-based modes. Runs before paint so nodes sit on top. */
function paintLines(ctx, lines, ramp) {
  const R = rampOf(ramp);
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    const alpha = l.a == null ? 1 : l.a;
    if (alpha < 0.02) continue;
    ctx.strokeStyle = 'rgba(' + inkAt(R, l.white) + ',' + alpha + ')';
    ctx.lineWidth = l.w;
    ctx.beginPath();
    ctx.moveTo(l.x1, l.y1);
    ctx.lineTo(l.x2, l.y2);
    ctx.stroke();
  }
}

/**
 * Dot radii were tuned for a 300pt frame; sub-linear scaling keeps small
 * spinners legible. Lower pow = radii shrink less with size.
 */
function radiusScale(size, pow) { return Math.pow(size / 300, pow); }

/* ===================================================================
   engine/orbits.ts — "working": particles on tilted orbits.
   =================================================================== */

function drawOrbits(ctx, size, t, ramp, o) {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.82;
  const pt = makeProj(t * 0.12, 0.3, cx, cy, 1);
  const rs = radiusScale(size, o.rsPow == null ? 0.6 : o.rsPow);

  const dots = [];
  const orbitN = o.orbitN == null ? 12 : o.orbitN;
  const ghostN = o.ghostN == null ? 40 : o.ghostN;
  const particles = o.particles == null ? 3 : o.particles;

  for (let orb = 0; orb < orbitN; orb++) {
    const h1 = hashD(orb, 1.7);
    const h2 = hashD(orb, 5.2);
    const h3 = hashD(orb, 8.9);
    const ro = R * (0.45 + 0.52 * h1);
    const th = h1 * 2 * Math.PI;
    const phi = Math.acos(2 * h2 - 1);
    // orbit plane basis (u, v perpendicular to normal n)
    const nx = Math.sin(phi) * Math.cos(th);
    const ny = Math.cos(phi);
    const nz = Math.sin(phi) * Math.sin(th);
    let ux = -ny;
    let uy = nx;
    const uz = 0;
    const ul = Math.max(1e-6, Math.sqrt(ux * ux + uy * uy));
    ux /= ul;
    uy /= ul;
    const vx = ny * uz - nz * uy;
    const vy = nz * ux - nx * uz;
    const vz = nx * uy - ny * ux;
    const speed = (0.25 + 0.55 * h3) * (h3 > 0.5 ? 1 : -1);

    // ghost path
    for (let k = 0; k < ghostN; k++) {
      const a = (k / ghostN) * 2 * Math.PI;
      const p = pt(
        (ux * Math.cos(a) + vx * Math.sin(a)) * ro,
        (uy * Math.cos(a) + vy * Math.sin(a)) * ro,
        (uz * Math.cos(a) + vz * Math.sin(a)) * ro
      );
      const depth = (p[2] / ro + 1) / 2;
      dots.push({
        x: p[0], y: p[1], z: p[2],
        r: (o.ghostR == null ? 0.9 : o.ghostR) * rs,
        white: 0.72,
        a: (o.ghostA == null ? 0.5 : o.ghostA) * (0.4 + 0.6 * depth)
      });
    }
    // the particles doing the work
    for (let m = 0; m < particles; m++) {
      const a = t * speed + (m / particles) * 2 * Math.PI + h2 * 6;
      const p = pt(
        (ux * Math.cos(a) + vx * Math.sin(a)) * ro,
        (uy * Math.cos(a) + vy * Math.sin(a)) * ro,
        (uz * Math.cos(a) + vz * Math.sin(a)) * ro
      );
      const depth = (p[2] / ro + 1) / 2;
      dots.push({
        x: p[0], y: p[1], z: p[2],
        r: ((o.partR == null ? 1.2 : o.partR) +
            (o.partRDepth == null ? 1.6 : o.partRDepth) * depth) * rs,
        white: 0.3 - 0.22 * depth
      });
    }
  }
  paint(ctx, dots, ramp, o.rMin);
}

/* ===================================================================
   engine/lattice.ts — "searching" / "solving" / "listening".
   =================================================================== */

// The solver heartbeat (rubik): rapid eased moves scramble, then replay in
// reverse (palindrome) so everything clicks back to solved, rests, repeats.
function solveCycle(time, count, slotDur, rest) {
  const cyc = 2 * count * slotDur + rest;
  const tc = time % cyc;
  const amount = new Array(count).fill(0);
  let active = -1;
  if (tc < 2 * count * slotDur) {
    const slot = Math.floor(tc / slotDur);
    const p = (tc - slot * slotDur) / slotDur;
    const cl = Math.min(1, p / 0.7);
    const ep = 1 - Math.pow(1 - cl, 3); // machine ease-out
    if (slot < count) {
      for (let i = 0; i < slot; i++) amount[i] = 1;
      amount[slot] = ep;
      active = slot;
    } else {
      const u = 2 * count - 1 - slot;
      for (let i = 0; i < u; i++) amount[i] = 1;
      amount[u] = 1 - ep;
      active = u;
    }
  }
  return { amount, active };
}

function applyMoves(pt3, moves, sc) {
  let x = pt3[0];
  let y = pt3[1];
  let z = pt3[2];
  let inActive = false;
  for (let i = 0; i < moves.length; i++) {
    if (sc.amount[i] <= 0) continue;
    const mv = moves[i];
    const coord = mv.axis === 0 ? x : mv.axis === 1 ? y : z;
    if (coord < mv.lo || coord >= mv.hi) continue;
    if (i === sc.active) inActive = true;
    const a = mv.ang * sc.amount[i];
    const ca = Math.cos(a);
    const sa = Math.sin(a);
    if (mv.axis === 0) {
      const y2 = y * ca - z * sa;
      z = y * sa + z * ca;
      y = y2;
    } else if (mv.axis === 1) {
      const x2 = x * ca + z * sa;
      z = -x * sa + z * ca;
      x = x2;
    } else {
      const x2 = x * ca - y * sa;
      y = x * sa + y * ca;
      x = x2;
    }
  }
  return [x, y, z, inActive];
}

function makeMoves(count) {
  const moves = [];
  for (let i = 0; i < count; i++) {
    const axis = Math.min(2, Math.floor(hashD(i, 2.3) * 3));
    const lo = -1.0 + 0.5 * Math.min(3, Math.floor(hashD(i, 5.9) * 4));
    const dir = hashD(i, 7.7) < 0.5 ? 1 : -1;
    moves.push({ axis, lo, hi: lo + 0.5, ang: (dir * Math.PI) / 2 });
  }
  return moves;
}

function drawGlobe(ctx, size, t, ramp, o) {
  const spin = 0.5;
  const cx = size / 2;
  const cy = size / 2;
  const radius = (size / 2) * 0.82;
  const tilt = 0.4 + 0.06 * Math.sin(t * 0.35);
  const pt = makeProj(t * spin, tilt, cx, cy, radius);
  // scan sweeps relative to the spin; scanMul scales that relative rate
  const scan = t * (spin + (1.7 - spin) * (o.scanMul == null ? 1 : o.scanMul));
  const rs = radiusScale(size, o.rsPow == null ? 0.6 : o.rsPow);
  const dimBase = o.dimBase == null ? 1 : o.dimBase;

  const dots = [];
  const latRings = o.latRings == null ? 17 : o.latRings;
  const lonDensity = o.lonDensity == null ? 44 : o.lonDensity;
  for (let li = 0; li <= latRings; li++) {
    const lat = -Math.PI / 2 + (li / latRings) * Math.PI;
    const cosLat = Math.cos(lat);
    const sinLat = Math.sin(lat);
    const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity));
    for (let lj = 0; lj < lonCount; lj++) {
      const lon = (lj / lonCount) * 2 * Math.PI;
      const p = pt(cosLat * Math.cos(lon), sinLat, cosLat * Math.sin(lon));
      const depth = (p[2] + 1) / 2;
      // the scan: a moving meridian read as a size ripple, not a shine
      const d = angleDelta(lon + t * spin, scan);
      const boost = Math.exp(-(d * d) / 0.18) * Math.max(0, p[2]);
      dots.push({
        x: p[0], y: p[1], z: p[2],
        r: ((o.rBase == null ? 0.6 : o.rBase) +
            (o.rDepth == null ? 1.7 : o.rDepth) * depth +
            (o.rBoost == null ? 1 : o.rBoost) * boost) * rs,
        white: (o.inkFar == null ? 0.62 : o.inkFar) -
               (o.inkSpan == null ? 0.54 : o.inkSpan) * depth,
        // dimBase < 1 fades un-scanned dots so the meridian reads clearly
        a: dimBase + (1 - dimBase) * Math.min(1, boost)
      });
    }
  }
  paint(ctx, dots, ramp, o.rMin);
}

function drawRubik(ctx, size, t, ramp, o) {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.82;
  const pt = makeProj(t * 0.55, 0.35 + 0.1 * Math.sin(t * 0.9), cx, cy, R);
  const rs = radiusScale(size, o.rsPow == null ? 0.6 : o.rsPow);
  const moveCount = o.moveCount == null ? 14 : o.moveCount;
  const moves = makeMoves(moveCount);
  const sc = solveCycle(t, moveCount, 0.42, 1.2);

  const dots = [];
  const latRings = o.latRings == null ? 15 : o.latRings;
  const lonDensity = o.lonDensity == null ? 40 : o.lonDensity;
  for (let li = 0; li <= latRings; li++) {
    const lat = -Math.PI / 2 + (li / latRings) * Math.PI;
    const cosLat = Math.cos(lat);
    const sinLat = Math.sin(lat);
    const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity));
    for (let lj = 0; lj < lonCount; lj++) {
      const lon = (lj / lonCount) * 2 * Math.PI;
      const mv = applyMoves(
        [cosLat * Math.cos(lon), sinLat, cosLat * Math.sin(lon)], moves, sc
      );
      const p = pt(mv[0], mv[1], mv[2]);
      const inActive = mv[3];
      const depth = (p[2] + 1) / 2;
      // the band being turned inks a touch darker — the "hand"
      dots.push({
        x: p[0], y: p[1], z: p[2],
        r: ((o.rBase == null ? 0.6 : o.rBase) +
            (o.rDepth == null ? 1.7 : o.rDepth) * depth +
            (inActive ? (o.rActive == null ? 0.3 : o.rActive) : 0)) * rs,
        white: (o.inkFar == null ? 0.62 : o.inkFar) -
               (o.inkSpan == null ? 0.54 : o.inkSpan) * depth -
               (inActive ? 0.14 : 0)
      });
    }
  }
  paint(ctx, dots, ramp, o.rMin);
}

function drawWave(ctx, size, t, ramp, o) {
  const cx = size / 2;
  const cy = size / 2;
  // 0.76 base x 1.15 — the undulation pulls the sphere inward, so wave read
  // ~15% smaller than the other lattice modes; scaled up to match them
  const R = (size / 2) * 0.874;
  const pt = makeProj(t * 0.18, 0.38, cx, cy, 1);
  const rs = radiusScale(size, o.rsPow == null ? 0.6 : o.rsPow);

  const dots = [];
  const rings = o.rings == null ? 15 : o.rings;
  const lonDensity = o.lonDensity == null ? 40 : o.lonDensity;
  for (let ri = 0; ri <= rings; ri++) {
    const lat = -Math.PI / 2 + (ri / rings) * Math.PI;
    const cosLat = Math.cos(lat);
    const sinLat = Math.sin(lat);
    // two waves, different tempi — organic, never quite repeating
    const w = 0.62 * Math.sin(t * 2.1 - ri * 0.52) + 0.38 * Math.sin(t * 1.27 + ri * 0.83);
    const rr = R * (0.88 + 0.105 * w);
    const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity));
    for (let lj = 0; lj < lonCount; lj++) {
      const lon = (lj / lonCount) * 2 * Math.PI;
      const p = pt(cosLat * Math.cos(lon) * rr, sinLat * rr, cosLat * Math.sin(lon) * rr);
      const depth = (p[2] / R + 1) / 2;
      const crest = Math.max(0, w);
      dots.push({
        x: p[0], y: p[1], z: p[2],
        r: ((o.rBase == null ? 0.6 : o.rBase) +
            (o.rDepth == null ? 1.7 : o.rDepth) * depth) * (1 + 0.4 * crest) * rs,
        white: 0.66 - 0.56 * depth - 0.1 * crest
      });
    }
  }
  paint(ctx, dots, ramp, o.rMin);
}

/* ===================================================================
   engine/web.ts — "connecting": a constellation wires itself.
   =================================================================== */

function drawWeb(ctx, size, t, ramp, o) {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.8 * (o.spread == null ? 1 : o.spread);
  // the projector carries the radius as its scale, so node vectors stay
  // unit-length and the distances below are in unit-sphere space
  const pt = makeProj(t * 0.12, 0.32, cx, cy, R);
  const rs = radiusScale(size, o.rsPow == null ? 0.6 : o.rsPow);

  const nodeN = o.nodeN == null ? 30 : o.nodeN;
  const thr = o.thr == null ? 0.72 : o.thr;
  const nodeR = o.nodeR == null ? 1.4 : o.nodeR;
  const nodeRDepth = o.nodeRDepth == null ? 1.8 : o.nodeRDepth;

  // nodes: fib lattice + slow noise wander, renormalised to the surface
  const nodes = [];
  for (let i = 0; i < nodeN; i++) {
    const d = fibDir(i, nodeN);
    const x = d[0] + 0.3 * (vnoise(i * 0.31 + 9, t * 0.24) - 0.5) * 2;
    const y = d[1] + 0.3 * (vnoise(i * 0.53 + 27, t * 0.21) - 0.5) * 2;
    const z = d[2] + 0.3 * (vnoise(i * 0.77 + 55, t * 0.27) - 0.5) * 2;
    const l = Math.sqrt(x * x + y * y + z * z);
    nodes.push([x / l, y / l, z / l]);
  }

  const lines = [];
  const dots = [];

  // edges between close neighbours, alpha by proximity + depth
  for (let i = 0; i < nodeN; i++) {
    for (let j = i + 1; j < nodeN; j++) {
      const dx = nodes[i][0] - nodes[j][0];
      const dy = nodes[i][1] - nodes[j][1];
      const dz = nodes[i][2] - nodes[j][2];
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
      if (dist >= thr) continue;
      const p1 = pt(nodes[i][0], nodes[i][1], nodes[i][2]);
      const p2 = pt(nodes[j][0], nodes[j][1], nodes[j][2]);
      const depth = ((p1[2] + p2[2]) / 2 + 1) / 2;
      lines.push({
        x1: p1[0], y1: p1[1], x2: p2[0], y2: p2[1],
        white: 0.42,
        a: (1 - dist / thr) * (0.3 + 0.55 * depth),
        w: Math.max(0.6, (o.lineW == null ? 0.8 : o.lineW) * rs)
      });
    }
  }

  for (let i = 0; i < nodeN; i++) {
    const p = pt(nodes[i][0], nodes[i][1], nodes[i][2]);
    const depth = (p[2] + 1) / 2;
    const pulse = 1 + 0.25 * Math.sin(t * 1.4 + i * 2.7);
    dots.push({
      x: p[0], y: p[1], z: p[2],
      r: (nodeR + nodeRDepth * depth) * pulse * rs,
      white: 0.55 - 0.45 * depth
    });
  }

  // signals: bright packets running between paired nodes
  const signals = o.signals == null ? 5 : o.signals;
  for (let s = 0; s < signals; s++) {
    const seg = Math.floor(t * 0.55 + s * 7.31);
    const a = Math.floor(hashD(seg, s * 3.1 + 1.7) * nodeN);
    const b = Math.floor(hashD(seg, s * 5.7 + 4.2) * nodeN);
    if (a === b) continue;
    const f = frac(t * 0.55 + s * 7.31);
    const x = lerp(nodes[a][0], nodes[b][0], f);
    const y = lerp(nodes[a][1], nodes[b][1], f);
    const z = lerp(nodes[a][2], nodes[b][2], f);
    const l = Math.max(1e-6, Math.sqrt(x * x + y * y + z * z));
    const p = pt(x / l, y / l, z / l);
    const depth = (p[2] + 1) / 2;
    dots.push({
      x: p[0], y: p[1], z: p[2],
      r: (nodeR * 1.5 + nodeRDepth * depth) * rs,
      white: 0.05,
      a: 0.5 + 0.5 * depth
    });
  }

  paintLines(ctx, lines, ramp);
  paint(ctx, dots, ramp, o.rMin);
}

/* ===================================================================
   engine/braid.ts — "weaving": three strands plait around the sphere.
   =================================================================== */

function drawBraid(ctx, size, t, ramp, o) {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.76;
  const pt = makeProj(t * 0.4, 0.3, cx, cy, 1);
  const rs = radiusScale(size, o.rsPow == null ? 0.6 : o.rsPow);

  const dots = [];
  const ghostN = o.ghostN == null ? 150 : o.ghostN;
  for (let i = 0; i < ghostN; i++) {
    const d = fibDir(i, ghostN);
    const p = pt(d[0] * R, d[1] * R, d[2] * R);
    const depth = (p[2] / R + 1) / 2;
    dots.push({ x: p[0], y: p[1], z: p[2], r: 0.8 * rs, white: 0.78, a: 0.1 + 0.22 * depth });
  }

  const strandN = o.strandN == null ? 52 : o.strandN;
  const turns = o.turns == null ? 3 : o.turns;
  for (let s = 0; s < 3; s++) {
    const phase = (s / 3) * 2 * Math.PI;
    for (let i = 0; i < strandN; i++) {
      // u walks pole to pole; the frac() drift slides the whole strand along
      const u = (frac(i / strandN + t * 0.045) * 2 - 1) * 0.96;
      const surf = Math.sqrt(Math.max(0, 1 - u * u));
      const endFade = Math.min(1, (1 - Math.abs(u)) / 0.1);
      const a = u * Math.PI * turns + phase;
      // radial breathing: strands trade places — the over/under of a plait
      const weave = 1 + 0.075 * Math.sin(u * Math.PI * turns * 2 + phase * 2 + t * 0.8);
      const rr = surf * R * weave;
      const p = pt(Math.cos(a) * rr, u * R * weave, Math.sin(a) * rr);
      const depth = (p[2] / R + 1) / 2;
      dots.push({
        x: p[0], y: p[1], z: p[2],
        r: ((o.rBase == null ? 1.2 : o.rBase) +
            (o.rDepth == null ? 1.8 : o.rDepth) * depth) * rs,
        white: 0.55 - 0.45 * depth,
        a: endFade * (0.45 + 0.55 * depth)
      });
    }
  }
  paint(ctx, dots, ramp, o.rMin);
}

/* ===================================================================
   engine/ribbon.ts — "composing" (sash) and "breathing" (faceOn ring).
   =================================================================== */

function drawRibbon(ctx, size, t, ramp, o) {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.78;
  // spin scales the 3D tumble; spin=0 freezes the band's orientation,
  // leaving only the traveling undulation
  const spin = o.spin == null ? 1 : o.spin;
  const camTilt = 0.3;
  const pt = makeProj(t * 0.1 * spin, camTilt, cx, cy, 1);
  const rs = radiusScale(size, o.rsPow == null ? 0.6 : o.rsPow);

  const dots = [];
  const ghostN = o.ghostN == null ? 150 : o.ghostN;
  for (let i = 0; i < ghostN; i++) {
    const d = fibDir(i, ghostN);
    const p = pt(d[0] * R, d[1] * R, d[2] * R);
    const depth = (p[2] / R + 1) / 2;
    dots.push({ x: p[0], y: p[1], z: p[2], r: 0.8 * rs, white: 0.78, a: 0.1 + 0.22 * depth });
  }

  // The band plane, precessing (frozen when spin=0). The projection squashes
  // the band's great circle vertically by cos(ta + camTilt); face-on sets
  // ta = -camTilt so that term is 1 and the band reads as a true circle
  // rather than ribbon's tilted ellipse.
  const ya = t * 0.24 * spin;
  const ta = o.faceOn ? -camTilt : 0.55 + 0.3 * Math.sin(t * 0.18) * spin;
  const ux = Math.cos(ya);
  const uy = 0;
  const uz = Math.sin(ya);
  const vx = -uz * Math.sin(ta);
  const vy = Math.cos(ta);
  const vz = ux * Math.sin(ta);
  // plane normal n = u x v
  const nx = uy * vz - uz * vy;
  const ny = uz * vx - ux * vz;
  const nz = ux * vy - uy * vx;

  // Radial lobes swell past R, so pull the base radius in by (most of) the
  // wobble amplitude. The silhouette then stays inside the frame however far
  // the deformation is pushed, while lobes keep getting deeper relative to
  // the mean radius.
  const wobAmp = 0.23 * (o.wobMul == null ? 1 : o.wobMul);
  const baseR = o.faceOn ? R / (1 + 0.85 * wobAmp) : R;

  const baseLanes = o.lanes == null ? 5 : o.lanes;
  const segs = o.segs == null ? 88 : o.segs;
  const lanes = Math.max(1, Math.round(baseLanes * (o.bandMul == null ? 1 : o.bandMul)));
  for (let w = 0; w < lanes; w++) {
    const laneOff = (w - (lanes - 1) / 2) * 0.075;
    const edge = Math.abs(w - (lanes - 1) / 2) / Math.max(1, (lanes - 1) / 2);
    for (let k = 0; k < segs; k++) {
      const a = (k / segs) * 2 * Math.PI;
      // the undulation: two traveling waves along the band; wobMul scales
      // the deformation — 0 is a clean band
      const wob =
        (0.16 * Math.sin(a * 3 - t * 1.7 + w * 0.22) + 0.07 * Math.sin(a * 5 + t * 1.1)) *
        (o.wobMul == null ? 1 : o.wobMul);
      // A normal-direction wobble is cancelled by the re-normalisation below:
      // the point lands back on the sphere, so the silhouette is pinned at R
      // and the deformation can only ever pull dots inward. Face-on instead
      // modulates the in-plane RADIUS, so lobes genuinely swell outward and
      // pinch inward. Ribbon keeps the original out-of-plane sash wobble.
      const radial = o.faceOn ? 1 + wob : 1;
      const off = o.faceOn ? laneOff : laneOff + wob;
      const x = ux * Math.cos(a) + vx * Math.sin(a) + nx * off;
      const y = uy * Math.cos(a) + vy * Math.sin(a) + ny * off;
      const z = uz * Math.cos(a) + vz * Math.sin(a) + nz * off;
      const l = Math.sqrt(x * x + y * y + z * z);
      const rr = baseR * radial;
      const p = pt((x / l) * rr, (y / l) * rr, (z / l) * rr);
      const depth = (p[2] / R + 1) / 2;
      dots.push({
        x: p[0], y: p[1], z: p[2],
        r: ((o.rBase == null ? 1.1 : o.rBase) +
            (o.rDepth == null ? 1.7 : o.rDepth) * depth) * (1 - 0.25 * edge) * rs,
        white: 0.52 - 0.44 * depth + 0.18 * edge,
        a: 0.4 + 0.6 * depth
      });
    }
  }
  paint(ctx, dots, ramp, o.rMin);
}

/* ===================================================================
   engine/morph.ts — "shaping": circle -> triangle -> square.
   =================================================================== */

function smoothE(x) { return x * x * (3 - 2 * x); }

function polyPath(verts) {
  const V = verts.length;
  const L = [];
  let total = 0;
  for (let i = 0; i < V; i++) {
    const a = verts[i];
    const b = verts[(i + 1) % V];
    const l = Math.hypot(b[0] - a[0], b[1] - a[1]);
    L.push(l);
    total += l;
  }
  return (f) => {
    let target = f * total;
    let i = 0;
    while (target > L[i] && i < V - 1) {
      target -= L[i];
      i++;
    }
    const a = verts[i];
    const b = verts[(i + 1) % V];
    const ff = L[i] ? Math.min(1, target / L[i]) : 0;
    return [a[0] + (b[0] - a[0]) * ff, a[1] + (b[1] - a[1]) * ff];
  };
}

const CIRCLE = (f) => {
  const a = -Math.PI / 2 + f * 2 * Math.PI;
  return [Math.cos(a) * 0.24, Math.sin(a) * 0.24];
};
const TRIANGLE = polyPath([[0.0, -0.26], [0.24, 0.16], [-0.24, 0.16]]);
// 5-vertex walk so the path STARTS at top-centre like the other shapes
const SQUARE = polyPath([[0, -0.2], [0.2, -0.2], [0.2, 0.2], [-0.2, 0.2], [-0.2, -0.2]]);
const CYCLE = [CIRCLE, TRIANGLE, SQUARE];

// low floor keeps sparse outlines possible while never degenerating
function morphN(d) { return Math.max(6, Math.round(34 * d)); }

const MORPH_HOLD = 1.4;
const MORPH_TWEEN = 0.9;
const MORPH_SEG = MORPH_HOLD + MORPH_TWEEN;

// Upstream notes this state was tuned in inkform, which paints it through a
// blur + threshold "goo" filter; plain circles are drawn instead, since
// ctx.filter and SVG filter refs are not safe across Chrome / Safari /
// Firefox. The dot GEOMETRY is identical either way — the threshold just
// yields a hard edge where a plain fill has an antialiased one, so these dots
// read a touch softer. Do not "correct" for that by shrinking the radius: it
// makes the mark genuinely smaller than the tuning.
function drawMorph(ctx, size, t, ramp, o) {
  const K = CYCLE.length;
  const tc = t % (MORPH_SEG * K);
  const k = Math.floor(tc / MORPH_SEG);
  const local = tc - k * MORPH_SEG;
  const m = local > MORPH_HOLD ? smoothE((local - MORPH_HOLD) / MORPH_TWEEN) : 0;
  const sprd = o.spread == null ? 1 : o.spread;

  // blend the two shape PATHS at m, then measure the blended outline
  const pA = CYCLE[k];
  const pB = CYCLE[(k + 1) % K];
  const M = 160;
  const pts = [];
  for (let i = 0; i < M; i++) {
    const f = i / M;
    const a = pA(f);
    const b = pB(f);
    pts.push([(a[0] + (b[0] - a[0]) * m) * sprd, (a[1] + (b[1] - a[1]) * m) * sprd]);
  }
  const L = [];
  let total = 0;
  for (let i = 0; i < M; i++) {
    const a = pts[i];
    const b = pts[(i + 1) % M];
    const l = Math.hypot(b[0] - a[0], b[1] - a[1]);
    L.push(l);
    total += l;
  }

  // dot radius depends ONLY on rDot (the size knob); the count sets the
  // gaps. Formed shapes breathe a little (uniform pulse).
  const n = morphN(o.iconD == null ? 1 : o.iconD);
  const re = (o.rDot == null ? 0.021 : o.rDot) * 1.35 * sprd;
  const pulse = 1 + 0.02 * Math.sin(local * 3.1);

  const dots = [];
  const c2 = size / 2;
  let seg = 0;
  let acc = 0;
  for (let k2 = 0; k2 < n; k2++) {
    const target = (k2 / n) * total;
    while (acc + L[seg] < target && seg < M - 1) {
      acc += L[seg];
      seg++;
    }
    const a = pts[seg];
    const b = pts[(seg + 1) % M];
    const f = L[seg] ? Math.min(1, (target - acc) / L[seg]) : 0;
    const x = (a[0] + (b[0] - a[0]) * f) * pulse;
    const y = (a[1] + (b[1] - a[1]) * f) * pulse;
    dots.push({
      x: c2 + x * size,
      y: c2 + y * size,
      z: 0,
      r: Math.max(0.35, re * size),
      white: 0.1
    });
  }
  paint(ctx, dots, ramp, o.rMin);
}

/* ===================================================================
   engine/registry.ts + engine/profiles.ts + presets.ts
   =================================================================== */

/** Mode key -> frame painter. */
export const MODE_DRAWS = {
  orbits: drawOrbits,
  globe: drawGlobe,
  rubik: drawRubik,
  wave: drawWave,
  web: drawWeb,
  braid: drawBraid,
  ribbon: drawRibbon,
  // ring shares ribbon's painter — the faceOn profile flag switches it
  ring: drawRibbon,
  morph: drawMorph
};

// 2-D lattices (rings x dots-per-ring) come in pairs — each side takes
// sqrt(scale) so the TOTAL dot count scales by `scale`; flat lists scale
// linearly. iconD sets the morph outline's sampling density.
const COUNT_PAIRS = [
  ['latRings', 'lonDensity'],
  ['rings', 'lonDensity'],
  ['lanes', 'segs']
];
const COUNT_KEYS = ['orbitN', 'ghostN', 'nodeN', 'strandN', 'signals'];
const ICON_DENSITY_KEYS = ['iconD'];

// Every key that sets a dot's rendered radius — scaling all of them keeps a
// dot's near/far falloff intact while shrinking or growing the mark.
const RADIUS_KEYS = [
  'rBase', 'rDepth', 'rActive', 'rDot', 'ghostR',
  'partR', 'partRDepth', 'nodeR', 'nodeRDepth'
];

function scaleCounts(opts, scale) {
  const out = { ...opts };
  const done = new Set();
  const rt = Math.sqrt(scale);
  for (const [a, b] of COUNT_PAIRS) {
    if (out[a] != null && out[b] != null && !done.has(a) && !done.has(b)) {
      out[a] = Math.max(2, Math.round(out[a] * rt));
      out[b] = Math.max(2, Math.round(out[b] * rt));
      done.add(a);
      done.add(b);
    }
  }
  for (const k of COUNT_KEYS) {
    const v = out[k];
    // 0 means the mode opted out of that layer entirely (ring has no ghost
    // sphere) — scaling must not resurrect it as a single stray dot
    if (v != null && v !== 0 && !done.has(k)) out[k] = Math.max(1, Math.round(v * scale));
  }
  for (const k of ICON_DENSITY_KEYS) {
    if (out[k] != null) out[k] = Math.max(0.02, out[k] * scale);
  }
  return out;
}

function scaleRadii(opts, scale) {
  const out = { ...opts };
  for (const k of RADIUS_KEYS) {
    if (out[k] != null) out[k] = out[k] * scale;
  }
  // remember the multiplier itself — spacing-derived radii (the morph
  // outline) use it, since they aren't based on any single radius key
  out.rSizeMul = (out.rSizeMul == null ? 1 : out.rSizeMul) * scale;
  return out;
}

/** Base (fine) profiles per mode, before preset multipliers. */
const BASE_PROFILES = {
  globe: { latRings: 17, lonDensity: 44, rBase: 0.6, rDepth: 1.7, rBoost: 1.0, inkFar: 0.62, inkSpan: 0.54, rsPow: 0.6, rMin: 0.3 },
  orbits: { orbitN: 12, ghostN: 40, ghostR: 0.9, ghostA: 0.5, particles: 3, partR: 1.2, partRDepth: 1.6, rsPow: 0.6, rMin: 0.3 },
  rubik: { latRings: 15, lonDensity: 40, moveCount: 14, rBase: 0.6, rDepth: 1.7, rActive: 0.3, inkFar: 0.62, inkSpan: 0.54, rsPow: 0.6, rMin: 0.3 },
  wave: { rings: 15, lonDensity: 40, rBase: 0.6, rDepth: 1.7, rsPow: 0.6, rMin: 0.3 },
  web: { nodeN: 30, thr: 0.72, signals: 5, nodeR: 1.4, nodeRDepth: 1.8, lineW: 0.8, rsPow: 0.6, rMin: 0.3 },
  braid: { strandN: 52, turns: 3.0, ghostN: 150, rBase: 1.2, rDepth: 1.8, rsPow: 0.6, rMin: 0.3 },
  ribbon: { lanes: 5, segs: 88, ghostN: 150, rBase: 1.1, rDepth: 1.7, rsPow: 0.6, rMin: 0.3 },
  // ring shares ribbon's painter; faceOn cancels the camera tilt and moves
  // the undulation onto the radius, and there is no ghost sphere behind it
  ring: { lanes: 5, segs: 88, ghostN: 0, faceOn: 1, rBase: 1.1, rDepth: 1.7, rsPow: 0.6, rMin: 0.3 },
  morph: { rDot: 0.021, iconD: 1, rMin: 0.25 }
};

/** The nine shipped states, in upstream's documented order. */
export const ORB_STATES = [
  'working', 'searching', 'solving', 'listening', 'connecting',
  'weaving', 'composing', 'breathing', 'shaping'
];

const STATE_TO_MODE = {
  working: 'orbits',
  searching: 'globe',
  solving: 'rubik',
  listening: 'wave',
  connecting: 'web',
  weaving: 'braid',
  composing: 'ribbon',
  breathing: 'ring',
  shaping: 'morph'
};

// The shipped tunings: nine states x two sizes. count/size are multipliers
// over the base fine profiles; speed multiplies the shared clock. 64 and 20
// are separate designs, not a scale factor.
const PRESETS = {
  orbits: {
    64: { speed: 1.885, count: 1, size: 1 },
    20: { speed: 3.9, count: 0.238, size: 2.4 }
  },
  globe: {
    64: { speed: 2.015, count: 0.42, size: 1.15, extra: { scanMul: 4.08, dimBase: 0.45 } },
    20: { speed: 2.665, count: 0.105, size: 1.75, extra: { scanMul: 4.335, dimBase: 0.45 } }
  },
  rubik: {
    64: { speed: 1.82, count: 0.35, size: 1.05 },
    20: { speed: 1.95, count: 0.088, size: 1.9 }
  },
  wave: {
    64: { speed: 4.388, count: 0.341, size: 1 },
    20: { speed: 3.998, count: 0.105, size: 1.6 }
  },
  web: {
    64: { speed: 3.315, count: 1.35, size: 0.95 },
    20: { speed: 6.63, count: 0.25, size: 1.52 }
  },
  braid: {
    64: { speed: 1.625, count: 0.5, size: 1 },
    20: { speed: 2.75, count: 0.1125, size: 1.36 }
  },
  ribbon: {
    64: { speed: 2.34, count: 0.25, size: 0.85, extra: { spin: 0, bandMul: 3.9, wobMul: 1 } },
    20: { speed: 3.12, count: 0.051, size: 1.073, extra: { spin: 0, bandMul: 4.94, wobMul: 1 } }
  },
  ring: {
    64: { speed: 3.24, count: 0.25, size: 0.956, extra: { spin: 0, bandMul: 3.627, wobMul: 0.368 } },
    20: { speed: 3.78, count: 0.028, size: 1.622, extra: { spin: 0, bandMul: 3.968, wobMul: 0.565 } }
  },
  morph: {
    64: { speed: 2.405, count: 0.702, size: 0.395, extra: { spread: 1.45 } },
    20: { speed: 2.08, count: 0.53, size: 1.011, extra: { spread: 1.45 } }
  }
};

const presetCache = new Map();

/** Resolve a (state, preset size) pair to its mode + fully-scaled options. */
export function resolvePreset(state, sizeKey) {
  const key = state + '-' + sizeKey;
  const hit = presetCache.get(key);
  if (hit) return hit;

  const mode = STATE_TO_MODE[state];
  if (!mode) throw new Error(`unknown orb state "${state}"`);
  const preset = PRESETS[mode][sizeKey];
  let opts = { ...BASE_PROFILES[mode] };
  if (preset.count !== 1) opts = scaleCounts(opts, preset.count);
  if (preset.size !== 1) opts = scaleRadii(opts, preset.size);
  if (preset.extra) opts = { ...opts, ...preset.extra };

  const resolved = { mode, speed: preset.speed, opts };
  presetCache.set(key, resolved);
  return resolved;
}

/* ===================================================================
   createOrb — the JTS replacement for upstream's React component.
   =================================================================== */

const DEFAULT_LABELS = {
  working: 'Working',
  searching: 'Searching',
  solving: 'Solving',
  listening: 'Listening',
  connecting: 'Connecting',
  weaving: 'Weaving',
  composing: 'Composing',
  breathing: 'Working',
  shaping: 'Working'
};

// A single frame that reads as representative rather than as a start-of-cycle
// pose, for prefers-reduced-motion.
const STILL_T = 1.2;

const live = new Set();
let rafId = 0;
let observer = null;

function ensureObserver() {
  if (observer || typeof IntersectionObserver === 'undefined') return observer;
  observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const orb = e.target.__jtsOrb;
      if (orb) orb.visible = e.isIntersecting;
    }
    // An orb scrolled back into view has to restart the shared loop: tick()
    // stops scheduling once every live orb is paused, still or offscreen.
    schedule();
  }, { rootMargin: '64px' });
  return observer;
}

function tick() {
  rafId = 0;
  // performance.now() is the shared clock: two orbs of the same state stay in
  // phase, and one resuming after a pause rejoins where the others are.
  const now = performance.now();
  let wantsMore = false;
  for (const orb of live) {
    if (orb.paused || !orb.visible) continue;
    if (orb.reduced) {
      if (orb.stillDrawn) continue;
      orb.render(STILL_T * 1000);
      orb.stillDrawn = true;
      continue;
    }
    orb.render(now);
    wantsMore = true;
  }
  if (wantsMore && !document.hidden) schedule();
}

function schedule() {
  if (rafId || live.size === 0) return;
  rafId = requestAnimationFrame(tick);
}

function wake() {
  for (const orb of live) orb.stillDrawn = false;
  schedule();
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) schedule();
  });
}
if (typeof window !== 'undefined' && window.matchMedia) {
  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  const onMotion = () => {
    for (const orb of live) {
      orb.reduced = motionQuery.matches;
      orb.stillDrawn = false;
    }
    schedule();
  };
  if (motionQuery.addEventListener) motionQuery.addEventListener('change', onMotion);
  // A theme flip changes what --orb-ink/--orb-paper resolve to; re-read them.
  const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');
  const onScheme = () => {
    for (const orb of live) orb.refresh();
  };
  if (darkQuery.addEventListener) darkQuery.addEventListener('change', onScheme);
}

/**
 * Attach an animated orb to a <canvas>.
 *
 * @param {HTMLCanvasElement} canvas  target; sized by this function
 * @param {object} [options]
 * @param {string} [options.state='working']  one of ORB_STATES
 * @param {number} [options.size=64]   rendered CSS px. Tuned presets exist at
 *   64 and 20; any other size draws the nearer tuning at that scale.
 * @param {number} [options.speed=1]   multiplier on the preset's baked speed
 * @param {boolean} [options.paused=false]
 * @param {string} [options.label]     aria-label; defaults per state
 * @returns {{setState:Function, setSpeed:Function, pause:Function,
 *            resume:Function, refresh:Function, destroy:Function}}
 */
export function createOrb(canvas, options = {}) {
  if (!canvas || canvas.tagName !== 'CANVAS') {
    throw new TypeError('createOrb(canvas): first argument must be a <canvas>');
  }
  const state0 = options.state || 'working';
  if (!STATE_TO_MODE[state0]) throw new Error(`unknown orb state "${state0}"`);

  const ctx = canvas.getContext('2d');
  const dpr = Math.min(2, window.devicePixelRatio || 1);

  const orb = {
    canvas,
    ctx,
    state: state0,
    size: 0,
    speed: options.speed == null ? 1 : options.speed,
    paused: !!options.paused,
    visible: true,
    stillDrawn: false,
    reduced:
      typeof window !== 'undefined' && window.matchMedia
        ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
        : false,
    ramp: DEFAULT_RAMP,

    resize(size) {
      orb.size = size;
      canvas.width = Math.round(size * dpr);
      canvas.height = Math.round(size * dpr);
      canvas.style.width = size + 'px';
      canvas.style.height = size + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      orb.stillDrawn = false;
    },

    render(nowMs) {
      const key = orb.size <= 32 ? 20 : 64;
      const preset = resolvePreset(orb.state, key);
      const t = (nowMs / 1000) * preset.speed * orb.speed;
      ctx.clearRect(0, 0, orb.size, orb.size);
      MODE_DRAWS[preset.mode](ctx, orb.size, t, orb.ramp, preset.opts);
    },

    /** Re-read --orb-ink / --orb-paper from the canvas's own cascade. */
    refresh() {
      orb.ramp = resolveRamp(canvas);
      orb.stillDrawn = false;
      schedule();
      return handle;
    }
  };

  canvas.__jtsOrb = orb;
  if (!canvas.hasAttribute('role')) canvas.setAttribute('role', 'img');
  if (!canvas.hasAttribute('aria-label')) {
    canvas.setAttribute('aria-label', options.label || DEFAULT_LABELS[state0]);
  }

  orb.resize(options.size == null ? 64 : options.size);
  orb.ramp = resolveRamp(canvas);

  live.add(orb);
  const io = ensureObserver();
  if (io) io.observe(canvas);
  schedule();

  const handle = {
    get state() { return orb.state; },
    setState(next) {
      if (!STATE_TO_MODE[next]) throw new Error(`unknown orb state "${next}"`);
      orb.state = next;
      if (!options.label) canvas.setAttribute('aria-label', DEFAULT_LABELS[next]);
      wake();
      return handle;
    },
    setSize(size) { orb.resize(size); wake(); return handle; },
    setSpeed(mul) { orb.speed = mul; return handle; },
    pause() { orb.paused = true; return handle; },
    resume() { orb.paused = false; wake(); return handle; },
    refresh: orb.refresh,
    destroy() {
      live.delete(orb);
      if (io) io.unobserve(canvas);
      delete canvas.__jtsOrb;
      ctx.clearRect(0, 0, orb.size, orb.size);
    }
  };
  return handle;
}
