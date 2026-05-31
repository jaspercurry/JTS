// main.js — /google/ wizard behaviour.
//
// The Google OAuth wizard is server-rendered request/response (forms POST
// to ./setup-credentials, ./start, ./remove, ./default, ./reset-credentials;
// GET /callback finishes OAuth). This module carries only the page's
// progressive-enhancement behaviour; with JS off the forms still submit and
// the page still works (you just lose the localStorage step tracker, the
// copy buttons, the Client-ID reveal, and the pre-submit confirms).
//
// Everything binds via delegated listeners on escaped data-* hooks — no
// inline handlers, no untrusted strings baked into JS. Confirms use the
// shared <dialog> helper (never window.confirm, which the browser can
// suppress — see /assets/shared/js/dialog.js).

import { jtsConfirm } from "/assets/shared/js/dialog.js";

// ---------------------------------------------------------------------------
// Destructive-action submit guard.
//
// Any <form data-confirm="…"> (reset-credentials, remove-account) is
// intercepted: we confirm via the shared dialog first, then let the native
// POST proceed on OK. data-confirm-danger reddens the confirm button.
// ---------------------------------------------------------------------------
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const message = form.dataset.confirm;
  if (!message) return; // not a guarded form — let it submit normally
  if (form.dataset.confirmed === "1") return; // second pass after OK

  event.preventDefault();
  const danger = form.hasAttribute("data-confirm-danger");
  const ok = await jtsConfirm(message, { danger });
  if (ok) {
    form.dataset.confirmed = "1";
    form.submit();
  }
});

// ---------------------------------------------------------------------------
// Delegated click handlers: copy-to-clipboard, Client-ID reveal,
// reset-progress, and select-on-click inputs.
// ---------------------------------------------------------------------------
document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;

  // Readonly inputs that select their contents when clicked (the redirect
  // URL fields). Replaces the old inline onclick="this.select()".
  if (target instanceof HTMLInputElement && target.hasAttribute("data-select-on-click")) {
    target.select();
    return;
  }

  // Copy-to-clipboard buttons. data-copy = id of the input to copy;
  // data-copy-feedback = id of the "Copied!" span to flash.
  const copyBtn = target.closest("[data-copy]");
  if (copyBtn) {
    const input = document.getElementById(copyBtn.dataset.copy);
    const feedback = document.getElementById(copyBtn.dataset.copyFeedback);
    if (input) {
      try {
        await navigator.clipboard.writeText(input.value);
      } catch (e) {
        // Older/locked-down browsers: fall back to execCommand.
        input.select();
        document.execCommand("copy");
      }
      if (feedback) {
        feedback.classList.add("shown");
        setTimeout(() => feedback.classList.remove("shown"), 1800);
      }
    }
    return;
  }

  // "Show full" Client ID. The full value rides in data-full (escaped
  // server-side as an HTML attribute) so the cached module bakes in no
  // secret and the value is never interpolated into JS.
  const revealBtn = target.closest('[data-action="reveal-client-id"]');
  if (revealBtn) {
    const display = document.getElementById("client-id-display");
    if (display) display.textContent = revealBtn.dataset.full || "";
    revealBtn.style.display = "none";
    return;
  }

  // Reset-progress: forget which setup steps were marked done.
  const resetBtn = target.closest('[data-action="reset-progress"]');
  if (resetBtn) {
    const ok = await jtsConfirm(
      "Forget which steps you marked done? The form at the bottom still works either way."
    );
    if (ok) {
      try {
        localStorage.removeItem(STORAGE_KEY);
      } catch (e) {
        /* private mode / quota — ignore */
      }
      location.reload();
    }
    return;
  }

  // "I've done this →" — mark a setup step done.
  const markBtn = target.closest("button.mark-done");
  if (markBtn) {
    event.preventDefault();
    const stepEl = markBtn.closest("li.wizard-step");
    if (stepEl) markDone(stepEl);
  }
});

// ---------------------------------------------------------------------------
// Setup-wizard progress tracking (state 1 only — the read-only "View setup
// guide" copy in state 3 has no .wizard-step elements with this script's
// hooks active, so init() is a no-op there).
//
// Each step has a "mark done" button that adds its step number to a JSON
// array in localStorage; on load we collapse done steps and auto-open the
// first not-done one. The browser's native <details> toggle still works
// after init (we only set state once).
// ---------------------------------------------------------------------------
const STORAGE_KEY = "jts.google.wizard.done";

function loadDone() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch (e) {
    return [];
  }
}

function saveDone(arr) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
  } catch (e) {
    /* private mode / quota — silent */
  }
}

function initWizard() {
  const done = loadDone();
  let firstNotDoneOpened = false;
  document.querySelectorAll("li.wizard-step").forEach((el) => {
    const step = el.dataset.step;
    const details = el.querySelector("details");
    if (!details) return;
    const isDone = done.indexOf(step) !== -1;
    if (isDone) {
      el.classList.add("done");
      details.removeAttribute("open");
    } else if (!firstNotDoneOpened) {
      firstNotDoneOpened = true;
      el.classList.add("active");
      details.setAttribute("open", "");
    }
  });
}

function markDone(stepEl) {
  const step = stepEl.dataset.step;
  const done = loadDone();
  if (done.indexOf(step) === -1) done.push(step);
  saveDone(done);
  stepEl.classList.add("done");
  stepEl.classList.remove("active");
  const details = stepEl.querySelector("details");
  if (details) details.removeAttribute("open");
  // Open the next not-done sibling (skip already-done ones).
  let next = stepEl.nextElementSibling;
  while (next && next.classList.contains("done")) {
    next = next.nextElementSibling;
  }
  if (next) {
    next.classList.add("active");
    const nDetails = next.querySelector("details");
    if (nDetails) nDetails.setAttribute("open", "");
    setTimeout(() => {
      next.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  }
}

// Modules are deferred by default, so the DOM is ready when this runs; but
// only the active state-1 wizard binds the localStorage state (the read-only
// guide inside state 3 has no "active" highlighting because its steps share
// the markup but were rendered read-only). initWizard() keys off
// li.wizard-step, which is present in both — that's fine, the read-only copy
// just gets the same collapse/expand affordance.
initWizard();
