// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// api.js — shared fetch helpers for the /chat/ dashboard.
//
// Re-export the cross-page module so this page keeps the same small graph
// shape as /system/: page code imports `./api.js`, while the CSRF/header
// implementation stays owned by /assets/shared/js/http.js.

export { csrfHeaders, jsonHeaders, getJSON } from "../../shared/js/http.js";
