// detail.js — generated /tools/tool/<name>/ detail page.
//
// The server only passes the requested name through a JSON data island; this
// module fetches the same catalog JSON as the list page and renders the matching
// entry. That keeps the tool detail surface catalog-driven and avoids a second
// Python route that imports the runtime registry.

import { getJSON } from "/assets/shared/js/http.js";
import { escapeHtml } from "/assets/shared/js/escape.js";
import { toolDetail } from "./render.js";

const mount = document.getElementById("tool-detail");

function requestedName() {
  const island = document.getElementById("tool-detail-data");
  if (!island) return "";
  try {
    const data = JSON.parse(island.textContent || "{}");
    return typeof data.name === "string" ? data.name : "";
  } catch (_) {
    return "";
  }
}

function toolsOf(catalog) {
  return catalog && Array.isArray(catalog.tools) ? catalog.tools : [];
}

function unavailable(message) {
  return (
    '<div class="info-card tool-empty">' +
    "<p>" + escapeHtml(message) + ' <a href="/tools/">Back to tools</a>.</p>' +
    "</div>"
  );
}

async function load() {
  if (!mount) return;
  const name = requestedName();
  if (!name) {
    mount.innerHTML = unavailable("Tool not found.");
    mount.removeAttribute("aria-busy");
    return;
  }
  try {
    const catalog = await getJSON("/tools/catalog.json");
    if (catalog && catalog.unavailable) {
      mount.innerHTML = unavailable("Tool catalog is not ready yet.");
      return;
    }
    const tool = toolsOf(catalog).find((t) => t && t.name === name);
    mount.innerHTML = toolDetail(tool);
  } catch (err) {
    mount.innerHTML = unavailable(
      "Could not load the tool catalog (" + err.message + ").",
    );
  } finally {
    mount.removeAttribute("aria-busy");
  }
}

load();
