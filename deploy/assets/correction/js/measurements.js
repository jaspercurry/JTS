// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { h, svg } from '/assets/shared/js/dom.js';
import { getJSON } from '/assets/shared/js/http.js';
import { cssColor, drawFrequencyChart } from './crossover/frequency-chart.js';

const els = {
  runA: document.getElementById('measurement-run-a'),
  runB: document.getElementById('measurement-run-b'),
  canvas: document.getElementById('measurement-chart'),
  status: document.getElementById('measurement-chart-status'),
  series: document.getElementById('measurement-series'),
  metadata: document.getElementById('measurement-metadata'),
};

const POSITION_DASHES = [
  [2, 3],
  [7, 3],
  [7, 3, 2, 3],
  [12, 3],
  [12, 3, 2, 3],
  [12, 3, 2, 3, 2, 3],
];

let currentView = null;
let visibleSeries = new Set();
let resizeTimer = null;

function seriesKey(run, series) {
  return `${run.slot}:${series.id}`;
}

function formatDate(value) {
  if (value == null || value === '') return 'Unknown time';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown time' : date.toLocaleString();
}

function optionLabel(run) {
  return `${formatDate(run.started_at)} · ${run.origin} ${run.name}`;
}

function fillRunPicker(select, catalog, selected, optional) {
  const options = optional ? [h('option', { value: '' }, 'None')] : [];
  for (const run of catalog) {
    options.push(h('option', { value: run.id }, optionLabel(run)));
  }
  select.replaceChildren(...options);
  select.value = selected || '';
}

function dataUrl(runA, runB) {
  const query = new URLSearchParams();
  if (runA) query.set('a', runA);
  if (runB) query.set('b', runB);
  const suffix = query.toString();
  return suffix ? `data?${suffix}` : 'data';
}

function angleLabel(value) {
  if (typeof value !== 'number') return '';
  return value === 0 ? '0°' : `${value > 0 ? '+' : ''}${value}°`;
}

function runColor(run) {
  return run.slot === 'a'
    ? cssColor(els.canvas, '--measurement-run-a', '#4f5965')
    : cssColor(els.canvas, '--measurement-run-b', '#356c91');
}

function seriesDash(series, index, allSeries) {
  if (series.kind === 'average') return [];
  if (series.kind === 'entry_baseline') return [10, 4];
  const detailIndex = allSeries.slice(0, index)
    .filter((candidate) => !['average', 'entry_baseline'].includes(candidate.kind)).length;
  return POSITION_DASHES[detailIndex % POSITION_DASHES.length];
}

function seriesSwatch(run, series, index) {
  const dash = seriesDash(series, index, run.series);
  return svg('svg.measurement-series__swatch', {
    viewBox: '0 0 24 4',
    'aria-hidden': 'true',
  }, svg('line', {
    x1: 0, y1: 2, x2: 24, y2: 2,
    stroke: runColor(run),
    'stroke-width': 2,
    'stroke-dasharray': dash.join(' '),
  }));
}

function addInterval(intervals, raw) {
  const lo = Number(Array.isArray(raw) ? raw[0] : raw && raw.f_lo_hz);
  const hi = Number(Array.isArray(raw) ? raw[1] : raw && raw.f_hi_hz);
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi < lo) return;
  const clippedLo = Math.max(20, lo);
  const clippedHi = Math.min(20000, hi);
  if (clippedHi >= clippedLo) intervals.push([clippedLo, clippedHi]);
}

function chartExclusions() {
  const intervals = [];
  for (const run of currentView.runs) {
    const metadata = run.metadata || {};
    const trustedFloor = Number(metadata.trusted_floor_hz);
    if (Number.isFinite(trustedFloor) && trustedFloor > 20) {
      addInterval(intervals, [20, trustedFloor]);
    }
    for (const band of metadata.excluded_bands_hz || []) addInterval(intervals, band);
    run.series.forEach((series) => {
      if (!visibleSeries.has(seriesKey(run, series))) return;
      const validityFloor = Number(series.validity_floor_hz);
      if (Number.isFinite(validityFloor) && validityFloor > 20) {
        addInterval(intervals, [20, validityFloor]);
      }
      for (const band of series.excluded_intervals_hz || []) addInterval(intervals, band);
    });
  }
  intervals.sort((left, right) => left[0] - right[0] || left[1] - right[1]);
  const merged = [];
  for (const interval of intervals) {
    const last = merged[merged.length - 1];
    if (last && interval[0] <= last[1]) last[1] = Math.max(last[1], interval[1]);
    else merged.push(interval.slice());
  }
  return merged.map(([lo, hi]) => ({ f_lo_hz: lo, f_hi_hz: hi }));
}

function draw() {
  if (!currentView) return;
  const chartSeries = [];
  const floors = [];
  for (const run of currentView.runs) {
    const storedFloor = run.metadata && run.metadata.trusted_floor_hz;
    const floor = Number(storedFloor);
    if (storedFloor != null && Number.isFinite(floor)) floors.push(floor);
    run.series.forEach((series, index) => {
      chartSeries.push({
        curve: series,
        referenceDb: series.reference_db,
        color: runColor(run),
        lineWidth: series.kind === 'average' ? 2.5 : 1.25,
        alpha: series.kind === 'average' ? 1 : 0.55,
        dash: seriesDash(series, index, run.series),
        draw: visibleSeries.has(seriesKey(run, series)),
      });
    });
  }
  const visibleCount = chartSeries.filter((series) => series.draw).length;
  const exclusions = chartExclusions();
  const drew = drawFrequencyChart(els.canvas, {
    series: chartSeries,
    frequencyRangeHz: [20, 20000],
    domainRangeHz: [floors.length ? Math.min(...floors) : 20, 20000],
    minSpanDb: 30,
    excludedIntervals: exclusions,
    theme: {
      grid: cssColor(els.canvas, '--border-strong', '#ccc'),
      text: cssColor(els.canvas, '--muted', '#888'),
      excluded: cssColor(els.canvas, '--crossover-chart-excluded', '#888'),
    },
  });
  els.status.textContent = !drew
    ? 'No plottable response curves are stored for this selection.'
    : `${visibleCount} of ${chartSeries.length} curves shown · relative to the stored reference frame${
      exclusions.length ? ' · shaded areas are untrusted' : ''
    }`;
}

function renderSeriesControls() {
  const groups = currentView.runs.map((run) => {
    const controls = run.series.map((series, index) => {
      const key = seriesKey(run, series);
      const input = h('input', {
        type: 'checkbox',
        checked: visibleSeries.has(key),
        onchange: (event) => {
          if (event.currentTarget.checked) visibleSeries.add(key);
          else visibleSeries.delete(key);
          draw();
        },
      });
      const smoothing = series.smoothing_fractional_octave
        ? `1/${series.smoothing_fractional_octave} octave`
        : 'stored curve';
      return h('label.measurement-series__item', null,
        input,
        seriesSwatch(run, series, index),
        h('span', null, series.label),
        h('small', null, smoothing),
      );
    });
    return h('fieldset.measurement-series__group', null,
      h('legend', null, run.label),
      controls,
    );
  });
  els.series.replaceChildren(...groups);
}

function detailRows(run) {
  const metadata = run.metadata || {};
  const smoothing = metadata.smoothing || {};
  const angles = (metadata.angles_deg || []).map(angleLabel).join(', ') || 'Not recorded';
  const result = (metadata.adoption && metadata.adoption.outcome)
    || (metadata.verification && metadata.verification.spec)
    || run.state || 'Unknown';
  const storedFloor = metadata.trusted_floor_hz;
  const floor = Number(storedFloor);
  const graph = metadata.applied_graph_fingerprint || metadata.entry_graph_fingerprint
    || (metadata.graph_fingerprints || [])[0];
  const smoothingText = smoothing.average_fractional_octave
    ? `Average 1/${smoothing.average_fractional_octave} · positions 1/${smoothing.positions_fractional_octave || '?'}`
    : 'Stored with each curve';
  const phases = (metadata.phases || []).map((phase) => String(phase).replaceAll('_', ' ')).join(', ');
  return [
    ['Captured', formatDate(run.started_at)],
    ['Result', String(result)],
    ['Type', String(run.measurement_family || 'speaker response').replaceAll('_', ' ')],
    ['Positions', String(metadata.position_count || 0)],
    ['Angles', angles],
    ['Phases', phases || 'Not recorded'],
    ['Smoothing', smoothingText],
    ['Trusted range', storedFloor != null && Number.isFinite(floor) ? `${Math.round(floor)} Hz–20 kHz` : 'Not recorded'],
    ['Graph', graph ? String(graph).slice(0, 12) : 'Not recorded'],
    ['Mic calibration', metadata.mic_calibration_id || 'Not recorded'],
  ];
}

function renderMetadata() {
  const cards = currentView.runs.map((run) => {
    const rows = detailRows(run).flatMap(([name, value]) => [
      h('dt', null, name), h('dd', null, value),
    ]);
    return h('section.info-card', null,
      h('p.eyebrow', null, run.label),
      h('h2.section__title', null, formatDate(run.started_at)),
      h('dl.deflist', null, rows),
    );
  });
  els.metadata.replaceChildren(...cards);
}

function clearView(message) {
  currentView = null;
  visibleSeries = new Set();
  els.series.replaceChildren();
  els.metadata.replaceChildren();
  drawFrequencyChart(els.canvas, { series: [] });
  els.status.textContent = message;
}

function render(payload) {
  fillRunPicker(els.runA, payload.catalog, payload.selected.a, false);
  fillRunPicker(els.runB, payload.catalog, payload.selected.b, true);
  currentView = payload.view;
  visibleSeries = new Set();
  if (!currentView) {
    clearView('No saved speaker measurements are available yet.');
    return;
  }
  for (const run of currentView.runs) {
    for (const series of run.series) {
      if (series.visible_by_default) visibleSeries.add(seriesKey(run, series));
    }
  }
  renderSeriesControls();
  renderMetadata();
  draw();
}

async function load(runA = '', runB = '') {
  els.status.textContent = 'Loading measurements…';
  try {
    render(await getJSON(dataUrl(runA, runB)));
  } catch (error) {
    clearView(error && error.message
      ? error.message : 'The saved measurements could not be loaded.');
  }
}

els.runA.addEventListener('change', () => load(els.runA.value, els.runB.value));
els.runB.addEventListener('change', () => load(els.runA.value, els.runB.value));
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(draw, 120);
});

load();
