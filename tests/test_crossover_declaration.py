"""What ``/sound`` declares, what a candidate crosses at, and the difference.

These pin ``jasper.active_speaker.crossover_declaration`` — the module that
decides whether an apply owes the Sound declaration a write, and refuses one
that would put a crossover below the protected driver's declared floor.

Two properties are load-bearing enough to name:

* **A candidate measured where Sound declares asks for nothing.** That is every
  shipped session, and it is what keeps the apply path byte-identical to what
  it did before this seam existed. A regression here would start writing the
  declaration on ordinary applies.
* **The preset/declaration vocabularies are inverses, verified rather than
  asserted.** ``staging`` compiles ``filter_type``/``slope_db_per_octave`` into
  ``target_type``/``order``; this module reads them back the other way. If the
  two ever disagree, a declaration write would land a crossover that recompiles
  into a different preset — and ``baseline_profile``'s whole-preset equality
  guard would block the apply with nothing on screen explaining why.
"""

from dataclasses import dataclass, replace

import pytest

from jasper.active_speaker.crossover_declaration import (
    CROSSOVER_BELOW_DECLARED_FLOOR,
    CROSSOVER_DECLARATION_CHANGE_KIND,
    CROSSOVER_DECLARATION_CHANGE_SCHEMA_VERSION,
    CrossoverBelowDeclaredFloor,
    CrossoverGeometry,
    assert_crossover_honours_declared_floor,
    change_from_record,
    change_to_record,
    declaration_change_for_candidate,
    declared_crossover_geometry,
    matching_declared_candidate_index,
    preset_crossover_geometry,
)
from jasper.active_speaker.profile import CrossoverRegion, DriverSpec
from jasper.active_speaker.declaration_vocabulary import (
    _slope_to_lr_order,
    declaration_filter_type,
    declaration_slope_db_per_octave,
    same_declared_filter_type,
)


@dataclass(frozen=True)
class _Preset:
    """The two fields every reader in this module actually touches.

    A real ``ActiveSpeakerPreset`` needs a topology, a channel map and a valid
    driver set to construct; none of that participates in the crossover/floor
    comparison, and building it would make these tests fail for reasons that
    have nothing to do with what they assert.
    """

    crossover_regions: tuple
    drivers: dict


def _region(*, fc_hz=2500.0, order=4, target_type="LinkwitzRiley",
            lower="woofer", upper="tweeter") -> CrossoverRegion:
    return CrossoverRegion(
        id=f"{lower}_{upper}_{int(round(fc_hz))}hz",
        lower_driver=lower, upper_driver=upper, fc_hz=fc_hz,
        target_type=target_type, order=order,
    )


def _preset(*regions, floor_hz=None) -> _Preset:
    return _Preset(
        crossover_regions=tuple(regions),
        drivers={
            "woofer": DriverSpec(role="woofer", manufacturer="m", model="w"),
            "tweeter": DriverSpec(
                role="tweeter", manufacturer="m", model="t",
                protection_highpass_floor_hz=floor_hz,
            ),
        },
    )


def _draft(*, fc_hz=2500.0, slope=24.0, filter_type="Linkwitz-Riley",
           roles=("woofer", "tweeter"), extra=None) -> dict:
    entry = {
        "between_roles": list(roles),
        "frequency_hz": fc_hz,
        "filter_type": filter_type,
        "slope_db_per_octave": slope,
    }
    if extra is not None:
        entry.update(extra)
    return {"manual_settings": {"crossover_candidates": [entry]}}


# --- the preset/declaration vocabularies are inverses ------------------------


@pytest.mark.parametrize("order", [2, 4, 8])
def test_declaration_slope_round_trips_through_stagings_own_compiler(order):
    """The inverse is VERIFIED, not asserted.

    ``order * 6`` is only the right declared slope while ``staging`` still
    reads that number back as this order. Pinning the round trip rather than
    the arithmetic means a change to the compiler's accepted set fails here
    instead of silently producing a declaration that compiles to a different
    filter.
    """
    slope = declaration_slope_db_per_octave(order)
    assert slope == float(order) * 6.0
    assert _slope_to_lr_order(slope) == order


@pytest.mark.parametrize("order", [0, 1, 3, 5, 6, -4, None, True, 4.0, "4"])
def test_declaration_slope_refuses_an_order_staging_cannot_compile(order):
    """An order outside 2 / 4 / 8 has no declared spelling — and gets none.

    Inventing one would put a slope into ``/sound`` that the next compile
    refuses, which surfaces to the household as a blocked apply with a correct
    declaration on screen.
    """
    assert declaration_slope_db_per_octave(order) is None


def test_declaration_filter_type_round_trips_and_refuses_the_unsupported():
    assert declaration_filter_type("LinkwitzRiley") == "Linkwitz-Riley"
    assert same_declared_filter_type(
        declaration_filter_type("LinkwitzRiley"), "LinkwitzRiley")
    assert declaration_filter_type("Butterworth") is None
    assert declaration_filter_type(None) is None


@pytest.mark.parametrize("spelling", ["LR", "lr", "linkwitz riley", "Linkwitz-Riley"])
def test_a_households_own_spelling_is_not_a_crossover_change(spelling):
    """Every spelling `staging` compiles is the same filter to this seam.

    Comparing the raw strings would make an ordinary apply rewrite `/sound` and
    stash an Undo record for a change nobody made — a household who typed "LR"
    into their draft would get their crossover "changed" to itself.
    """
    assert declaration_change_for_candidate(
        source_preset=_preset(_region()), design_draft=_draft(filter_type=spelling)
    ) is None


# --- reading one preset ------------------------------------------------------


def test_preset_geometry_speaks_the_declarations_vocabulary():
    roles, geometry = preset_crossover_geometry(_preset(_region()))
    assert roles == ("woofer", "tweeter")
    assert geometry == CrossoverGeometry(
        fc_hz=2500.0, filter_type="Linkwitz-Riley", slope_db_per_octave=24.0
    )


@pytest.mark.parametrize("preset", [
    pytest.param(_preset(), id="no_regions"),
    pytest.param(
        _preset(_region(), _region(fc_hz=300.0, lower="subwoofer", upper="woofer")),
        id="two_regions",
    ),
    pytest.param(_preset(_region(order=3)), id="uncompilable_order"),
    pytest.param(_preset(_region(target_type="Butterworth")), id="unknown_type"),
])
def test_preset_geometry_refuses_what_it_cannot_state_as_a_declaration(preset):
    """Each of these is a real refusal, not a gap.

    A three-way's declaration has two entries and this seam writes one; an
    order or type Sound has no spelling for cannot be declared at all. The
    alternative to ``None`` in every case is guessing what ``/sound`` should
    say, which is how a declaration stops describing the graph.
    """
    assert preset_crossover_geometry(preset) is None


# --- reading the declaration -------------------------------------------------


def test_declared_geometry_reads_the_matching_entry():
    assert declared_crossover_geometry(_draft(), ("woofer", "tweeter")) == (
        CrossoverGeometry(
            fc_hz=2500.0, filter_type="Linkwitz-Riley", slope_db_per_octave=24.0
        )
    )


@pytest.mark.parametrize("draft", [
    pytest.param({}, id="no_manual_settings"),
    pytest.param({"manual_settings": {}}, id="no_candidates"),
    pytest.param(_draft(roles=("woofer", "midrange")), id="other_roles"),
    pytest.param(_draft(slope=None), id="no_slope"),
    pytest.param(_draft(filter_type=None), id="no_filter_type"),
    pytest.param(_draft(fc_hz=None), id="no_frequency"),
])
def test_declared_geometry_refuses_an_entry_it_cannot_fully_read(draft):
    """An entry missing any of the three fields is left alone, not completed.

    ``design_draft`` drops empty fields on save, so a partially-declared
    crossover is a real shape — and the honest response to it is to write
    nothing, because a slope nobody declared is a number nobody chose.
    """
    assert declared_crossover_geometry(draft, ("woofer", "tweeter")) is None


def test_two_entries_for_one_role_pair_are_ambiguous_not_first_wins():
    entry = _draft()["manual_settings"]["crossover_candidates"][0]
    assert matching_declared_candidate_index([entry, dict(entry)],
                                             ("woofer", "tweeter")) is None
    assert matching_declared_candidate_index([], ("woofer", "tweeter")) is None


# --- the difference, which is the instruction --------------------------------


def test_a_candidate_measured_where_sound_declares_asks_for_nothing():
    """The invariant that keeps every shipped apply unchanged.

    If this ever returns a change, ordinary applies start writing ``/sound``
    and stashing an Undo record for a change that was never made.
    """
    assert declaration_change_for_candidate(
        source_preset=_preset(_region()), design_draft=_draft()
    ) is None


def test_a_frequency_the_declaration_does_not_carry_is_a_change():
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2750.0)), design_draft=_draft()
    )
    assert change is not None
    assert change.between_roles == ("woofer", "tweeter")
    assert change.configured.fc_hz == 2500.0
    assert change.selected.fc_hz == 2750.0
    assert change.changes_frequency is True
    assert change.changes_slope is False


def test_a_slope_the_declaration_does_not_carry_is_also_a_change():
    """The half with no route before this seam.

    Slope compiles into ``CrossoverRegion.order``, which participates in
    ``baseline_profile``'s whole-preset equality guard exactly as ``fc_hz``
    does — so a candidate measured at another slope is as unapplyable as one
    measured at another corner until the declaration carries it.
    """
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(order=2)), design_draft=_draft()
    )
    assert change is not None
    assert change.changes_frequency is False
    assert change.changes_slope is True
    assert change.configured.slope_db_per_octave == 24.0
    assert change.selected.slope_db_per_octave == 12.0


def test_a_frequency_inside_the_declared_tolerance_is_the_same_corner():
    assert declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2500.04)), design_draft=_draft()
    ) is None


# --- the record Undo reads back ----------------------------------------------


def test_the_undo_record_round_trips_both_parameters():
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2750.0, order=2)), design_draft=_draft()
    )
    assert change is not None
    assert change_from_record(change_to_record(change)) == change


def test_the_record_carries_kind_and_schema_version():
    """The envelope :func:`change_to_record` writes, named as data.

    Mirrors :data:`~jasper.active_speaker.crossover_v2.driver_prescription.
    DRIVER_PRESCRIPTION_KIND`'s shape: a reader handed this record can tell
    what it is without guessing from its field names alone.
    """
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2750.0)), design_draft=_draft()
    )
    assert change is not None
    record = change_to_record(change)
    assert record["kind"] == CROSSOVER_DECLARATION_CHANGE_KIND
    assert record["artifact_schema_version"] == (
        CROSSOVER_DECLARATION_CHANGE_SCHEMA_VERSION
    )


@pytest.mark.parametrize("missing", [
    "kind", "artifact_schema_version",
    "between_roles", "applied_hz", "previous_hz", "applied_filter_type",
    "previous_filter_type", "applied_slope_db_per_octave",
    "previous_slope_db_per_octave",
])
def test_a_record_missing_any_field_reads_as_no_change_to_reverse(missing):
    """Strict on purpose, and it costs nothing.

    A partial record completed with a default would put a crossover into
    ``/sound`` that no accept chose. ``kind`` and ``artifact_schema_version``
    are on the same terms as the seven fields beside them here — missing ONE
    of the two, with the other present, is not the wholly-absent legacy shape
    (see ``test_a_pre_envelope_record_round_trips_instead_of_stranding_the_
    declaration`` for that one) and reads as ``None`` exactly like any other
    incomplete record.
    """
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2750.0)), design_draft=_draft()
    )
    assert change is not None
    record = change_to_record(change)
    del record[missing]
    assert change_from_record(record) is None


def test_a_record_naming_an_unsupported_schema_version_reads_as_no_change():
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2750.0)), design_draft=_draft()
    )
    assert change is not None
    record = change_to_record(change)
    record["artifact_schema_version"] = 2
    assert change_from_record(record) is None


def test_a_pre_envelope_record_round_trips_instead_of_stranding_the_declaration():
    """The retrofit contract, and the reason it exists.

    #2743 shipped :func:`change_to_record` three days before this envelope,
    and ``sound_declaration_undo`` is carried UNCONDITIONALLY across a deploy
    for as long as the applied graph is
    (``correction_crossover_v2.observe_apply_success`` /
    ``persist_conductor_state``) — so a record on a live speaker RIGHT NOW can
    be exactly this pre-envelope shape. Refusing it outright would strand a
    reader against a record an older build legitimately wrote.

    Generated from the CURRENT :func:`change_to_record` output with the two
    envelope keys removed, not hand-typed, so this is exactly the shape the
    merge-base wrote rather than a guess at it.
    """
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2750.0, order=2)), design_draft=_draft()
    )
    assert change is not None
    pre_envelope_record = change_to_record(change)
    del pre_envelope_record["kind"]
    del pre_envelope_record["artifact_schema_version"]
    assert change_from_record(pre_envelope_record) == change


@pytest.mark.parametrize("mutation", [
    {"kind": "nope"},
    {"artifact_schema_version": 2},
    {"kind": "nope", "artifact_schema_version": 2},
])
def test_a_record_naming_a_wrong_envelope_field_still_refuses(mutation):
    """A record that TRIED to speak the envelope and got it wrong is not the
    legacy shape, and is refused exactly like any other malformed field —
    the future-version case (an as-yet-unbuilt schema bump) included."""
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2750.0)), design_draft=_draft()
    )
    assert change is not None
    record = {**change_to_record(change), **mutation}
    assert change_from_record(record) is None


@pytest.mark.parametrize("keep", ["kind", "artifact_schema_version"])
def test_naming_only_one_envelope_field_is_not_the_legacy_shape(keep):
    """EITHER field present, even correctly, with the other missing, is not
    the wholly-absent shape the retrofit tolerates."""
    change = declaration_change_for_candidate(
        source_preset=_preset(_region(fc_hz=2750.0)), design_draft=_draft()
    )
    assert change is not None
    record = change_to_record(change)
    other = "artifact_schema_version" if keep == "kind" else "kind"
    del record[other]
    assert change_from_record(record) is None


def test_a_mangled_or_absent_record_still_reads_as_no_change():
    assert change_from_record({}) is None
    assert change_from_record({"kind": "nope"}) is None
    assert change_from_record(None) is None


def test_a_record_that_is_not_a_mapping_reads_as_no_change():
    assert change_from_record(None) is None
    assert change_from_record(["woofer", "tweeter"]) is None


# --- the hearing-safety boundary ---------------------------------------------


def test_a_crossover_at_the_declared_floor_is_allowed():
    """The 2026-08-17 owner ruling: a driver rated to cross at 2500 may.

    A strict comparison here would refuse the very crossover the manufacturer
    published, which is the nanny behaviour the never-nanny ruling excludes.
    """
    assert_crossover_honours_declared_floor(
        _preset(_region(fc_hz=2500.0), floor_hz=2500.0)
    )


def test_a_crossover_below_the_declared_floor_is_refused_by_name():
    with pytest.raises(CrossoverBelowDeclaredFloor) as excinfo:
        assert_crossover_honours_declared_floor(
            _preset(_region(fc_hz=2250.0), floor_hz=2500.0)
        )
    assert excinfo.value.reason == CROSSOVER_BELOW_DECLARED_FLOOR
    message = str(excinfo.value)
    assert "2250 Hz" in message and "2500 Hz" in message
    # The sentence tells the household what to DO, not merely what went wrong.
    assert "raise the crossover to at least 2500 Hz" in message


def test_a_driver_that_declares_no_floor_is_honoured_unchanged():
    """``None`` means the operator declared nothing — never a floor of zero,
    and never an invitation to substitute a class default."""
    assert_crossover_honours_declared_floor(
        _preset(_region(fc_hz=900.0), floor_hz=None)
    )


def test_a_declared_floor_with_no_readable_corner_is_refused():
    """Fail-closed: a floor that cannot be checked is not a floor that passed."""
    with pytest.raises(CrossoverBelowDeclaredFloor) as excinfo:
        assert_crossover_honours_declared_floor(
            _preset(_region(fc_hz=2500.0, upper="midrange"), floor_hz=2500.0)
        )
    assert excinfo.value.reason == CROSSOVER_BELOW_DECLARED_FLOOR
    assert "no crossover corner" in str(excinfo.value)


def test_the_strictest_corner_is_the_one_compared():
    """A tweeter high-passed by two regions is protected at the highest of
    them, so that is the number the floor is checked against."""
    preset = _preset(
        _region(fc_hz=2600.0),
        _region(fc_hz=2400.0, lower="midrange"),
        floor_hz=2500.0,
    )
    assert_crossover_honours_declared_floor(preset)
    lowered = replace(
        preset,
        crossover_regions=tuple(
            replace(region, fc_hz=2400.0) for region in preset.crossover_regions
        ),
    )
    with pytest.raises(CrossoverBelowDeclaredFloor):
        assert_crossover_honours_declared_floor(lowered)
