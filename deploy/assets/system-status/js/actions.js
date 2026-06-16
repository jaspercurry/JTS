// actions.js — the page's mutating interactions: restart / reboot / power-off,
// the audio-conversion apply, and run-diagnostics. Each surfaces failure
// honestly (button label or status text, plus console.error) — no silent paths.

import { h } from "./dom.js";
import { jsonHeaders } from "./api.js";
import { postControlAction } from "/assets/shared/js/http.js";
import { updateAudioQuality } from "./sections.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";

const QUALITY_CONFIRM =
  "Change audio conversion quality? Music renderers will restart briefly.";

const BEST_QUALITY_CONFIRM =
  "Switch to Best audio conversion? Music renderers will restart briefly.\n\n" +
  "Best uses more CPU. On lower-powered hardware, especially with synced " +
  "AirPlay, it can cause packet drops or underruns. Medium is recommended " +
  "unless this hardware has been verified.";

function qualityConfirmMessage(converter) {
  return converter === "samplerate_best" ? BEST_QUALITY_CONFIRM : QUALITY_CONFIRM;
}

// confirm (one or two prompts) → POST → reflect Working…/Sent/Failed → restore.
// opts.statusEl + opts.sentMessage: on a successful POST, write a contextual
// note (e.g. "Rebooting — unreachable for ~60 s") into an aria-live region —
// the page is about to go away, so the button label alone isn't enough.
// opts.danger styles the confirm red + autofocuses Cancel (reboot / power off).
export async function postAction(path, btn, confirmLines, opts = {}) {
  for (const line of confirmLines) {
    if (!await jtsConfirm(line, { danger: opts.danger })) return;
  }
  if (!btn) return;
  const { statusEl, sentMessage } = opts;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Working…";
  if (statusEl) statusEl.textContent = "";
  try {
    // postControlAction attaches the opt-in X-JTS-Token and, on a 403
    // control_token_required, prompts once + retries — so reboot/power-off
    // work whether or not the gate is enabled. (Shared helper; no per-page
    // token plumbing.)
    const { ok, status, body } = await postControlAction(path);
    if (ok) {
      btn.textContent = "Sent";
      if (statusEl && sentMessage) statusEl.textContent = sentMessage;
    } else {
      console.error("system: action '" + path + "' failed", body);
      btn.textContent = "Failed: " + (body.error || status);
    }
  } catch (e) {
    console.error("system: action '" + path + "' failed", e);
    btn.textContent = "Failed: " + e.message;
  }
  setTimeout(() => { btn.disabled = false; btn.textContent = original; }, 3000);
}

export async function setQuality(refs, converter) {
  if (refs.systemCapabilities && refs.systemCapabilities.audio_quality === false) {
    refs.aq.status.textContent = "Audio conversion is managed by the leader on this install role.";
    return;
  }
  if (!await jtsConfirm(qualityConfirmMessage(converter))) return;
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
