// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — behaviour for the area hubs (/sound/, /assistant/), which are
// static pages rendered at install time by jasper.web.nav. A hub is rows and
// nothing else, so all of it is the shared settings-surface module; the title
// stays the hub's own name (docs/web-ia.md §2).

import {
  bakedCaps,
  initSettingsStatus,
} from "/assets/shared/js/settings-status.js";

initSettingsStatus({ caps: bakedCaps(), titleFollowsSpeakerName: false });
