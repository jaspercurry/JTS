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


def _make_manager(*, tts_playout=None) -> AudioCueManager:
    """Build a manager from environment variables. Same env names the
    daemon uses, so a CLI run and a daemon run agree on which file is
    canonical for a given cue.

    `tts_playout` is optional — `list` and `regenerate` don't need
    audio output, so they pass None. `play` constructs a TtsPlayout
    inside an `async with` block (so its underlying ALSA stream
    actually opens) and passes it in here."""
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

    hostname = _hostname_from_url(management_url) or "this speaker"
    return AudioCueManager(
        sounds_dir=sounds_dir,
        hostname=hostname,
        voice=voice,
        backend=backend,
        tts_playout=tts_playout,
    )


# --- subcommands ---


def _cmd_list(_args) -> int:
    mgr = _make_manager(tts_playout=None)
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
    mgr = _make_manager(tts_playout=None)
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
    """Play a cached cue by routing the request through the running
    jasper-voice daemon's control socket (via jasper-control's
    /cue/play HTTP endpoint).

    Why we don't play locally: the daemon's TtsPlayout has its gain
    set dynamically by TtsVolumeTracker to match current music
    level. A standalone CLI process can't easily replicate that math
    and would either play too loud (early version: ~20 dB hot) or
    fight the daemon's audio path on the same dmix. Routing through
    the daemon means the cue plays through the same audio chain,
    same gain, same ducking that real failure-triggered cues use."""
    cue = find_cue(args.slug)
    if cue is None:
        print(f"error: unknown cue slug: {args.slug!r}", file=sys.stderr)
        return 2

    import json as _json
    import urllib.error
    import urllib.request

    host = _env("JASPER_CONTROL_HOST", "127.0.0.1")
    port = int(_env("JASPER_CONTROL_PORT", "8780"))
    url = f"http://{host}:{port}/cue/play"
    body = _json.dumps({"slug": args.slug}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = _json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            data = {"error": str(e)}
        print(
            f"play request returned HTTP {e.code}: {data}",
            file=sys.stderr,
        )
        return 1
    except (urllib.error.URLError, ConnectionError) as e:
        print(
            f"could not reach jasper-control at {url}: {e}\n"
            f"is jasper-control running? "
            f"(`sudo systemctl status jasper-control`)",
            file=sys.stderr,
        )
        return 1
    if data.get("result") != "ok":
        print(f"play failed: {data}", file=sys.stderr)
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
        "play",
        help=(
            "play a cached cue through the running daemon's audio "
            "chain (so gain matches normal Jarvis voice level)"
        ),
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
