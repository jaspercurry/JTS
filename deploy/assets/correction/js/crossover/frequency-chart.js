// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Canvas geometry shared by the live crossover chart and saved measurements.

const GRID_FREQS_HZ = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];

export function cssColor(canvas, name, fallback) {
  const value = getComputedStyle(canvas).getPropertyValue(name).trim();
  return value || fallback;
}

function curvePoints(series, loHz, hiHz) {
  const curve = series && series.curve;
  const deviations = curve && curve.display && curve.display.deviation_db;
  if (!curve || !Array.isArray(curve.freqs_hz) || !Array.isArray(deviations)) return [];
  const points = [];
  const length = Math.min(curve.freqs_hz.length, deviations.length);
  for (let index = 0; index < length; index += 1) {
    const frequency = Number(curve.freqs_hz[index]);
    if (!Number.isFinite(frequency)) continue;
    if (frequency < loHz || frequency > hiHz) continue;
    points.push({ frequency, deviation: deviations[index] });
  }
  return points;
}

function dbDomain(pointSets, series, domainRangeHz, corridorBands, padDb, minSpanDb) {
  const [domainLo, domainHi] = domainRangeHz || [null, null];
  let bound = 0;
  for (const band of corridorBands) {
    const tolerance = Number(band && band.tolerance_db);
    if (Number.isFinite(tolerance)) bound = Math.max(bound, Math.abs(tolerance));
  }
  for (const [index, points] of pointSets.entries()) {
    const exclusions = series[index].curve?.display?.untrusted_intervals_hz || [];
    for (const point of points) {
      if (domainLo !== null && point.frequency < domainLo) continue;
      if (domainHi !== null && point.frequency > domainHi) continue;
      if (exclusions.some(([lo, hi]) => point.frequency >= lo && point.frequency <= hi)) continue;
      bound = Math.max(bound, Math.abs(point.deviation));
    }
  }
  bound = Math.ceil(Math.max(minSpanDb / 2, bound + padDb));
  return [-bound, bound];
}

export function drawFrequencyChart(canvas, payload) {
  const series = (payload && payload.series) || [];
  const frequencyRangeHz = (payload && payload.frequencyRangeHz) || [20, 20000];
  const loHz = Number(frequencyRangeHz[0]);
  const hiHz = Number(frequencyRangeHz[1]);
  const pointSets = series.map((item) => item.draw === false ? [] : curvePoints(item, loHz, hiHz));

  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return false;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const context = canvas.getContext('2d');
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.scale(dpr, dpr);
  context.clearRect(0, 0, rect.width, rect.height);
  if (!pointSets.some((points) => points.some((point) => point.deviation != null))) return false;

  const margins = { left: 44, right: 10, top: 10, bottom: 22 };
  const width = rect.width - margins.left - margins.right;
  const height = rect.height - margins.top - margins.bottom;
  const corridorBands = (payload && payload.corridorBands) || [];
  const [dbMin, dbMax] = dbDomain(
    pointSets,
    series,
    payload && payload.domainRangeHz,
    corridorBands,
    Number(payload && payload.padDb) || 3,
    Number(payload && payload.minSpanDb) || 0,
  );

  const x = (frequency) => margins.left + width * (
    Math.log2(frequency / loHz) / Math.log2(hiHz / loHz)
  );
  const y = (db) => margins.top + height * (1 - (db - dbMin) / (dbMax - dbMin));
  const clampX = (frequency) => Math.max(margins.left, Math.min(margins.left + width, x(frequency)));
  const theme = (payload && payload.theme) || {};

  context.strokeStyle = theme.grid || '#ccc';
  context.fillStyle = theme.text || '#888';
  context.font = '11px sans-serif';
  context.lineWidth = 1;
  let labelRight = -Infinity;
  for (const frequency of GRID_FREQS_HZ) {
    if (frequency < loHz || frequency > hiHz) continue;
    const gridX = x(frequency);
    context.beginPath();
    context.moveTo(gridX, margins.top);
    context.lineTo(gridX, margins.top + height);
    context.stroke();
    const label = frequency >= 1000 ? `${frequency / 1000}k` : `${frequency}`;
    const labelWidth = context.measureText(label).width;
    const labelX = Math.min(rect.width - labelWidth, gridX - labelWidth / 2);
    if (labelX >= labelRight + 6) {
      context.fillText(label, labelX, margins.top + height + 14);
      labelRight = labelX + labelWidth;
    }
  }
  const step = dbMax <= 5 ? 1 : Math.ceil(dbMax / 5);
  for (let db = Math.ceil(dbMin / step) * step; db <= dbMax; db += step) {
    const gridY = y(db);
    context.beginPath();
    context.moveTo(margins.left, gridY);
    context.lineTo(margins.left + width, gridY);
    context.stroke();
    context.fillText(`${db} dB`, 2, gridY + 3);
  }
  context.strokeStyle = theme.text || '#888';
  context.beginPath();
  context.moveTo(margins.left, y(0));
  context.lineTo(margins.left + width, y(0));
  context.stroke();

  context.save();
  context.beginPath();
  context.rect(margins.left, margins.top, width, height);
  context.clip();

  context.save();
  context.globalAlpha = 0.16;
  context.fillStyle = theme.excluded || '#888';
  context.beginPath();
  for (const { curve, draw } of series) {
    if (draw === false) continue;
    for (const [lo, hi] of (curve && curve.display && curve.display.untrusted_intervals_hz) || []) {
      if (hi < loHz || lo > hiHz) continue;
      const x0 = clampX(lo);
      const x1 = clampX(hi);
      context.rect(x0, margins.top, Math.max(1, x1 - x0), height);
    }
  }
  context.fill();
  context.restore();

  context.save();
  context.globalAlpha = 0.16;
  context.fillStyle = theme.corridor || '#4a90a4';
  for (const band of corridorBands) {
    const lo = Number(band && band.f_lo_hz);
    const hi = Number(band && band.f_hi_hz);
    const tolerance = Number(band && band.tolerance_db);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || !Number.isFinite(tolerance)) continue;
    const x0 = clampX(lo);
    const x1 = clampX(hi);
    if (x1 > x0) context.fillRect(x0, y(tolerance), x1 - x0, y(-tolerance) - y(tolerance));
  }
  context.restore();

  pointSets.forEach((points, index) => {
    if (!points.some((point) => point.deviation != null)) return;
    const style = series[index];
    context.strokeStyle = style.color;
    context.lineWidth = style.lineWidth || 2;
    context.globalAlpha = style.alpha == null ? 1 : style.alpha;
    context.setLineDash(style.dash || []);
    context.beginPath();
    let start = true;
    points.forEach((point) => {
      if (point.deviation == null) { start = true; return; }
      const method = start ? 'moveTo' : 'lineTo';
      context[method](x(point.frequency), y(point.deviation));
      start = false;
    });
    context.stroke();
  });
  context.setLineDash([]);
  context.globalAlpha = 1;
  context.restore();
  return true;
}
