// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Live before/after adapter over the shared frequency-response canvas.

import { cssColor, drawFrequencyChart } from './frequency-chart.js';

const PREDICTED_DASH = [6, 4];

function gradedFrequencyRange(specBands) {
  let lo = null;
  let hi = null;
  for (const band of specBands || []) {
    const bandLo = Number(band && band.f_lo_hz);
    const bandHi = Number(band && band.f_hi_hz);
    if (Number.isFinite(bandLo)) lo = lo === null ? bandLo : Math.min(lo, bandLo);
    if (Number.isFinite(bandHi)) hi = hi === null ? bandHi : Math.max(hi, bandHi);
  }
  return [lo, hi];
}

export function drawCloudChart(canvas, payload) {
  const specBands = (payload && payload.specBands) || [];
  const series = [
    {
      curve: payload && payload.measureCurve,
      referenceDb: payload && payload.measureReferenceDb,
      color: cssColor(canvas, '--crossover-chart-measure', '#c0392b'),
    },
    {
      curve: payload && payload.verifyCurve,
      referenceDb: payload && payload.verifyReferenceDb,
      color: cssColor(canvas, '--crossover-chart-verify', '#2e8b57'),
    },
    {
      curve: payload && payload.predictedCurve,
      referenceDb: payload && payload.predictedReferenceDb,
      color: cssColor(canvas, '--crossover-chart-predicted', '#d08b25'),
      dash: PREDICTED_DASH,
    },
  ];
  return drawFrequencyChart(canvas, {
    series,
    frequencyRangeHz: [20, 20000],
    domainRangeHz: gradedFrequencyRange(specBands),
    corridorBands: specBands,
    excludedIntervals: (payload && payload.excludedIntervals) || [],
    padDb: 3,
    theme: {
      grid: cssColor(canvas, '--border-strong', '#ccc'),
      text: cssColor(canvas, '--muted', '#888'),
      corridor: cssColor(canvas, '--crossover-chart-corridor', '#4a90a4'),
      excluded: cssColor(canvas, '--crossover-chart-excluded', '#888'),
    },
  });
}
