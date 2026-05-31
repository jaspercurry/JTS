// api.js — CSRF-aware fetch helpers for the /system/ dashboard.
//
// These helpers are now the shared cross-page module
// /assets/shared/js/http.js; this file re-exports them so the system-status
// modules keep importing `./api.js` unchanged. Same X-CSRF-Token contract,
// same behaviour — see http.js for the rationale (the token rides in the
// <meta name="jts-csrf"> tag so the cacheable module bakes in no secret).
//
// http.js additionally exports postJSON(); import it directly from the shared
// module when a new caller needs a JSON POST.

export { csrfHeaders, jsonHeaders, getJSON } from "../../shared/js/http.js";
