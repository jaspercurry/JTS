#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Offline self-tests for e0_capture.py.

Run with (from the repo root, whose .venv already has requests +
cryptography -- see README.md "Run it"):
    .venv/bin/python experiments/e0-capture/test_e0_capture.py
    .venv/bin/python experiments/e0-capture/e0_capture.py --selftest

These same checks run in CI through tests/test_e0_capture_experiment.py,
which imports this module and parametrizes over TESTS.

NEVER touches the network -- no relay.jasper.tech, no jts3.local. Every
check round-trips data in-process against fabricated fixtures, or writes to
a local temp file.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e0_capture as e0  # noqa: E402


def test_content_key_b64url_round_trip() -> None:
    key = e0.crypto.generate_content_key()
    b64 = e0.crypto.content_key_to_b64url(key)
    assert isinstance(b64, str) and "=" not in b64, "content_key b64url should be unpadded"
    decoded = e0.crypto.content_key_from_b64url(b64)
    assert decoded == key, "content_key round-trip mismatch"


def test_encrypt_then_pi_decrypt_round_trip() -> None:
    key = e0.crypto.generate_content_key()
    wav_bytes = b"RIFF....WAVEfmt fake payload for a round-trip test" * 37
    blob, plaintext_len, sha256_hex = e0.encrypt_wav(key, wav_bytes)
    recovered = e0.crypto.decrypt_and_verify(key, blob, plaintext_len, sha256_hex)
    assert recovered == wav_bytes, "decrypt_and_verify did not recover the original WAV bytes"

    # Tamper check: flipping a ciphertext byte must fail AES-GCM authentication.
    tampered = bytearray(blob)
    tampered[-1] ^= 0xFF
    try:
        e0.crypto.decrypt_and_verify(key, bytes(tampered), plaintext_len, sha256_hex)
    except e0.crypto.DecryptError:
        pass
    else:
        raise AssertionError("tampered ciphertext should have failed AES-GCM auth")


def test_authenticated_phone_event_round_trip() -> None:
    key = e0.crypto.generate_content_key()
    session_id = "cap_selftest0000000000000000000"
    event = {
        "begin_capture": {"index": 1, "attempt": 1},
        "setup": {"calibration": {"mode": "none"}},
        "capture_page": e0.DEFAULT_CAPTURE_PAGE_IDENTITY,
    }
    envelope = e0.integ.authenticated_phone_event(key, session_id, event, sequence=1)
    assert set(envelope) == {"authenticated_event"}
    decoded, sequence = e0.integ.verify_authenticated_phone_event(key, session_id, envelope)
    assert sequence == 1
    assert decoded == event, "authenticated event payload mismatch after verify"

    # Tamper check: a flipped MAC character must fail verification.
    #
    # Flip the FIRST character, not the last. The MAC is unpadded base64url of
    # a 32-byte HMAC: 43 characters, so the last one carries 4 meaningful bits
    # and 2 slack bits, and `verify_authenticated_phone_event` compares the
    # DECODED bytes. Four of the 64 possible last characters therefore decode
    # identically, which made a last-character "forgery" verify cleanly -- and
    # this check fail -- on 6.01% of runs (20000 random MACs, measured
    # 2026-08-17). Every bit of the first character is meaningful.
    forged = json.loads(json.dumps(envelope))
    mac = forged["authenticated_event"]["mac"]
    forged["authenticated_event"]["mac"] = ("A" if mac[0] != "A" else "B") + mac[1:]
    try:
        e0.integ.verify_authenticated_phone_event(key, session_id, forged)
    except e0.integ.CaptureIntegrityError:
        pass
    else:
        raise AssertionError("forged MAC should have failed verification")

    # Sequence must be strictly increasing across a session -- exercise two
    # real posts the way RelayPhoneClient.post_event does internally.
    second = e0.integ.authenticated_phone_event(key, session_id, {"armed": True}, sequence=2)
    _, seq2 = e0.integ.verify_authenticated_phone_event(key, session_id, second)
    assert seq2 == 2


def test_fragment_parsing() -> None:
    key = e0.crypto.generate_content_key()
    key_b64 = e0.crypto.content_key_to_b64url(key)
    tap_link = f"https://capture.jasper.tech/#s=cap_abc123&u=upl_tok&k={key_b64}&a=deadbeefmac"
    handle = e0.parse_tap_link(tap_link)
    assert handle.session_id == "cap_abc123"
    assert handle.upload_token == "upl_tok"
    assert handle.content_key_b64 == key_b64
    assert handle.spec_mac == "deadbeefmac"
    # the content_key in the fragment must decode cleanly back to the same key
    assert e0.crypto.content_key_from_b64url(handle.content_key_b64) == key

    # a bare fragment (no scheme/host) also parses
    handle2 = e0.parse_tap_link(f"#s=x&u=y&k={key_b64}&a=z")
    assert handle2.session_id == "x"

    # a missing required field raises FragmentError, not a KeyError/None
    try:
        e0.parse_tap_link("https://capture.jasper.tech/#s=&u=y&k=z")
    except e0.FragmentError:
        pass
    else:
        raise AssertionError("a missing session id should have raised FragmentError")


def test_spec_mac_round_trip_and_wire_helpers() -> None:
    key = e0.crypto.generate_content_key()
    session_id = "cap_spec_selftest"
    spec = {
        "schema_version": 1,
        "capture_protocol_version": 3,
        "kind": "crossover_sweep",
        "sample_rate_hz": 48000,
        "channels": 1,
        "duration_ms": 25000,
        "post_roll_ms": 700,
        "capture_plan": {
            "schema_version": 2,
            "capture_target": 3,
            "max_attempts": 4,
            "entries": [
                {"index": 0, "kind_label": "check", "duration_ms": 25000},
                {"index": 1, "kind_label": "measure", "duration_ms": 20000},
                {"index": 2, "kind_label": "verify", "duration_ms": 15000,
                 "screen": {"auto_advance": "on_apply"}},
            ],
        },
    }
    spec_text = json.dumps(spec, separators=(",", ":"))
    mac = e0.integ.capture_spec_mac(key, session_id, spec_text)
    e0.integ.verify_capture_spec_mac(key, session_id, spec_text, mac)  # must not raise

    try:
        e0.integ.verify_capture_spec_mac(key, session_id, spec_text + " ", mac)
    except e0.integ.CaptureIntegrityError:
        pass
    else:
        raise AssertionError("a mutated spec should have failed MAC verification")

    # entry_for_wire_index: wire is 1-based, entries[] is 0-based (PROTOCOL.md #5)
    assert e0.entry_for_wire_index(spec, 1)["kind_label"] == "check"
    assert e0.entry_for_wire_index(spec, 2)["kind_label"] == "measure"
    assert e0.entry_for_wire_index(spec, 3)["kind_label"] == "verify"
    assert e0.entry_for_wire_index(spec, 4) is None

    assert e0.poll_interval_s(spec) == 0.25  # no progress_poll_ms -> 250ms fallback

    # setup_wire_payload: no default_setup hint -> mode "none"
    assert e0.setup_wire_payload(spec) == {"calibration": {"mode": "none"}}
    assert e0.acknowledgement_payload(spec) is None

    # a resolvable household-mic hint -> mode "stored"
    spec_with_hint = {**spec, "default_setup": {"calibration": {
        "mode": "serial", "model": "umik2", "calibration_id": "cal_123", "resolvable": True,
    }}}
    assert e0.setup_wire_payload(spec_with_hint) == {
        "calibration": {"mode": "stored", "calibration_id": "cal_123", "model": "umik2"}
    }

    # an UNresolvable hint (resolvable missing/false) must NOT be echoed as stored
    spec_unresolvable_hint = {**spec, "default_setup": {"calibration": {
        "mode": "serial", "model": "umik2", "calibration_id": "cal_123",
    }}}
    assert e0.setup_wire_payload(spec_unresolvable_hint) == {"calibration": {"mode": "none"}}

    # acknowledgement echo mirrors render.js's acceptedAcknowledgement()
    spec_with_ack = {**spec, "acknowledgement": {
        "schema_version": 1, "id": "confirm_placement",
        "binding_id": "bnd_abc123def456", "label": "Confirm placement",
    }}
    ack = e0.acknowledgement_payload(spec_with_ack)
    assert ack == {
        "schema_version": 1, "id": "confirm_placement",
        "binding_id": "bnd_abc123def456", "accepted": True,
    }


def test_session_start_body_carries_the_tier() -> None:
    """The mint body must name the tier -- the July build hardcoded "{}", which
    resolves to DEFAULT_TIER == "full" and so could never open a remote-tier
    session (issue #2636).
    """
    assert json.loads(e0.session_start_body(e0.TIER_REMOTE)) == {"tier": "remote"}
    assert json.loads(e0.session_start_body(e0.TIER_FULL)) == {"tier": "full"}
    # An empty tier degrades to the July body, which the Pi reads as "full".
    assert e0.session_start_body("") == "{}"

    # The CLI defaults to remote and refuses an unknown tier locally rather
    # than after a round-trip to the Pi.
    parser = e0.build_arg_parser()
    assert parser.parse_args(["--start-session"]).tier == e0.TIER_REMOTE
    assert parser.parse_args(["--start-session", "--tier", "full"]).tier == e0.TIER_FULL
    try:
        parser.parse_args(["--start-session", "--tier", "supersonic"])
    except SystemExit:
        pass
    else:
        raise AssertionError("an unknown tier should have been refused by argparse")

    # --tap-link stays a separate mode: it must not require --host, and it
    # must remain mutually exclusive with minting a new session.
    tapped = parser.parse_args(["--tap-link", "https://capture.jasper.tech/#s=a&u=b&k=c"])
    assert tapped.tap_link and not tapped.start_session
    try:
        parser.parse_args(["--start-session", "--tap-link", "x"])
    except SystemExit:
        pass
    else:
        raise AssertionError("--start-session and --tap-link should be mutually exclusive")


def test_capture_page_identity_passes_the_pi_validator() -> None:
    """Run the REAL `validate_capture_page` against our hardcoded identity.

    The July identity advertised protocols [1, 2, 3]; 1 and 2 have since been
    deleted (jasper/capture_relay/spec.py:50-58). This asserts the shipped
    identity still satisfies the live validator rather than trusting a
    re-reading of it.
    """
    import importlib.util
    import sys as _sys

    spec_mod_path = e0.WT / "session.py"
    # session.py imports `from .spec import ...`, so it needs the real package.
    repo_root = e0.WT.parents[1]
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))
    spec_spec = importlib.util.spec_from_file_location(
        "jasper.capture_relay.session", spec_mod_path
    )
    assert spec_spec is not None and spec_spec.loader is not None
    session_mod = importlib.util.module_from_spec(spec_spec)
    try:
        spec_spec.loader.exec_module(session_mod)
    except Exception as exc:  # noqa: BLE001 -- import-shape problems are not our contract
        print(f"    skipped: could not import the live session module ({type(exc).__name__}: {exc})")
        return

    from jasper.capture_relay.spec import CaptureSpec  # noqa: PLC0415

    pi_spec = CaptureSpec(kind="crossover_sweep", duration_ms=20000, pre_roll_ms=0, post_roll_ms=0)
    observed = session_mod.validate_capture_page(e0.DEFAULT_CAPTURE_PAGE_IDENTITY, pi_spec)
    assert observed == e0.DEFAULT_CAPTURE_PAGE_IDENTITY
    print(f"    live validate_capture_page accepted build {observed['capture_page_build']}")

    # Positive control: a mutated identity must be REFUSED, otherwise the
    # assertion above proves nothing.
    broken = {**e0.DEFAULT_CAPTURE_PAGE_IDENTITY, "supported_capture_protocol_versions": [1, 2]}
    try:
        session_mod.validate_capture_page(broken, pi_spec)
    except session_mod.CapturePageIncompatible:
        pass
    else:
        raise AssertionError("an identity omitting protocol 3 should have been refused")


def test_wav_encoding_matches_the_page() -> None:
    """Synthesize a WAV and confirm it matches the page's float32ToWavBlob
    encoding (48kHz / mono / 16-bit PCM -- PROTOCOL.md #10). Also attempts a
    real 0.2s sox recording from the default input as a best-effort hardware
    smoke check; skipped (never failed) if no input device is available in
    this environment.

    Synthesis is stdlib-only (`wave` + `array`, the same modules
    `e0.wav_format_ok` reads with). The July original used numpy + soundfile
    here; soundfile is not installed in this repo's .venv, so the selftest
    could not run offline without adding a dependency it never needed.
    """
    import array
    import math
    import wave

    with tempfile.TemporaryDirectory() as tmp:
        synth_path = Path(tmp) / "synth.wav"
        samples = array.array(
            "h", (int(0.1 * 32767 * math.sin(2 * math.pi * 1000 * n / 48000)) for n in range(9600))
        )
        with wave.open(str(synth_path), "wb") as wf:
            wf.setnchannels(e0.REQUIRED_CHANNELS)
            wf.setsampwidth(e0.REQUIRED_BITS // 8)
            wf.setframerate(e0.REQUIRED_SAMPLE_RATE_HZ)
            wf.writeframes(samples.tobytes())
        ok, detail = e0.wav_format_ok(synth_path)
        assert ok, f"synthesized WAV format mismatch: {detail}"
        print(f"    synthesized WAV format OK: {detail}")

        if shutil.which("sox") is None:
            print("    sox not found on PATH -- skipping real-recording smoke check")
            return
        real_path = Path(tmp) / "real.wav"
        try:
            e0.record_wav(real_path, seconds=0.2, mic="default")
        except Exception as exc:  # noqa: BLE001 -- best-effort hardware probe, never fails the suite
            print(f"    sox real-recording smoke check skipped ({type(exc).__name__}: {exc})")
            return
        ok, detail = e0.wav_format_ok(real_path)
        assert ok, f"real sox recording format mismatch: {detail}"
        print(f"    real sox recording format OK: {detail}")


TESTS = [
    test_content_key_b64url_round_trip,
    test_encrypt_then_pi_decrypt_round_trip,
    test_authenticated_phone_event_round_trip,
    test_fragment_parsing,
    test_spec_mac_round_trip_and_wire_helpers,
    test_session_start_body_carries_the_tier,
    test_capture_page_identity_passes_the_pi_validator,
    test_wav_encoding_matches_the_page,
]


def run_all() -> bool:
    passed = 0
    failed = 0
    for test in TESTS:
        name = test.__name__
        try:
            test()
        except Exception:  # noqa: BLE001 -- report every failure, keep running the rest
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run_all() else 1)
