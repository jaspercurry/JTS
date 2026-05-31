// main.js — /transit/ wizard behaviour.
//
// The page is server-rendered: the address / save / clear forms POST and the
// server re-renders. This module only adds the small client-side glue the
// forms need — it never fetches or renders state itself:
//
//   1. Multi-select sync. The bus-stop and Citi Bike pickers are checkbox
//      grids, but they round-trip through one urlencoded hidden field each
//      ("id|label,id|label" — the format parse_bus_stops / parse_saved_stations
//      read server-side). We keep that hidden field in lockstep with the
//      checkboxes on every change.
//   2. "Change address" reveal. The geocode result panel hides its re-geocode
//      form until the user clicks Change.
//   3. Clear confirm. Clearing transit config is destructive (the subway/bus
//      tools go quiet until reconfigured), so we confirm via the shared
//      <dialog> helper — never window.confirm, which the browser can suppress.
//
// All wiring is addEventListener on escaped data-* attributes; no inline
// handlers, no untrusted strings interpolated into JS.

import { jtsConfirm } from "/assets/shared/js/dialog.js";

// ---- 1. Multi-select checkbox → hidden field sync -------------------------

// Keep `hiddenId`'s value as the comma-joined "id|label" list of the checked
// rows in `pickClass`. `idKey` / `labelKey` are the camelCased data-attribute
// names (dataset keys) carrying each row's id and label. Pipes/commas in a
// label are squashed to spaces so they can't corrupt the delimiter format.
function syncPicker(pickClass, hiddenId, idKey, labelKey) {
  const hidden = document.getElementById(hiddenId);
  if (!hidden) return;
  const checkboxes = document.querySelectorAll("." + pickClass);
  const sync = () => {
    const parts = [];
    checkboxes.forEach((cb) => {
      if (!cb.checked) return;
      const id = cb.dataset[idKey] || "";
      const label = (cb.dataset[labelKey] || "").replace(/[|,]/g, " ");
      parts.push(label ? id + "|" + label : id);
    });
    hidden.value = parts.join(",");
  };
  checkboxes.forEach((cb) => cb.addEventListener("change", sync));
  sync(); // reconcile once on load
}

syncPicker("bus-stop-pick", "nyc-bus-stops-hidden", "stopId", "stopLabel");
syncPicker("citibike-pick", "citibike-stations-hidden", "stationId", "stationLabel");

// ---- 2. "Change address" reveals the re-geocode form ---------------------

const changeBtn = document.querySelector("[data-action='change-address']");
const redoForm = document.getElementById("redo-form");
const addressResult = document.getElementById("address-result");
if (changeBtn && redoForm) {
  changeBtn.addEventListener("click", () => {
    redoForm.hidden = false;
    if (addressResult) addressResult.hidden = true;
  });
}

// ---- 3. Clear-config confirm via the shared dialog -----------------------

const clearForm = document.getElementById("clear-form");
if (clearForm) {
  clearForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ok = await jtsConfirm(
      "Clear all saved transit settings? Subway and bus tools will stop " +
        "responding until reconfigured.",
      { danger: true }
    );
    if (ok) clearForm.submit();
  });
}
