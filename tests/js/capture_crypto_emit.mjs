// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Cross-language E2E proof emitter (phone-mic relay steps 3 + 5).
//
// Encrypts a payload with the PAGE's WebCrypto encryptor (capture-page/js/
// crypto.js) and prints the key/blob/integrity as JSON so the Pi-side
// decryptor (jasper/capture_relay/crypto.py) can prove a bit-identical
// round-trip — the plan §15 "decrypted WAV is bit-identical" criterion.
//
// Run by tests/test_capture_relay_crypto.py. It imports the capture page, so it
// only executes once BOTH the page (step 3) and this test land on the same tree
// (e.g. main); the Python test skips gracefully otherwise. `node --check` is
// syntax-only and does not resolve the import, so it stays green either way.
//
//   node tests/js/capture_crypto_emit.mjs

import { importContentKey, encryptWav } from "../../capture-page/js/crypto.js";

function b64url(bytes) {
  return Buffer.from(bytes)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

const keyBytes = new Uint8Array(32);
for (let i = 0; i < keyBytes.length; i++) keyBytes[i] = (i * 9 + 1) & 0xff;
const keyB64url = b64url(keyBytes);
const key = await importContentKey(keyB64url);

// A deterministic, WAV-sized-ish payload.
const plaintext = new Uint8Array(2048);
for (let i = 0; i < plaintext.length; i++) plaintext[i] = (i * 13 + 7) & 0xff;

const { blob, plaintextLen, sha256 } = await encryptWav(key, plaintext);

console.log(
  JSON.stringify({
    key_b64url: keyB64url,
    blob_b64: Buffer.from(blob).toString("base64"),
    plaintext_b64: Buffer.from(plaintext).toString("base64"),
    plaintext_len: plaintextLen,
    sha256,
  }),
);
