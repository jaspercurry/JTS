// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import {
  loadBoundSetup,
  refreshBoundSetup,
  SETUP_ABSOLUTE_TTL_MS,
  SETUP_IDLE_TTL_MS,
  SETUP_STORAGE_KEY,
  storeBoundSetup,
} from "../../capture-page/js/setup-store.js";

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) || null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

let passed = 0;
const spec = {
  kind: "room_sweep",
  setup_binding_id: "room-session-12345",
};
const identity = {
  schema: 1,
  binding_id: spec.setup_binding_id,
  sha256: "a".repeat(64),
};
const summary = {
  total_positions: 5,
  calibration: { mode: "none", model: "" },
};

// Successful capture-only reuse slides the idle deadline.
{
  const storage = new MemoryStorage();
  storage.setItem("jts.capture.bound-setup.v1", JSON.stringify({ expires_at: Infinity }));
  let clock = 1_000_000;
  const now = () => clock;
  assert.equal(storeBoundSetup(spec, identity, summary, { storage, now }), true);
  assert.equal(storage.getItem("jts.capture.bound-setup.v1"), null);
  const first = loadBoundSetup(spec, { storage, now });
  assert.equal(first.expires_at, clock + SETUP_IDLE_TTL_MS);

  clock += SETUP_IDLE_TTL_MS - 1000;
  assert.equal(refreshBoundSetup(spec, { storage, now }), true);
  const refreshed = loadBoundSetup(spec, { storage, now });
  assert.equal(refreshed.expires_at, clock + SETUP_IDLE_TTL_MS);
  assert.equal(refreshed.absolute_expires_at, 1_000_000 + SETUP_ABSOLUTE_TTL_MS);
  passed += 1;
}

// Reuse can never move the setup past its absolute privacy lifetime.
{
  const storage = new MemoryStorage();
  let clock = 2_000_000;
  const created = clock;
  const now = () => clock;
  assert.equal(storeBoundSetup(spec, identity, summary, { storage, now }), true);

  for (let i = 1; i <= 6; i += 1) {
    clock = created + i * (SETUP_IDLE_TTL_MS - 1000);
    if (clock < created + SETUP_ABSOLUTE_TTL_MS) {
      assert.equal(refreshBoundSetup(spec, { storage, now }), true);
    }
  }
  const raw = JSON.parse(storage.getItem(SETUP_STORAGE_KEY));
  assert.equal(raw.absolute_expires_at, created + SETUP_ABSOLUTE_TTL_MS);
  assert.ok(raw.expires_at <= raw.absolute_expires_at);

  clock = created + SETUP_ABSOLUTE_TTL_MS;
  assert.equal(loadBoundSetup(spec, { storage, now }), null);
  assert.equal(storage.getItem(SETUP_STORAGE_KEY), null);
  passed += 1;
}

// A different setup/session binding cannot reuse the frozen identity.
{
  const storage = new MemoryStorage();
  const now = () => 3_000_000;
  assert.equal(storeBoundSetup(spec, identity, summary, { storage, now }), true);
  assert.equal(loadBoundSetup({ ...spec, setup_binding_id: "another-session-123" }, {
    storage,
    now,
  }), null);
  passed += 1;
}

console.log(JSON.stringify({ ok: true, passed }));
