# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-settings`` -- read or change a speaker setting from a shell.

One verb per ``/assistant/`` wizard page, each calling the owner function that
page calls, so a change from an agent is validated, logged and applied the way
a change from the page is (ADR-0350). ``--help`` is the contract (ADR-0204);
stdout is one JSON document and the exit codes are ``_refusal``'s (ADR-0237).
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Callable

from jasper import wake_models
from jasper.env_load import env_file_path
from jasper.logging_setup import configure_logging
from jasper.model_downloads import active_wake_model
from jasper.voice import model_discovery
from jasper.voice.catalog import PROVIDERS, VALID_PROVIDER_IDS
from jasper.voice.provider_state import (
    VoiceSelectionRefused,
    keys_set,
    offered_models,
    read_active_model_from_env_files,
    read_active_provider,
    read_active_provider_state,
    select_voice,
    voice_env_files,
)
from jasper.web._common import RestartOutcome, restart_voice_daemon

from ._refusal import (
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    answered,
    failed,
    refused,
)

PROG = "jasper-settings"
SUDO = f"sudo /opt/jasper/.venv/bin/{PROG}"

_EPILOG = f"""\
run it on the speaker as root:
  {SUDO} show
or from a laptop:
  ssh $PI_USER@$PI_HOST {SUDO} voice --provider openai

API keys never pass through here (argv shows in ps, shell history and agent
transcripts): save them at /assistant/voice/. `show` prints each key as "set"
or "unset" only.

A change restarts voice through the wizard's gates: with no provider selected,
or on a bonded follower, the change is saved and applies when voice next starts.

stdout is one JSON document:
  show           {{"voice": <voice>, "wake": <wake>}}
  voice          {{"provider", "model", "providers": {{ID: {{"key": "set"|"unset",
                 "model", "models": [ID, ...]}}}}}}
  wake           {{"model", "models": {{KEY: "available"|"not_downloaded"}}}}
  voice --provider/--model
                 {{"provider", "model", "changed": ["provider", "model"],
                 "restart": "ran"|"skipped", "restart_reason"}}
  wake --model   {{"model", "restart", "restart_reason"}}
  a failure      {{"status": "refused"|"unreadable"|"unwritable", "reason", "detail"}}

exit codes:
  0  answered; a change is saved, and voice restarted or its restart was
     skipped ("restart_reason": "provider_unset" or "bonded_follower")
  1  refused: not root, or an unknown or unusable value (nothing saved), or
     saved but the restart was refused ("restart_refused", detail.saved true)
  2  a settings file could not be read
  3  the change could not be saved
"""


def _voice_view() -> dict[str, Any]:
    files = voice_env_files()
    keys = keys_set(files)
    provider = read_active_provider_state().provider
    discovered = model_discovery.load_cache(model_discovery.DEFAULT_CACHE_PATH)
    providers = {
        p.id: {
            "key": "set" if p.key_env in keys else "unset",
            "model": read_active_model_from_env_files(p.id, files),
            "models": offered_models(p, discovered.get(p.id)),
        }
        for p in PROVIDERS
    }
    return {
        "provider": provider,
        "model": providers[provider]["model"] if provider else None,
        "providers": providers,
    }


def _wake_view() -> dict[str, Any]:
    active = active_wake_model(
        env={}, jasper_env_path=env_file_path(),
        wake_env_path=wake_models.WAKE_MODEL_FILE,
    )
    entry = wake_models.by_model(active)
    return {
        "model": entry.key if entry else active,
        "models": {
            e.key: "available" if wake_models.is_available(e) else "not_downloaded"
            for e in wake_models.REGISTRY
        },
    }


def _answer(view: Callable[[], dict[str, Any]]) -> int:
    try:
        document = view()
    except OSError as exc:
        return failed(EXIT_UNREADABLE, "unreadable", str(exc))
    return answered(document)


def _restart(saved: dict[str, Any]) -> int:
    outcome = restart_voice_daemon()
    if outcome is RestartOutcome.REFUSED:
        return refused(
            "restart_refused", {**saved, "saved": True}, exit_code=EXIT_REFUSED,
        )
    document = {**saved, "restart": outcome.value}
    if outcome is RestartOutcome.SKIPPED:
        # restart_voice_daemon's two gates, in its order.
        document["restart_reason"] = (
            "bonded_follower" if read_active_provider() else "provider_unset"
        )
    return answered(document)


def _show(args: argparse.Namespace) -> int:
    return _answer(lambda: {"voice": _voice_view(), "wake": _wake_view()})


def _voice(args: argparse.Namespace) -> int:
    if args.provider is None and args.model is None:
        return _answer(_voice_view)
    try:
        selection = select_voice(args.provider, args.model, via="cli")
    except VoiceSelectionRefused as exc:
        return refused(exc.reason, str(exc), exit_code=EXIT_REFUSED)
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, "save_failed", str(exc))
    return _restart({
        "provider": selection.provider,
        "model": selection.model,
        "changed": list(selection.changed),
    })


def _wake(args: argparse.Namespace) -> int:
    if args.model is None:
        return _answer(_wake_view)
    try:
        selection = wake_models.select_wake_model(args.model, via="cli")
    except wake_models.WakeModelRefused as exc:
        return refused(exc.reason, str(exc), exit_code=EXIT_REFUSED)
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, "save_failed", str(exc))
    return _restart({"model": selection.entry.key})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Read or change the speaker's settings the way its /assistant/ wizard "
            "pages do: one verb per page, calling the page's own owner function, "
            "so a change is validated, logged (event=voice.save, event=wake.model) "
            "and applied exactly as from the page. With no flags a verb only reads."
        ),
        epilog=_EPILOG,
    )
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="VERB")
    verbs.add_parser(
        "show", help="every page: its values, its choices, and each API key as set/unset",
    ).set_defaults(run=_show)
    voice = verbs.add_parser("voice", help="the /assistant/voice/ page: provider and model")
    voice.add_argument(
        "--provider", metavar="ID",
        help=f"make this provider active; one of: {', '.join(sorted(VALID_PROVIDER_IDS))}",
    )
    voice.add_argument(
        "--model", metavar="ID",
        help="a model the provider offers (listed by `voice`); default: the one it runs",
    )
    voice.set_defaults(run=_voice)
    wake = verbs.add_parser("wake", help="the /assistant/wake/ page: wake word")
    wake.add_argument(
        "--model", metavar="KEY",
        help=f"one of: {', '.join(entry.key for entry in wake_models.REGISTRY)}",
    )
    wake.set_defaults(run=_wake)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(fmt="%(message)s")
    if os.geteuid() != 0:
        return refused(
            "not_root", f"run it as root: {SUDO} {args.verb}", exit_code=EXIT_REFUSED,
        )
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
