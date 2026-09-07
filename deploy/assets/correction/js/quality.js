// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// quality.js — the capture-quality banner: which reports a /status payload
// carries, and the one banner they render into.

export var qualityBanner = document.getElementById('quality-banner');

function qualityReports(payload) {
  var reports = [];
  if (payload && Array.isArray(payload.capture_quality)) {
    reports = reports.concat(payload.capture_quality);
  }
  if (payload && payload.verify_quality) {
    reports.push(payload.verify_quality);
  }
  return reports;
}

export function renderQuality(payload) {
  var seen = {};
  var issues = [];
  qualityReports(payload).forEach(function (report) {
    (report && report.issues || []).forEach(function (issue) {
      var key = [issue.severity, issue.code].join('|');
      if (!seen[key]) {
        seen[key] = true;
        issues.push(issue);
      }
    });
  });
  if (!issues.length) {
    qualityBanner.className = 'quality-banner';
    qualityBanner.hidden = true;
    qualityBanner.innerHTML = '';
    return;
  }
  var hasFail = issues.some(function (issue) {
    return issue.severity === 'fail';
  });
  qualityBanner.className = 'quality-banner ' + (hasFail ? 'fail' : 'warn');
  qualityBanner.hidden = false;
  qualityBanner.innerHTML =
    '<strong>' + (hasFail ? 'Measurement blocked:' : 'Measurement quality warnings:') +
    '</strong><p>' + (hasFail
      ? 'This capture could not be used safely. Try this position again.'
      : 'A quieter re-measure may improve confidence, but you can continue.') +
    '</p>';
}
