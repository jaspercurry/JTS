// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0

// Public-page/Pi compatibility contract. Specs created before this handshake
// had no explicit version; their shipped behavior is protocol 1. Keeping that
// one narrow legacy mapping lets the page be published before Pi upgrades.
export const LEGACY_CAPTURE_PROTOCOL_VERSION = 1;

export function requiredCaptureProtocol(spec) {
  const raw = spec && spec.capture_protocol_version;
  if (raw === undefined || raw === null) return LEGACY_CAPTURE_PROTOCOL_VERSION;
  const version = Number(raw);
  return Number.isInteger(version) && version > 0 ? version : null;
}

export function validateCapturePageIdentity(identity) {
  if (
    !identity ||
    identity.schema_version !== 1 ||
    !Number.isInteger(identity.capture_protocol_version) ||
    identity.capture_protocol_version <= 0 ||
    !Array.isArray(identity.supported_capture_protocol_versions) ||
    identity.supported_capture_protocol_versions.length === 0 ||
    !identity.supported_capture_protocol_versions.every(
      (value) => Number.isInteger(value) && value > 0,
    ) ||
    new Set(identity.supported_capture_protocol_versions).size !==
      identity.supported_capture_protocol_versions.length ||
    !identity.supported_capture_protocol_versions.includes(
      identity.capture_protocol_version,
    ) ||
    typeof identity.capture_page_build !== "string" ||
    !/^[0-9]{8}\.[0-9]+$/.test(identity.capture_page_build)
  ) {
    throw new Error("capture page version is invalid");
  }
  return identity;
}

export function assertCaptureProtocolCompatible(spec, pageIdentity) {
  const expected = requiredCaptureProtocol(spec);
  const supported = pageIdentity && pageIdentity.supported_capture_protocol_versions;
  if (
    expected === null ||
    !Array.isArray(supported) ||
    !supported.includes(expected)
  ) {
    throw new Error(
      `capture page is incompatible with this speaker (speaker protocol ${expected || "unknown"}, page supports ${Array.isArray(supported) ? supported.join(", ") : "unknown"})`,
    );
  }
  return expected;
}
