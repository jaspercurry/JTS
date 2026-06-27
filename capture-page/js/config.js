// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Deploy-time constants for the capture page (build step 3).
//
// The relay is ONE origin for the whole fleet (the O(1) property — see
// docs/phone-mic-relay-plan.md §2). Change this once per deployment; the Pi
// points its tap-link at this page's origin, and this page knows its relay.
export const RELAY_BASE = "https://relay.jasper.tech";
