# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether the cloud's null evidence bound this round's linearization fit.

* ``cloud-binding <round-dir>`` — re-fit this round's linearization with the
  cloud's null evidence CUT, and say whether that evidence actually BOUND the
  fit: by how much, and in which octave bands. It refuses rather than
  reporting when the all-inputs-wired refit does not reproduce the fit the
  round banked, and names a round whose MEASURE take predates the per-
  occurrence fit inputs as unevaluable instead of refitting it into an empty
  answer. Observed only: no grade moves, and it says nothing about whether
  the wired answer was RIGHT — nothing banked is ground truth for that.
  Writes ``cloud_binding.json``.
"""

from __future__ import annotations

import argparse

from jasper.active_speaker.crossover_v2.round_views import cloud_binding_view

from ._common import (
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _load_round,
    _view_out,
    _write,
    answer,
)

def _cmd_cloud_binding(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    view = cloud_binding_view(banked)
    written = _write(view.to_dict(), args.out, _view_out(args, banked))
    if not view.evaluable:
        summary = f"NOT EVALUATED ({view.not_evaluated_reason})"
    else:
        moved = [
            f"{role.role} {role.max_delta_db:.2f} dB"
            for role in view.roles if role.bound
        ]
        summary = (
            f"BOUND: {', '.join(moved)}" if moved else "NOT BOUND: no branch moved"
        ) + f"; refit vs banked {view.refit_vs_banked_db:.3f} dB"
    return answer(
        args.command, out=written, evaluable=view.evaluable,
        # Only inside the evaluable arm: a drifted refit keeps its per-role
        # numbers (:class:`CloudBindingView`), and the view declined to judge
        # binding, so a bound role read off them is one nothing decided.
        bound_roles=(
            [role.role for role in view.roles if role.bound]
            if view.evaluable else None
        ),
        refit_vs_banked_db=view.refit_vs_banked_db,
        not_evaluated_reason=view.not_evaluated_reason,
        line=(
            f"cloud-binding [observed only, no grade moves]: {summary}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    cloud_binding = sub.add_parser(
        "cloud-binding",
        help="re-fit with the cloud's null evidence cut, and say whether it bound the fit — observed only",
    )
    cloud_binding.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    cloud_binding.add_argument("--out", default=None, help="write the result here (- for stdout)")
    cloud_binding.set_defaults(func=_cmd_cloud_binding)
