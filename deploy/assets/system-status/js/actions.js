// actions.js — the page's mutating interactions: restart / reboot / power-off,
// the audio-conversion apply, and run-diagnostics. Each surfaces failure
// honestly (button label or status text, plus console.error) — no silent paths.

import { h } from "./dom.js";
import { csrfHeaders, jsonHeaders } from "./api.js";
import { updateAudioQuality } from "./sections.js";

// confirm (one or two prompts) → POST → reflect Working…/Sent/Failed → restore.
export async function postAction(path, btn, confirmLines) {
  for (const line of confirmLines) {
    if (!confirm(line)) return;
  }
  if (!btn) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Working…";
  try {
    const r = await fetch(path, { method: "POST", headers: csrfHeaders() });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) console.error("system: action '" + path + "' failed", body);
    btn.textContent = r.ok ? "Sent" : "Failed: " + (body.error || r.status);
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
