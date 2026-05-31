// main.js — /ha/ (Home Assistant connection wizard) page behaviour.
//
// The page is server-rendered in one of three states (none / partial /
// connected) by jasper/web/home_assistant_setup.py. This module only adds
// the client-side behaviour that used to live in inline <script> blocks:
//
//   state 1 (none)      — the "Find Home Assistant" mDNS scan + click-to-fill
//                         on result rows and recent-URL chips.
//   state 3 (connected) — the "Test connection" button, the agent-picker
//                         populate-on-load, the post-save restart-poll chip,
//                         and the two voice-pack copy buttons.
//
// It imports the shared CSRF/fetch helpers and the shared <dialog> confirm
// (never window.confirm — the browser can suppress that; see dialog.js). The
// CSRF token rides in the <meta name="jts-csrf"> tag that canonical_page()
// renders, read at call time by http.js so this cacheable module bakes in no
// secret. The live HA URL + token are NEVER in the page DOM — the credentials
// copy button fetches them lazily from ./credentials-for-copy (CSRF-gated).
//
// Untrusted strings (mDNS-advertised instance names/URLs) are written via
// textContent / escaped data-* attributes, never innerHTML string-concat, so a
// hostile instance name can't inject markup.

import { csrfHeaders } from "/assets/shared/js/http.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";

// ---- shared helpers --------------------------------------------------------

function spinner() {
  const s = document.createElement("span");
  s.className = "ha-spinner";
  return s;
}

// Replace an element's children with a spinner + a text label, atomically.
function setLoading(el, label) {
  el.replaceChildren(spinner(), document.createTextNode(label));
}

async function postForm(path) {
  // Mutating-but-bodyless POSTs (./discover, ./ready, ./verify,
  // ./credentials-for-copy). csrfHeaders() adds X-CSRF-Token from the meta
  // tag; the server's verify_csrf() accepts the header like a form field.
  return fetch(path, { method: "POST", headers: csrfHeaders({}) });
}

// ---- state 1: discover + recent-URL fill -----------------------------------

function wireDiscover() {
  const btn = document.getElementById("discover-btn");
  const status = document.getElementById("discover-status");
  const results = document.getElementById("discover-results");
  const urlField = document.getElementById("url");
  if (!btn || !results || !urlField) return; // not state 1

  function fillUrl(value) {
    urlField.value = value;
    urlField.focus();
    urlField.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // Build one result row with DOM APIs (instance name/url are untrusted).
  function makeRow(item) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "discover-row";
    row.dataset.url = item.url || "";

    const nameLine = document.createElement("span");
    nameLine.className = "row-name";
    nameLine.textContent = item.location_name || "Home Assistant";
    if (item.version) {
      const ver = document.createElement("span");
      ver.className = "row-version";
      ver.textContent = ` (${item.version})`;
      nameLine.appendChild(ver);
    }

    const urlLine = document.createElement("span");
    urlLine.className = "row-url";
    urlLine.textContent = item.url || "";

    row.append(nameLine, urlLine);
    row.addEventListener("click", () => fillUrl(row.dataset.url));
    return row;
  }

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    if (status) setLoading(status, "Scanning the network…");
    results.replaceChildren();
    try {
      const r = await postForm("./discover");
      const data = await r.json();
      const items = (data && data.instances) || [];
      if (items.length === 0) {
        const empty = document.createElement("p");
        empty.className = "discover-empty";
        empty.textContent =
          "No Home Assistant instances found on this network. " +
          "Use the manual URL field below.";
        results.replaceChildren(empty);
      } else {
        results.replaceChildren(...items.map(makeRow));
      }
      if (status) status.textContent = "";
    } catch (e) {
      if (status) status.textContent = "Scan failed: " + e.message;
    }
    btn.disabled = false;
  });

  // Recent-URL chips fill the manual field (data-url is escaped server-side).
  for (const chip of document.querySelectorAll(".recent-link")) {
    chip.addEventListener("click", () => fillUrl(chip.dataset.url || ""));
  }
}

// ---- state 3: connected — verify / agents / restart-poll / copy ------------

function readPageData() {
  const el = document.getElementById("ha-page-data");
  if (!el) return null;
  try {
    return JSON.parse(el.textContent || "{}");
  } catch (e) {
    return {};
  }
}

function wireConnected() {
  const testBtn = document.getElementById("test-btn");
  const agentSelect = document.getElementById("agent_id");
  if (!testBtn && !agentSelect) return; // not state 3

  const pageData = readPageData() || {};
  const currentAgent = pageData.currentAgent || "";

  // Two endpoints, two purposes:
  //   ./ready  — one HA HTTP call (GET /api/). Used for the restart-poll loop
  //              where we just need a yes/no.
  //   ./verify — three HA HTTP calls. Used for the initial agent-picker
  //              populate AND the final post-readiness enrichment.
  async function pollReady() {
    try {
      const r = await postForm("./ready");
      const data = await r.json();
      return Boolean(data && data.ok);
    } catch (e) {
      return false;
    }
  }
  async function fullVerify() {
    try {
      const r = await postForm("./verify");
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  function populateAgents(data) {
    if (!data || !data.ok || !agentSelect) return;
    while (agentSelect.options.length > 1) agentSelect.remove(1);
    for (const a of data.agents || []) {
      const opt = document.createElement("option");
      opt.value = a.entity_id;
      opt.textContent = a.name + " (" + a.entity_id + ")";
      if (a.entity_id === currentAgent) opt.selected = true;
      agentSelect.appendChild(opt);
    }
    if (data.instance_name && data.instance_name !== "Home Assistant") {
      document.title = data.instance_name + " · Home Assistant · JTS speaker";
    }
  }

  // restarting=1 marker → the page just landed from a successful ./save.
  // jasper-voice restarts asynchronously (--no-block), so ./verify might 401
  // or hit a transient error for a few seconds. Poll ./ready every 1 s for up
  // to 15 s, showing a "Configuring…" chip that clears once ok=true.
  const isRestarting =
    new URLSearchParams(window.location.search).get("restarting") === "1";

  (async () => {
    if (!isRestarting) {
      populateAgents(await fullVerify());
      return;
    }
    const card = document.querySelector(".info-card");
    const chip = document.createElement("div");
    chip.className = "ha-chip ha-chip--busy";
    chip.append(
      spinner(),
      Object.assign(document.createElement("span"), {
        textContent:
          "Configuring… the speaker is finishing its restart. " +
          "Voice commands will work in a few seconds.",
      }),
    );
    if (card) card.insertAdjacentElement("afterend", chip);

    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (await pollReady()) {
        const data = await fullVerify();
        chip.className = "ha-chip ha-chip--ok";
        chip.replaceChildren(
          Object.assign(document.createElement("strong"), {
            textContent: "Ready.",
          }),
          document.createTextNode(" Smart-home commands work now."),
        );
        populateAgents(data);
        history.replaceState(null, "", window.location.pathname);
        return;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    // Timed out — friendly fallback with a manual retry.
    chip.className = "ha-chip ha-chip--warn";
    chip.replaceChildren(
      document.createTextNode(
        "The restart is taking longer than expected. ",
      ),
    );
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "btn btn--default";
    retry.textContent = "Test connection";
    retry.addEventListener("click", async () => populateAgents(await fullVerify()));
    chip.append(retry, document.createTextNode(" in a moment."));
    history.replaceState(null, "", window.location.pathname);
  })();

  // Test connection button.
  const testStatus = document.getElementById("test-status");
  if (testBtn) {
    testBtn.addEventListener("click", async () => {
      testBtn.disabled = true;
      if (testStatus) setLoading(testStatus, "Checking…");
      try {
        const r = await postForm("./verify");
        const data = await r.json();
        if (testStatus) {
          testStatus.className = "form-hint";
          if (data.ok) {
            testStatus.textContent =
              "Connected to " +
              (data.instance_name || "Home Assistant") +
              (data.version ? " (" + data.version + ")" : "") +
              ".";
            testStatus.classList.add("ha-ok");
          } else {
            testStatus.textContent = data.error || "Connection failed.";
            testStatus.classList.add("ha-err");
          }
        }
      } catch (e) {
        if (testStatus) testStatus.textContent = "Network error: " + e.message;
      }
      testBtn.disabled = false;
    });
  }

  wireCopyButtons(pageData.voicePackPrompt || "");
}

// ---- state 3: voice-pack copy buttons --------------------------------------

const URL_PLACEHOLDER_FOR_SHARING =
  "<your HA URL, e.g. http://homeassistant.local:8123>";
const TOKEN_PLACEHOLDER_FOR_SHARING =
  "<paste a long-lived access token from HA → Profile → Security → " +
  "Long-Lived Access Tokens>";

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // execCommand fallback for browsers that block writeText in non-secure
    // contexts (rare on LAN but cheap insurance).
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;left:-9999px;top:-9999px;";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (e2) {
      ok = false;
    }
    document.body.removeChild(ta);
    return ok;
  }
}

function wireCopyButtons(template) {
  const feedback = document.getElementById("copy-voice-prompt-feedback");
  function showFeedback(msg, ok) {
    if (!feedback) return;
    feedback.textContent = msg;
    feedback.classList.toggle("ha-ok", ok);
    feedback.classList.toggle("ha-err", !ok);
    feedback.classList.add("shown");
    setTimeout(() => feedback.classList.remove("shown"), 3000);
  }

  const plainBtn = document.getElementById("copy-voice-prompt-btn");
  if (plainBtn) {
    plainBtn.addEventListener("click", async () => {
      const text = template
        .replace("{HA_URL_PLACEHOLDER}", URL_PLACEHOLDER_FOR_SHARING)
        .replace("{HA_TOKEN_PLACEHOLDER}", TOKEN_PLACEHOLDER_FOR_SHARING);
      const ok = await copyToClipboard(text);
      showFeedback(
        ok
          ? "Prompt copied — paste into your coding agent"
          : "Copy failed — try selecting the page text manually",
        ok,
      );
    });
  }

  const credsBtn = document.getElementById("copy-voice-prompt-creds-btn");
  if (credsBtn) {
    credsBtn.addEventListener("click", async () => {
      const ok = await jtsConfirm(
        "This will put your Home Assistant URL and a long-lived access " +
          "token onto your clipboard.\n\n" +
          "Anyone with this token can control your Home Assistant. Do NOT:\n" +
          "  • paste into a public chat or shared doc\n" +
          "  • commit to a git repo\n" +
          "  • share a screenshot of the prompt\n\n" +
          "Continue?",
        { danger: true },
      );
      if (!ok) return;
      // Fetch credentials lazily — the page never holds the live URL/token in
      // the DOM. csrfHeaders() supplies the X-CSRF-Token the endpoint requires.
      let creds;
      try {
        const r = await postForm("./credentials-for-copy");
        if (!r.ok) {
          showFeedback(
            "Could not fetch credentials (server returned " + r.status + ")",
            false,
          );
          return;
        }
        creds = await r.json();
      } catch (e) {
        showFeedback("Could not fetch credentials — network error", false);
        return;
      }
      const text = template
        .replace("{HA_URL_PLACEHOLDER}", creds.url)
        .replace("{HA_TOKEN_PLACEHOLDER}", creds.token);
      const copied = await copyToClipboard(text);
      showFeedback(
        copied
          ? "Prompt + credentials copied — paste into your coding agent"
          : "Copy failed — try the placeholder button instead",
        copied,
      );
    });
  }
}

// ---- shared: confirm-on-submit for the Disconnect form ---------------------
//
// The connected-state Disconnect form carries data-confirm / data-confirm-
// danger (set server-side). Intercept submit, confirm via the shared
// <dialog>, then let the native POST proceed. Mirrors the spotify/ page's
// data-confirm convention so the behaviour is identical across wizards.

function wireConfirmForms() {
  for (const form of document.querySelectorAll("form[data-confirm]")) {
    form.addEventListener("submit", async (event) => {
      if (form.dataset.confirmed === "1") return; // re-submit after confirm
      event.preventDefault();
      const ok = await jtsConfirm(form.dataset.confirm, {
        danger: form.dataset.confirmDanger === "1",
      });
      if (!ok) return;
      form.dataset.confirmed = "1";
      form.submit();
    });
  }
}

wireDiscover();
wireConnected();
wireConfirmForms();
