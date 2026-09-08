// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// report.js — the two read-only report surfaces: the browser audio-path
// card and the measurement evidence report. Both render into markup the
// server authored; neither owns page state.
import { escapeHtml as escapeText } from "/assets/shared/js/escape.js";
import { formatMaybeDb, reportIssueList } from "./format.js";

var browserAudioReport = document.getElementById('browser-audio-report');
// main.js still owns this card's fetch/lifecycle half (loading, delete,
// failure copy), so both modules address the node the server rendered.
var sessionReport = document.getElementById('session-report');

export function renderBrowserAudioLocal(actual, problems) {
  var issues = problems.slice();
  if (actual.channelCount !== 1) {
    issues.push('channelCount is ' + actual.channelCount + ' (JTS will use the first channel)');
  }
  var level = problems.length ? 'fail' : (issues.length ? 'warn' : 'ok');
  browserAudioReport.className = 'browser-audio-card ' + level;
  browserAudioReport.hidden = false;
  browserAudioReport.innerHTML =
    '<strong>Browser audio path: ' +
    (level === 'ok' ? 'ready' : (level === 'fail' ? 'blocked' : 'usable with warnings')) +
    '</strong>' +
    reportIssueList(
      issues,
      'Input metadata looks ready for measurement. Capture quality is still checked after each sweep.'
    );
}

export function renderBrowserAudioReport(report) {
  if (!report) return;
  var level = report.level || (report.failed ? 'fail' : 'warn');
  browserAudioReport.className = 'browser-audio-card ' + level;
  browserAudioReport.hidden = false;
  browserAudioReport.innerHTML =
    '<strong>Browser audio path: ' +
    escapeText(level === 'ok' ? 'ready' : (level === 'fail' ? 'blocked' : 'usable with warnings')) +
    '</strong><p class="hint">' + (level === 'ok'
      ? 'The microphone settings are ready for measurement.'
      : (level === 'fail'
        ? 'The microphone settings are not safe for this measurement.'
        : 'The microphone may reduce measurement accuracy.')) + '</p>';
}

export function renderSessionReport(payload) {
  var evidence = payload.evidence || {};
  var readiness = evidence.agent_readiness || {};
  var bundle = evidence.bundle || {};
  var measurement = evidence.measurement || {};
  var confidence = evidence.confidence || {};
  var acoustic = (evidence.acoustic_quality || {}).summary || {};
  var runtime = (evidence.runtime_integrity || {}).summary || {};
  var position = evidence.position_analysis || {};
  var repeatability = evidence.repeatability || {};
  var versions = payload.artifact_versions || {};
  var readinessLevel = readiness.level || 'caution';
  var suspicious = []
    .concat(bundle.issues || [])
    .concat(((evidence.runtime_integrity || {}).issues || []))
    .concat(((evidence.acoustic_quality || {}).issues || []))
    .concat(repeatability.issues || [])
    .concat(position.feature_flags || []);
  var trusted = [];
  if (bundle.has_result) trusted.push({message: 'Analysis result is present.'});
  if (bundle.has_artifact_manifest) trusted.push({message: 'Artifact manifest is present.'});
  if (acoustic.snr_level && acoustic.snr_level !== 'unavailable') {
    trusted.push({message: 'SNR evidence is ' + acoustic.snr_level + '.'});
  }
  if (runtime.level === 'ok') trusted.push({message: 'Runtime integrity is OK.'});
  if (repeatability.available) {
    trusted.push({message: 'Same-seat repeatability is ' + repeatability.level + '.'});
  }
  var gates = confidence.strategy_gates || {};
  var refused = ['safe', 'balanced', 'assertive'].filter(function (name) {
    return gates[name] && gates[name].allowed === false;
  }).map(function (name) {
    var reason = (gates[name].reasons || [])[0] || 'strategy gate blocked';
    return {message: name + ' correction blocked: ' + reason};
  });
  sessionReport.className = 'session-report ' + readinessLevel;
  sessionReport.innerHTML =
    '<h3>Measurement report · ' + escapeText(evidence.session_id || payload.session_id || 'unknown') + '</h3>' +
    '<p class="hint"><strong>Recommended next action:</strong> ' +
    escapeText(readiness.recommended_action || 'review evidence before applying stronger correction') + '</p>' +
    '<div class="metric-grid">' +
      '<div class="metric"><span class="label">Readiness</span><span class="value">' +
      escapeText(readinessLevel) + '</span></div>' +
      '<div class="metric"><span class="label">Confidence</span><span class="value">' +
      escapeText(confidence.level || '—') + ' · ' + Number(confidence.score || 0).toFixed(0) + '/100</span></div>' +
      '<div class="metric"><span class="label">SNR</span><span class="value">' +
      escapeText(acoustic.snr_level || '—') + ' · ' + formatMaybeDb(acoustic.min_estimated_snr_db) + '</span></div>' +
      '<div class="metric"><span class="label">Runtime</span><span class="value">' +
      escapeText(runtime.level || 'unknown') + '</span></div>' +
      '<div class="metric"><span class="label">Positions</span><span class="value">' +
      Number(position.position_count || measurement.positions_completed || 0) + '</span></div>' +
      '<div class="metric"><span class="label">Repeatability</span><span class="value">' +
      escapeText(repeatability.level || 'unavailable') + '</span></div>' +
    '</div>' +
    '<h4>What happened</h4>' +
    '<p class="hint">State ' + escapeText(bundle.state || 'unknown') +
    ' · target ' + escapeText(measurement.target_choice || 'unknown') +
    ' · strategy ' + escapeText(measurement.strategy_choice || 'unknown') +
    ' · bundle schema v' + escapeText(bundle.schema_version || 'unknown') + '.</p>' +
    '<h4>What looks trustworthy</h4>' +
    reportIssueList(trusted, 'No positive evidence was available yet.') +
    '<h4>What looks suspicious or missing</h4>' +
    reportIssueList(suspicious.concat((readiness.reasons || []).map(function (reason) {
      return {message: reason};
    })), 'No warnings were recorded in the read-only evidence packet.') +
    '<h4>What JTS refused to correct</h4>' +
    reportIssueList(refused, 'No strategy gate refusal was recorded.') +
    '<h4>Artifact versions</h4>' +
    '<p class="hint">bundle v' + escapeText(versions.bundle_schema_version || bundle.schema_version || 'unknown') +
    ' · manifest v' + escapeText(versions.artifact_manifest_schema_version || 'missing') +
    ' · result v' + escapeText(versions.result_json_schema_version || 'missing') +
    ' · runtime v' + escapeText(versions.runtime_integrity_schema_version || 'missing') +
    ' · acoustic v' + escapeText(versions.acoustic_quality_schema_version || 'missing') +
    ' · evidence packet v' + escapeText(versions.evidence_packet_schema_version || evidence.artifact_schema_version || 'unknown') +
    '.</p>';
}
