// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Privacy-bounded persistence for the guided phone-microphone setup.
//
// The idle expiry slides only after a successful capture-only measurement, so
// a deliberate multi-position run does not unexpectedly lose its frozen mic /
// calibration binding.  The absolute expiry never moves: browser storage can
// keep a setup only for the lifetime of this bound measurement session.

export const SETUP_STORAGE_KEY = "jts.capture.bound-setup.v2";
const LEGACY_SETUP_STORAGE_KEYS = Object.freeze([
  "jts.capture.bound-setup.v1",
]);
export const SETUP_STORAGE_SCHEMA = 2;
export const SETUP_IDLE_TTL_MS = 20 * 60 * 1000;
export const SETUP_ABSOLUTE_TTL_MS = 2 * 60 * 60 * 1000;
export const MAX_SETUP_STORAGE_BYTES = 8 * 1024;

function utf8Size(value) {
  return new TextEncoder().encode(String(value || "")).byteLength;
}

function storageOrDefault(storage) {
  if (storage) return storage;
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

function clearLegacyRecords(storage) {
  if (!storage) return;
  for (const key of LEGACY_SETUP_STORAGE_KEYS) storage.removeItem(key);
}

export function setupBindingId(spec) {
  const value = String(
    (spec && spec.setup_binding_id) ||
      (spec && spec.kind === "level_ramp" && spec.run_token) ||
      "",
  ).trim();
  return /^[A-Za-z0-9_-]{12,160}$/.test(value) ? value : "";
}

export function clearBoundSetup({ storage } = {}) {
  try {
    const target = storageOrDefault(storage);
    if (target) {
      target.removeItem(SETUP_STORAGE_KEY);
      clearLegacyRecords(target);
    }
  } catch {
    // Storage denial is equivalent to an absent setup; callers fail visibly.
  }
}

function validRecord(record, spec, nowMs) {
  const bindingId = setupBindingId(spec);
  return Boolean(
    record &&
      record.schema === SETUP_STORAGE_SCHEMA &&
      bindingId &&
      record.binding_id === bindingId &&
      Number(record.expires_at || 0) > nowMs &&
      Number(record.absolute_expires_at || 0) > nowMs &&
      Number(record.created_at || 0) > 0 &&
      Number(record.created_at) <= nowMs &&
      Number(record.expires_at) <= Number(record.absolute_expires_at) &&
      record.identity &&
      typeof record.identity === "object" &&
      record.summary &&
      typeof record.summary === "object"
  );
}

export function loadBoundSetup(spec, { storage, now = () => Date.now() } = {}) {
  const target = storageOrDefault(storage);
  try {
    clearLegacyRecords(target);
    const raw = target ? String(target.getItem(SETUP_STORAGE_KEY) || "") : "";
    if (!raw || utf8Size(raw) > MAX_SETUP_STORAGE_BYTES) {
      clearBoundSetup({ storage: target });
      return null;
    }
    const record = JSON.parse(raw);
    if (!validRecord(record, spec, Number(now()))) {
      clearBoundSetup({ storage: target });
      return null;
    }
    return record;
  } catch {
    clearBoundSetup({ storage: target });
    return null;
  }
}

export function storeBoundSetup(
  spec,
  identity,
  summary,
  { storage, now = () => Date.now() } = {},
) {
  const bindingId = setupBindingId(spec);
  const target = storageOrDefault(storage);
  if (!bindingId || !identity || !target) return false;
  const createdAt = Number(now());
  const absoluteExpiresAt = createdAt + SETUP_ABSOLUTE_TTL_MS;
  const record = {
    schema: SETUP_STORAGE_SCHEMA,
    binding_id: bindingId,
    created_at: createdAt,
    expires_at: Math.min(createdAt + SETUP_IDLE_TTL_MS, absoluteExpiresAt),
    absolute_expires_at: absoluteExpiresAt,
    identity,
    summary,
  };
  const raw = JSON.stringify(record);
  if (utf8Size(raw) > MAX_SETUP_STORAGE_BYTES) return false;
  try {
    clearLegacyRecords(target);
    target.setItem(SETUP_STORAGE_KEY, raw);
    return true;
  } catch {
    return false;
  }
}

export function refreshBoundSetup(
  spec,
  { storage, now = () => Date.now() } = {},
) {
  const target = storageOrDefault(storage);
  const record = loadBoundSetup(spec, { storage: target, now });
  if (!record || !target) return false;
  const nowMs = Number(now());
  const refreshed = {
    ...record,
    expires_at: Math.min(
      nowMs + SETUP_IDLE_TTL_MS,
      Number(record.absolute_expires_at),
    ),
  };
  try {
    target.setItem(SETUP_STORAGE_KEY, JSON.stringify(refreshed));
    return true;
  } catch {
    return false;
  }
}
