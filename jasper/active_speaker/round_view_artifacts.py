# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The tuning tool catalog (ADR-0393), and the round artifacts with the command that makes each."""
from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple
from jasper.audio_measurement.evidence_reasons import (
    REASON_REFUSED as REASON_REFUSED,
    REASON_UNREADABLE as REASON_UNREADABLE,
    REASON_UNWRITABLE as REASON_UNWRITABLE,
)
from .answer_schemas import ANSWER_SCHEMAS
from .run_manifest import RUN_MANIFEST_FILENAME
from .bench.replay import DSP_LEVELS_SCHEMA, DSP_REPLAY_SCHEMA
from .measurement_bass import BASS_VIEW_SCHEMA
from .measurement_programs import (
    PROGRAM_ROWS, PURPOSE_BASS, PURPOSE_REAR, PURPOSE_REFERENCE, PURPOSE_ROOM, PURPOSE_SPEAKER, RUNNABLE_PROGRAMS,
)
from .frequency_view import FREQUENCY_VIEW_FILENAME, SCHEMA as FREQUENCY_VIEW_SCHEMA
from .crossover_v2.evidence_packet.offline_reads import CLASSIFICATION_ARTIFACT, HARMONICS_ARTIFACT
from .crossover_v2.position_cycle import POSITION_CYCLE_FILENAME
from .crossover_v2.round_inputs import ROOM_ARTIFACT, RoundInputs, banked_round_of, recent_round_sessions

PROG = "jasper-round-views"
TAKES_THIS_ROUND = "<this-round>"
TAKES_THIS_BUNDLE = "<this-round's bundle>"
TAKES_SET = (TAKES_THIS_ROUND, "--set", "<set-id>")
TAKES_ONE_TAKE = (*TAKES_SET, "--take", "<take-id>")
TAKES_BEFORE_ANOTHER = (TAKES_THIS_ROUND, "<other-round>")

#: What a tool reads: the round's banked documents, a take's recording (a WAV it
#: decodes), or files that stay on the laptop (ADR-0353).
READS_RECORD = "record"
READS_RECORDING = "recording"
READS_LAPTOP = "laptop"
READS = (READS_RECORD, READS_RECORDING, READS_LAPTOP)

_PRESCRIBER = "jasper-crossover-prescriber"
_ROUND = "jasper-round"
_CABINET = ".venv/bin/python scripts/cabinet-model"
_PREVIEW_PROGRAMS = tuple(row.purpose for row in PROGRAM_ROWS if row.preview)


class CatalogRow(NamedTuple):
    """One tool an agent can call, or one round artifact and the command that makes it.

    A tool row states the ``question`` it answers, what it ``needs`` (pose,
    regime, take kind), what it ``reads`` (:data:`READS`), the ``programs``
    whose rounds it reads (empty: every program), its ``argv`` after the
    command, and the ``answer_fields`` its answer carries beside the envelope
    (ADR-0387). A view's row also says what not to use it for (``avoid``), as
    its ``--help`` renders from the row. ``schema`` names the shape of that
    answer and of the ``artifact`` it files; a script that prints text has none.
    ``in_artifact_dir`` marks an artifact the round's own evidence holds; every
    view files beside the round instead, never inside its evidence.
    ``producer`` names the command that makes an artifact no tool row answers for.
    ``bookkeeping`` names the purposes whose round publishes this view by
    itself, in :data:`BOOKKEEPING_ORDER`, through ``builder`` — this package's
    ``<module>.<function>`` answering ``(payload, provenance fields)``;
    ``packet`` is the analysis family carrying it in ``packet.json``.
    ``per_take`` marks a view that files one artifact per take it reads
    (:func:`~.crossover_v2.round_inputs.take_artifact_name`), so no round
    inventory row stands for it.
    """

    artifact: str = ""
    argv: tuple[str, ...] = (TAKES_THIS_ROUND,)
    in_artifact_dir: bool = False
    producer: str | None = None
    programs: tuple[str, ...] = ()
    bookkeeping: tuple[str, ...] = ()
    grades_against_base: bool = False
    builder: str | None = None
    packet: str | None = None
    schema: str = ""
    per_take: bool = False
    question: str = ""
    needs: str = ""
    avoid: str = ""
    reads: str = READS_RECORD
    answer_fields: tuple[str, ...] = ()

    @property
    def per_set(self) -> bool: return "<set-id>" in self.argv

ARTIFACT_BY_VIEW: dict[str, CatalogRow] = {
    "repeat": CatalogRow("repeatability.json", TAKES_BEFORE_ANOTHER, schema="jts_repeatability/1",
        question="How far apart are each driver's mark takes, within each round and between rounds?",
        needs="two or more rounds holding one driver's MEASURE takes at 0°/0° (speaker/mark, per_driver)",
        avoid="one set's repeated takes; repeat --set reads those",
        answer_fields=("drivers", "rounds")),
    "candidates": CatalogRow("candidates.json", schema="jts_candidates/2",
        question="Where do a round's candidates differ most, at each held pose and window?",
        needs="one round that played two or more candidates at each held pose",
        avoid="a round that played one candidate; compare reads two takes",
        answer_fields=("banked", "candidates", "max_abs_delta_band_hz", "max_abs_delta_between", "max_abs_delta_db",
                       "max_abs_delta_hz", "max_abs_delta_pose_key", "max_abs_delta_position_deg", "max_abs_delta_role",
                       "max_abs_delta_vertical_deg", "max_abs_delta_window", "omitted", "pairs", "poses", "round_dir",
                       "superseded_take_ids", "takes_naming_no_candidate")),
    "directivity": CatalogRow("directivity.json", TAKES_SET, programs=(PURPOSE_SPEAKER,), schema="jts_directivity/1",
        question="How do each spec band's level and shape change off axis, against the 0°/0° takes?",
        needs="one driver's set with 0°/0° takes and off-axis bearings (speaker/mark on baseline_express or baseline_full)",
        avoid="a set with no 0°/0° take, or the response at the seat; room reads that",
        answer_fields=("omitted_take_ids", "poses", "reference_take_ids", "role", "set_id")),
    "sweep --scope round": CatalogRow("gate_sweep.json", TAKES_SET, schema="jts_gate_sweep/2", reads=READS_RECORDING,
        question="Does each band's and feature's spread across poses grow with the gate (the room) or hold (the speaker)?",
        needs="takes at two or more poses of one round; --set narrows them to one set",
        avoid="one take's response; --scope take reads one take",
        answer_fields=("bands", "features", "ladder", "omitted", "poses", "rungs_ms", "scope")),
    "sweep --scope take": CatalogRow("window_view.json", TAKES_ONE_TAKE, schema=FREQUENCY_VIEW_SCHEMA, reads=READS_RECORDING,
        question="How does one take's response change through each gate of the ladder?",
        needs="one take by its id (jasper-round show lists them); --role picks a driver it recorded",
        avoid="the room-or-speaker verdict over poses; --scope round gives it",
        answer_fields=("capture_id", "image", "scope")),
    "impulse": CatalogRow("impulse.json", TAKES_ONE_TAKE, schema="jts_impulse/1", per_take=True, reads=READS_RECORDING,
        question="When does a take arrive, how clean is its onset, and how far is its peak above the noise?",
        needs="one take by its id; --role picks a driver it recorded, or summed",
        avoid="a frequency response; frequency reads that",
        answer_fields=("arrival_ms", "etc_db", "onset_before_peak_ms", "peak_to_noise_db", "polarity",
                       "reflection_free_ms")),
    "group-delay": CatalogRow("group_delay.json", TAKES_ONE_TAKE, schema="jts_group_delay/1", per_take=True,
                              reads=READS_RECORDING,
        question="What are a take's phase, group delay and excess group delay, band by band?",
        needs="one take by its id; --role picks a driver it recorded, or summed",
        avoid="the delay between two drivers; delay-landscape finds it",
        answer_fields=("arrival_ms", "bands")),
    "decay": CatalogRow("decay.json", TAKES_ONE_TAKE, schema="jts_decay/1", per_take=True, reads=READS_RECORDING,
        question="How fast does a take's sound decay in each octave (EDT, T20 and T30)?",
        needs="one take by its id; an ungated seat take (room/seat) reads the room",
        avoid="a driver's own ringing or a waterfall; it reads the room's decay by octave",
        answer_fields=("arrival_ms", "bands", "kept_after_onset_ms")),
    "compare": CatalogRow("compare.json", ("<round-a>", TAKES_THIS_ROUND, "--a-take", "<take-id>", "--b-take", "<take-id>"),
                          schema="jts_compare/2", per_take=True, reads=READS_RECORDING,
        question="How does take B differ from take A, or from a forecast, through one window and smoothing?",
        needs=("two takes by their ids, from one round or two; one take and its comparand (no --a-* flag); "
               "or one take and a judge --preview --out forecast"),
        avoid="more than two sides; frequency overlays whole rounds",
        answer_fields=("bands", "basis", "bins", "comparand", "level_offset_db", "max_abs_db", "max_abs_hz",
                       "mean_abs_db", "relative_arrival_ms", "rms_db", "same_recording")),
    "frequency": CatalogRow(FREQUENCY_VIEW_FILENAME, bookkeeping=(PURPOSE_ROOM, PURPOSE_BASS, PURPOSE_REAR),
                            builder="round_bookkeeping.frequency", schema=FREQUENCY_VIEW_SCHEMA,
        question="What frequency response did each take bank, for one or two rounds, bundles or documents?",
        needs="one or two banked rounds, session bundles or JSON documents whose takes banked curves",
        avoid="a take's impulse or phase; impulse and group-delay read those",
        answer_fields=("image", "runs", "series")),
    # The packet owns these two names, so the rows take those constants rather
    # than a second spelling of them.
    "distortion": CatalogRow(HARMONICS_ARTIFACT, programs=(PURPOSE_SPEAKER,), schema="jts_harmonic_distortion/4",
                             reads=READS_RECORDING,
        question="How much H2 and H3 did each driver make, at the drive each MEASURE capture used?",
        needs="a banked round's MEASURE captures of each driver (speaker/mark, per_driver)",
        avoid="bass takes; bass reads their H2/H3",
        answer_fields=("blocks", "captures_read", "captures_refused", "orders")),
    "dsp-replay": CatalogRow("dsp_replay.json", ("<graph.yml>", "<stimulus.wav>", "--main-db", "<db>", "--out", "<render-dir>"),
                             schema=DSP_REPLAY_SCHEMA, reads=READS_LAPTOP,
        question="What does a graph play for a stimulus, rendered through the native DSP with no audio device?",
        needs="a CamillaDSP graph, a PCM16 stimulus WAV and the native DSP binary; no round",
        avoid="measured sound: it renders the graph's digital output only",
        answer_fields=("output",)),
    "dsp-levels": CatalogRow("dsp_levels.json", ("<dsp_replay.json>", "--raw", "<output.f64le>", "--window-s", "<start>", "<stop>"),
                             schema=DSP_LEVELS_SCHEMA, reads=READS_LAPTOP,
        question="What are a dsp-replay render's digital bass-band levels over one time window?",
        needs="a dsp-replay render: its dsp_replay.json and the copied output.f64le",
        avoid="a render dsp-replay did not write; run dsp-replay first",
        answer_fields=("channels",)),
    "classify-features": CatalogRow(CLASSIFICATION_ARTIFACT, programs=(PURPOSE_SPEAKER,), schema="jts_feature_classification/3",
        question="Is a feature in the response a driver defect, an interference, or the room?",
        needs="a round's kept verify or lateral speaker takes, each with the impulse it kept",
        avoid="a round with no kept verify or lateral speaker take",
        answer_fields=("captures", "classifiable_band_hz", "features")),
    "delay-landscape": CatalogRow("delay_landscape.json", programs=(PURPOSE_SPEAKER,), schema="jts_delay_landscape/1",
        question="Which branch delay sums the two drivers best through the crossover?",
        needs="a take with both drivers' curves at one pose (speaker/mark) and a crossover corner, applied or --fc-hz",
        avoid="a take that holds only one driver's curve",
        answer_fields=("best_coordinate_us", "confirmation_coordinates_us", "next", "phase", "phase_composition",
                       "take_path")),
    "room": CatalogRow(ROOM_ARTIFACT, TAKES_SET, programs=(PURPOSE_ROOM,), bookkeeping=(PURPOSE_ROOM,),
                       builder="round_bookkeeping.room", packet="room", schema="jts_room/2",
        question="What is the room's median response at the seats, with its ceiling, lasting features and incumbent?",
        needs="one set of summed takes at the seat poses (room/seat or rear/seat)",
        avoid="the speaker's own response; directivity and frequency read that",
        answer_fields=("ceiling_hz", "coverage_hz", "features", "incumbent", "incumbent_reason", "n_positions",
                       "set_id", "spatial_support")),
    "room-grade": CatalogRow("room_grade.json", TAKES_SET, programs=(PURPOSE_ROOM,), bookkeeping=(PURPOSE_ROOM,),
                             grades_against_base=True, builder="round_bookkeeping.room_grade", packet="room",
                             schema="jts_room_grade/3",
        question="Does a room set lose any band against its incumbent from the same run?",
        needs="a room set and its incumbent set from one run, each with its room view (room/seat with candidates)",
        avoid="a set with no incumbent in its run; room reads one set",
        answer_fields=("bands", "ceiling_hz", "ceiling_source", "comparison", "evidence", "graph_scopes",
                       "graph_scopes_source", "incumbent", "incumbent_evidence", "incumbent_reason", "incumbent_set_id",
                       "ladder", "n_positions", "regressed_bands", "set_id", "spatial_support")),
    "bass": CatalogRow("bass_view.json", TAKES_SET, programs=(PURPOSE_BASS,), bookkeeping=(PURPOSE_BASS,),
                       builder="round_bookkeeping.bass", packet="bass", schema=BASS_VIEW_SCHEMA,
        question="What are each bass take's response, quiet-window SNR and H2/H3?",
        needs="one bass set: summed bass sweeps through a candidate graph (bass/axis)",
        avoid="the response above the bass band; frequency reads the full band",
        answer_fields=("takes",)),
    "bass-compare": CatalogRow("bass_comparison.json", ("<before-round>", TAKES_THIS_ROUND, "--before-set", "<before-set-id>",
                                                        "--after-set", "<set-id>", "--change", "<change>"),
                               programs=(PURPOSE_BASS,), packet="bass", schema="jts_bass_comparison/3",
        question="How did the bass change between two sets, across one candidate, volume, demand or diagnostic change?",
        needs="two bass sets whose bass views are filed, before and after the change",
        avoid="sets whose bass views are not filed yet; run bass on each first",
        answer_fields=("bands", "comparison", "context", "ladder")),
    "bass-fit-table": CatalogRow("bass_table.json", (TAKES_THIS_ROUND, "--candidate", "<candidate.json>"),
                                 programs=(PURPOSE_BASS,), packet="bass", schema="jts_bass_run_table/2",
        question="How much reach, drive and headroom does each bass candidate have at each level?",
        needs="bass rounds with their bass views: each candidate's takes beside its baseline's at every level",
        avoid="rounds with no candidate takes beside their baseline's",
        answer_fields=("level_count", "levels", "run_ids")),
    "inventory": CatalogRow("inventory.json", TAKES_SET, bookkeeping=(PURPOSE_SPEAKER, PURPOSE_ROOM, PURPOSE_BASS, PURPOSE_REAR),
                            builder="round_bookkeeping.inventory", schema="jts_inventory/1",
        question="Which analysis artifacts does a round have, and which command makes each missing one?",
        needs="any banked round or live session bundle",
        avoid="what a round measured; jasper-round show lists its sets and takes",
        answer_fields=("bytes_total", "latest_agent_note", "missing", "present", "program", "total",
                       "unavailable_repairs")),
    "nearfield": CatalogRow("nearfield_view.json", programs=(PURPOSE_REFERENCE,), schema="jts_nearfield_view/1",
        question="What does each driver radiate close up, band by band and per distance, and does its step match a piston?",
        needs="each driver's takes alone: near field at 15 and 30 mm (nearfield/each) or at the mark (drivers/each)",
        avoid="far-field takes; frequency reads those",
        answer_fields=("drivers", "level_mismatches")),
    "run-manifest": CatalogRow(RUN_MANIFEST_FILENAME, in_artifact_dir=True, producer="plan_run.run_plan"),
    # The banker writes this view; agents read it in packet["rear"].
    "rear": CatalogRow(
        "rear_view.json", ("--run", "<run-id>"), producer="jasper-round wait", programs=(PURPOSE_REAR,),
        bookkeeping=(PURPOSE_REAR,), builder="round_view_builders.rear", packet="rear", schema="jts_rear_view/2",
    ),
    # The banker writes this index; inventory reports its presence.
    "position-cycle": CatalogRow(POSITION_CYCLE_FILENAME, ("--run", "<run-id>"), producer="jasper-round wait"),
}

#: The run order of the views a finished round publishes: ``room-grade``
#: grades the median ``room`` wrote, so this is a data dependency.
BOOKKEEPING_ORDER = ("room", "room-grade", "bass", "rear", "frequency", "inventory")
#: The analysis families, in the order ``packet.json`` carries their keys.
PACKET_FAMILIES = tuple(dict.fromkeys(row.packet for row in map(ARTIFACT_BY_VIEW.__getitem__, BOOKKEEPING_ORDER) if row.packet))

#: Every tool an agent can call, by the command that runs it: the views, then
#: the reads of the other tuning CLIs, then the laptop scripts.
CATALOG: dict[str, CatalogRow] = {
    **{f"{PROG} {name}": row for name, row in ARTIFACT_BY_VIEW.items() if row.producer is None},
    f"{PROG} repeat --set": CatalogRow("repeat.json", ("<set-id>", TAKES_THIS_ROUND), programs=(PURPOSE_SPEAKER,),
                                       schema=ANSWER_SCHEMAS["repeat --set"],
        question="How much do one set's repeated takes vary in delay, polarity, ripple and trims?",
        needs="one set with two or more takes at one 0°/0° pose, each with its banked analysis (speaker/mark)",
        avoid="comparing rounds; repeat without --set reads those",
        answer_fields=("floor", "mark_pairs", "roles", "set_id", "take", "take_ids")),
    f"{PROG} speaker-fit": CatalogRow(argv=TAKES_SET, programs=(PURPOSE_SPEAKER,), schema=ANSWER_SCHEMAS["speaker-fit"],
        question="Which driver filters does the fit propose, and which alignment and trims did the round bank?",
        needs="one speaker/mark set with each driver's take at the mark (per_driver)",
        avoid="a set with no per-driver take at the mark",
        answer_fields=("alignment", "boost_evidence", "linearization", "set_id", "take_id", "trim_decision")),
    f"{_PRESCRIBER} judge --preview": CatalogRow(argv=("<document.json>", "--round", TAKES_THIS_ROUND, "--set", "<set-id>"),
                                                 programs=_PREVIEW_PROGRAMS, reads=READS_RECORDING,
                                                 schema=ANSWER_SCHEMAS[f"{_PRESCRIBER} judge --preview"],
        question="What would a prescription document's sections do, predicted from a round without playing?",
        needs="a document and its round: branches/express for driver or blend, room/seat for room, rear/pair for rear",
        answer_fields=("adopted", "banked", "compiled_stage", "preview", "section", "sections")),
    f"{_PRESCRIBER} judge --preview --vary": CatalogRow(
        argv=("<path=value,value>", "<document.json>", "--round", TAKES_THIS_ROUND, "--set", "<set-id>", "--out-dir", "<dir>"),
        programs=_PREVIEW_PROGRAMS, reads=READS_RECORDING, schema=ANSWER_SCHEMAS[f"{_PRESCRIBER} judge --preview --vary"],
        question="How does a preview change over a grid of a document's values, without playing?",
        needs="what judge --preview needs, one --vary axis per parameter, and a directory for the variants",
        answer_fields=("adopted", "banked", "section", "variants")),
    f"{_PRESCRIBER} contract": CatalogRow(argv=("--round", TAKES_THIS_ROUND, "--section", "<program>"),
                                          programs=RUNNABLE_PROGRAMS, schema=ANSWER_SCHEMAS[f"{_PRESCRIBER} contract"],
        question="What may a prescription document write for a program: its schema and bounds, evaluated on a round?",
        needs="nothing; --round evaluates the bounds on that round",
        answer_fields=("sections",)),
    f"{_PRESCRIBER} status": CatalogRow(argv=(), schema=ANSWER_SCHEMAS[f"{_PRESCRIBER} status"],
        question="Where does tuning stand: applied layers, the last banked rounds and the next program?",
        needs="nothing; a round directory adds its evidence packet",
        answer_fields=("applied", "banked", "context_error", "contracts", "declared", "driver_caps_live", "last_banked",
                       "latest_agent_note", "next", "next_commands", "packet_contracts", "packet_error",
                       "packet_fingerprint", "reading_order", "recent_rounds", "seat_level_reference_volume_db",
                       "selected_round", "speaker")),
    f"{_ROUND} list": CatalogRow(argv=("--program", "<program>"), programs=RUNNABLE_PROGRAMS,
                                 schema=ANSWER_SCHEMAS[f"{_ROUND} list"],
        question="Which rounds are banked, newest first, with their program, result and applied identity?",
        needs="nothing; --program narrows the list to one program's rounds",
        answer_fields=("rounds", "truncated")),
    f"{_ROUND} show": CatalogRow(schema=ANSWER_SCHEMAS[f"{_ROUND} show"],
        question="Which sets and selected takes does a banked round hold, by the ids the views take?",
        needs="a banked round id from jasper-round list, or a round directory",
        answer_fields=("applied_identity", "banked_at", "layout", "program", "purposes", "result", "round_dir",
                       "round_id", "sets")),
    f"{_ROUND} presets": CatalogRow(argv=("--json",), schema=ANSWER_SCHEMAS[f"{_ROUND} presets"],
        question="Which measurement presets exist, what does each play, and what do its layouts cost here?",
        needs="nothing",
        answer_fields=("presets",)),
    f"{_CABINET}/bem-transfer.py": CatalogRow(argv=("--case", "<case-dir>", "--out", "<transfer.npz>"),
                                              programs=(PURPOSE_REFERENCE, PURPOSE_REAR), reads=READS_LAPTOP,
        question="How does each woofer's near-field pressure carry to the far field, from a solved cabinet model?",
        needs="a solved Boundary Lab case of the cabinet from the CAD repo, and each woofer's step from the nearfield view"),
    f"{_CABINET}/predict.py": CatalogRow(argv=("--transfer", "<transfer.npz>", "--nearfield", "<nearfield_view.json>"),
                                         programs=(PURPOSE_REFERENCE, PURPOSE_REAR), reads=READS_LAPTOP,
        question="What does the woofer pair make with no room and at a seat before a wall, and do far-field takes agree?",
        needs="bem-transfer.py's output and a nearfield/each round's nearfield view; --farfield adds a drivers/each one"),
    f"{_CABINET}/rear-design.py": CatalogRow(
        argv=("--transfer", "<transfer.npz>", "--nearfield", "<nearfield_view.json>", "--out", "<prescription.json>"),
        programs=(PURPOSE_REAR,), reads=READS_LAPTOP,
        question="Which rear stage gives the smoothest response at the seat, the wall behind the speaker included?",
        needs="bem-transfer.py's output and a nearfield/each round's nearfield view"),
}

INVENTORY_ARTIFACT = ARTIFACT_BY_VIEW["inventory"].artifact


def view_rows(view: str) -> dict[str, CatalogRow]:
    """The catalog rows of one ``jasper-round-views`` subcommand, by command, one per mode it answers in."""
    return {command: row for command, row in CATALOG.items() if command.split()[:2] == [PROG, view]}


def context_artifacts(inputs: RoundInputs, round_dir: Path) -> dict[str, Any]:
    """Paths and sizes only; optional agent prose never becomes measurement data."""
    bundles = recent_round_sessions(inputs.session_dir)
    latest_note = next((
        path
        for bundle in bundles
        for path in dict.fromkeys((
            (banked_round_of(bundle) or bundle) / "agent_notes.md",
            bundle / "agent_notes.md",
        ))
        if path.is_file()
    ), None)
    return {
        key: {
            "path": str(path) if path else None,
            "present": path is not None and path.is_file(),
            "bytes": path.stat().st_size if path and path.is_file() else None,
        }
        for key, path in (("latest_agent_note", latest_note),)
    }


def read_purposes(purposes: tuple[str, ...], *, has_room: bool = False) -> set[str]:
    """The purposes whose views read a round of these purposes: a speaker round
    that kept its summed stops as room sets is read by the room views too."""
    return set(purposes) | ({PURPOSE_ROOM} if purposes[0] == PURPOSE_SPEAKER and has_room else set())


def bookkeeping_views(purposes: tuple[str, ...], *, has_room: bool = False) -> tuple[tuple[str, bool, bool], ...]:
    """View name, per-set scope, and whether it grades against the base, for a round of these purposes."""
    purpose = purposes[0]
    wanted = read_purposes(purposes, has_room=has_room)
    rows = ((name, ARTIFACT_BY_VIEW[name]) for name in BOOKKEEPING_ORDER)
    # A speaker round takes its frequency view from the packet writer, not here.
    return tuple((name, row.per_set, row.grades_against_base) for name, row in rows
                 if wanted.intersection(row.bookkeeping) and (purpose != PURPOSE_SPEAKER or name != "frequency"))
