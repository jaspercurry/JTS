// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Shared browser-side normalization for links to another JTS speaker's
// management surface. Raw IPs are intentionally rejected: pair/directory links
// should use the stable mDNS name, not whichever address discovery last saw.
const HOST_RE = /^[A-Za-z0-9][A-Za-z0-9.-]{0,253}$/;
const IPV4_RE = /^(?:\d{1,3}\.){3}\d{1,3}$/;

export function localWebHost(value) {
  const host = String(value || "").trim().replace(/\.$/, "");
  if (!host || !HOST_RE.test(host) || IPV4_RE.test(host)) return "";
  return host.endsWith(".local") ? host : `${host}.local`;
}
