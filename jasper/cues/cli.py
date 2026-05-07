"""`jasper-cues` — operator CLI for the audio-cue subsystem.

Subcommands:
  list                       — show every registered cue, its rendered
                                text, expected filename, whether the
                                cached file exists.
  regenerate [--cue X]
             [--force]       — synthesise missing (or all, with
                                --force) cues. Reads
                                JASPER_MANAGEMENT_URL,
                                JASPER_GEMINI_VOICE,
                                JASPER_SOUNDS_DIR,
                                GEMINI_API_KEY from env.
  play <slug>                — play a cached cue locally for testing.
                                Uses TtsPlayout the same way the
                                daemon does. Useful when you change
                                a template and want to hear the new
                                phrasing before deploying.

Designed to be run interactively or from `install.sh` post-install.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import urllib.parse

from .generator import GeminiTTSGenerator
from .manager import AudioCueManager
from .registry import CUES, find as find_cue


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _hostname_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname or ""


def _make_manager(*, with_tts: bool = False) -> AudioCueManager:
    """Build a manager from environment variables. Same env names the
    daemon uses, so a CLI run and a daemon run agree on which file is
    canonical for a given cue."""
    sounds_dir = _env("JASPER_SOUNDS_DIR", "/var/lib/jasper/sounds")
    management_url = _env("JASPER_MANAGEMENT_URL", "https://jts.local")
    voice = _env("JASPER_GEMINI_VOICE", "Aoede")
    api_key = _env("GEMINI_API_KEY") or _env("JASPER_GEMINI_API_KEY")

    backend = None
    if api_key:
        try:
            backend = GeminiTTSGenerator(api_key=api_key, voice=voice)
        except ValueError as e:
            print(f"warning: TTS backend disabled ({e}); regen will fail",
                  file=sys.stderr)

    tts = None
    if with_tts:
        # Constructed lazily here so `list` and `regenerate` don't
        # need ALSA/portaudio access — those subcommands run fine on
        # a laptop without an audio device.
        from ..audio_io import TtsPlayout
        device = _env("JASPER_TTS_DEVICE", "default")
        rate = int(_env("JASPER_TTS_OUTPUT_RATE", "48000"))
        tts = TtsPlayout(device, output_rate=rate)

    hostname = _hostname_from_url(management_url) or "this speaker"
    return AudioCueManager(
        sounds_dir=sounds_dir,
        hostname=hostname,
        voice=voice,
        backend=backend,
        tts_playout=tts,
    )


# --- subcommands ---


def _cmd_list(_args) -> int:
    mgr = _make_manager()
    rows = mgr.status()
    if not rows:
        print("no cues registered")
        return 0
    width_slug = max(len(r["slug"]) for r in rows)
    for r in rows:
        flag = "✓ cached" if r["cached"] else "✗ MISSING"
        print(f"{r['slug']:<{width_slug}}  {flag}")
        print(f"  text:     {r['rendered_text']}")
        print(f"  file:     {r['expected_filename']}")
        print(f"  desc:     {r['description']}")
        print()
    missing = [r["slug"] for r in rows if not r["cached"]]
    if missing:
        print(
            f"{len(missing)} cue(s) missing — run "
            "`jasper-cues regenerate` to bake them.",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_regenerate(args) -> int:
    mgr = _make_manager()
    try:
        written = mgr.regenerate(slug=args.cue, force=args.force)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"regen failed: {e}", file=sys.stderr)
        return 4
    if not written:
        print("all cues already cached, nothing to do")
    else:
        for slug in written:
            print(f"wrote {slug}")
    return 0


def _cmd_play(args) -> int:
    """Play a cached cue locally — same code path the daemon uses
    when it hits a failure state. Useful for previewing a phrasing
    change before deploying."""
    cue = find_cue(args.slug)
    if cue is None:
        print(f"error: unknown cue slug: {args.slug!r}", file=sys.stderr)
        return 2
    mgr = _make_manager(with_tts=True)
    try:
        ok = asyncio.run(mgr.play(args.slug))
    except KeyboardInterrupt:
        return 130
    if not ok:
        print(
            "play failed — see above logs. Common causes: "
            "no cached file (run regenerate), audio device unavailable.",
            file=sys.stderr,
        )
        return 1
    print(f"played {args.slug}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="jasper-cues",
        description="Manage the speaker's pre-rendered audio cues.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show all registered cues + cache status")

    p_regen = sub.add_parser(
        "regenerate",
        help="synthesise missing cue files (or --force to re-render all)",
    )
    p_regen.add_argument(
        "--cue", default=None,
        help="regenerate only this cue (default: all)",
    )
    p_regen.add_argument(
        "--force", action="store_true",
        help="re-render even if the cached file already exists",
    )

    p_play = sub.add_parser(
        "play", help="play a cached cue locally for testing",
    )
    p_play.add_argument("slug", help="cue slug (see `jasper-cues list`)")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        return _cmd_list(args)
    if args.cmd == "regenerate":
        return _cmd_regenerate(args)
    if args.cmd == "play":
        return _cmd_play(args)
    parser.error(f"unknown subcommand: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
