// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The return URL is display/navigation data from the Pi-supplied capture_spec.
// Treat it as untrusted even though the Pi validates it before registration:
// the relay is deliberately opaque, and the capture page is the final safety
// boundary before creating a clickable link.

export function safeReturnUrl(spec) {
  const raw = spec && typeof spec.return_url === "string" ? spec.return_url.trim() : "";
  if (!raw || /[\u0000-\u001F\u007F]/.test(raw)) return "";
  let url;
  try {
    url = new URL(raw);
  } catch {
    return "";
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return "";
  if (url.username || url.password || url.hash) return "";
  return url.href;
}
