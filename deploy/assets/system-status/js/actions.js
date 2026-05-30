// actions.js — the page's mutating interactions: restart / reboot / power-off,
// the audio-conversion apply, and run-diagnostics. Each surfaces failure
// honestly (button label or status text, plus console.error) — no silent paths.

import { h } from "./dom.js";
import { csrfHeaders, jsonHeaders } from "./api.js";
import { updateAudioQuality } from "./sections.js";

// confirm (one or two prompts) → POST → reflect Working…/Sent/Failed → restore.
// opts.statusEl + opts.sentMessage: on a successful POST, write a contextual
// note (e.g. "Rebooting — unreachable for ~60 s") into an aria-live region —
// the page is about to go away, so the button label alone isn't enough.
export async function postAction(path, btn, confirmLines, opts = {}) {
  for (const line of confirmLines) {
    if (!confirm(line)) return;
  }
  if (!btn) return;
  const { statusEl, sentMessage } = opts;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Working…";
  if (statusEl) statusEl.textContent = "";
  try {
    const r = await fetch(path, { method: "POST", headers: csrfHeaders() });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      btn.textContent = "Sent";
      if (statusEl && sentMessage) statusEl.textContent = sentMessage;
    } else {
      console.error("system: action '" + path + "' failed", body);
      btn.textContent = "Failed: " + (body.error || r.status);
    }
  } catch (e) {
    console.error("system: action '" + path + "' failed", e);
    btn.textContent = "Failed: " + e.message;
  }
  setTimeout(() => { btn.disabled = false; btn.textContent = original; }, 3000);
}

export async function setQuality(refs, converter) {
  if (!confirm("Change audio conversion quality? Music renderers will restart briefly.")) return;
  const aq = refs.aq;
  aq.buttons.forEach((b) => { b.el.disabled = true; b.el.dataset.applying = "1"; });
  aq.status.textContent = "Applying…";
  try {
    const r = await fetch("audio-quality", {
      method: "POST", headers: jsonHeaders(), body: JSON.stringify({ converter }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
    // Reflect the new active/pressed state immediately rather than waiting
    // for the next 5 s poll.
    if (body.audio_quality) updateAudioQuality(aq, body.audio_quality);
    aq.status.textContent = "Applied. Music renderers are restarting briefly.";
  } catch (e) {
    console.error("system: audio-quality apply failed", e);
    aq.status.textContent = "Failed: " + e.message;
  } finally {
    aq.buttons.forEach((b) => { delete b.el.dataset.applying; b.el.disabled = false; });
  }
}

function renderDiagnostics(out, body) {
  const mark = (s) => (s === "fail" ? "✗" : s === "warn" ? "!" : "✓");
  const tone = (s) => (s === "fail" ? "danger" : s === "warn" ? "warn" : "ok");
  const rows = (body.results || []).map((c) =>
    h("tr", null,
      h("td.diag-mark", { style: { color: "var(--status-" + tone(c.status) + ")" } }, mark(c.status)),
      h("td", null, c.name),
      h("td.muted", null, c.detail || "")));
  out.replaceChildren(
    h("div.table-wrap", null, h("table.table.table--diag", null, h("tbody", null, ...rows))),
    h("p.info-card__note", null, body.fails + " failed, " + body.warns + " warning(s)."));
}

export async function runDiagnostics(btn, out) {
  btn.disabled = true;
  out.style.display = "block";
  out.replaceChildren(h("span.muted", null, "Running jasper-doctor…"));
  try {
    const r = await fetch("diagnostics.json", { cache: "no-store" });
    const body = await r.json();
    if (body.error) out.replaceChildren(h("span.muted", null, "Error: " + body.error));
    else renderDiagnostics(out, body);
  } catch (e) {
    console.error("system: diagnostics failed", e);
    out.replaceChildren(h("span.muted", null, "Failed: " + e.message));
  }
  btn.disabled = false;
}

// Download a logs + redacted-config bundle (control runs pi-bundle.sh and
// streams the tarball). Fetch-then-blob (not a plain <a download>) so a
// 409/502 surfaces as a friendly message instead of a browser error page.
export async function downloadDiagnostics(btn, statusEl) {
  if (!confirm(
    "Gather a diagnostics bundle (logs + config, secrets redacted)? " +
    "This does heavy I/O on the speaker and may briefly affect audio."
  )) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Bundling…";
  statusEl.textContent = "";
  try {
    const r = await fetch("/diagnostics-bundle", { cache: "no-store" });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.error || "HTTP " + r.status);
    }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    const name = m ? m[1] : "jasper-bundle.tar.gz";
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: name });
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    btn.textContent = "Downloaded";
    statusEl.textContent = "Saved " + name + ".";
  } catch (e) {
    console.error("system: diagnostics bundle failed", e);
    btn.textContent = "Failed";
    statusEl.textContent = "Failed: " + e.message;
  } finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = original; }, 3000);
  }
}
