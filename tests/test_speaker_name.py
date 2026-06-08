from __future__ import annotations

import pytest

from jasper.speaker_name import (
    DEFAULT_SPEAKER_NAME,
    ENV_VAR,
    ENV_VAR_ROOM,
    SpeakerNameError,
    SpeakerNameState,
    read_state,
    runtime_name,
    runtime_room,
    validate_name,
    validate_room,
    write_state,
)
from jasper.speaker_name_discovery import _display_name_candidates


def test_speaker_name_state_positional_name_first_room_defaults_empty():
    """Back-compat contract: `name` stays the first positional field so
    SpeakerNameState(name) keeps working; `room` joins as a defaulted
    field (the identity home is now name + room)."""
    st = SpeakerNameState("Kitchen")
    assert st.name == "Kitchen"
    assert st.room == ""  # defaulted — old positional construction unaffected
    # Room is a real second field when supplied.
    assert SpeakerNameState("Kitchen", "Upstairs").room == "Upstairs"


def test_validate_name_accepts_room_name_punctuation():
    assert validate_name("  Jasper's Kitchen-2  ") == "Jasper's Kitchen-2"
    assert validate_name("Living Room #2") == "Living Room #2"
    assert validate_name("A&B + C") == "A&B + C"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "-Kitchen",
        "Kitchen-",
        "Kitchen/Bedroom",
        "Kitchen$",
        "Jasper’s Room",
        "x" * 33,
    ],
)
def test_validate_name_rejects_unsafe_names(raw: str):
    with pytest.raises(SpeakerNameError):
        validate_name(raw)


def test_validate_room_allows_empty_for_unset():
    # An optional room: empty / whitespace normalizes to "" rather than raising.
    assert validate_room("") == ""
    assert validate_room("   ") == ""


def test_validate_room_reuses_name_rules():
    assert validate_room("  Living Room  ") == "Living Room"
    assert validate_room("Bedroom #2") == "Bedroom #2"
    with pytest.raises(SpeakerNameError):
        validate_room("Kitchen/Bedroom")


def test_state_file_round_trips_spaces_and_apostrophes(tmp_path):
    path = tmp_path / "speaker_name.env"
    saved = write_state("Jasper's Room #2", path=str(path))

    assert saved == "Jasper's Room #2"
    # Both keys are persisted; room defaults to "" when not supplied.
    assert path.read_text() == (
        'JASPER_SPEAKER_NAME="Jasper\'s Room #2"\n'
        'JASPER_SPEAKER_ROOM=""\n'
    )
    state = read_state(str(path))
    assert state.name == "Jasper's Room #2"
    assert state.room == ""
    assert state.source == "state"


def test_state_file_round_trips_name_and_room(tmp_path):
    path = tmp_path / "speaker_name.env"
    write_state("Kitchen", "Upstairs", path=str(path))
    state = read_state(str(path))
    assert state.name == "Kitchen"
    assert state.room == "Upstairs"
    assert state.source == "state"


def test_write_state_preserves_existing_room_on_name_only_save(tmp_path):
    """Back-compat: write_state(name) keeps the previously-stored room."""
    path = tmp_path / "speaker_name.env"
    write_state("Kitchen", "Upstairs", path=str(path))
    # Rename without re-passing the room — the room must survive.
    write_state("Living Room", path=str(path))
    state = read_state(str(path))
    assert state.name == "Living Room"
    assert state.room == "Upstairs"


def test_write_state_can_clear_room_with_empty_string(tmp_path):
    path = tmp_path / "speaker_name.env"
    write_state("Kitchen", "Upstairs", path=str(path))
    write_state("Kitchen", "", path=str(path))
    assert read_state(str(path)).room == ""


def test_read_state_parses_room_regardless_of_line_order(tmp_path):
    path = tmp_path / "speaker_name.env"
    # Room line BEFORE name line — read_state must still pick up both.
    path.write_text('JASPER_SPEAKER_ROOM="Den"\nJASPER_SPEAKER_NAME="Office"\n')
    state = read_state(str(path))
    assert state.name == "Office"
    assert state.room == "Den"


def test_read_state_invalid_room_falls_back_to_empty_keeps_name(tmp_path):
    path = tmp_path / "speaker_name.env"
    path.write_text('JASPER_SPEAKER_NAME="Office"\nJASPER_SPEAKER_ROOM="Bad/Room"\n')
    state = read_state(str(path))
    assert state.name == "Office"
    assert state.room == ""


def test_missing_state_defaults_to_jts(tmp_path):
    state = read_state(str(tmp_path / "missing.env"))
    assert state.name == DEFAULT_SPEAKER_NAME
    assert state.room == ""
    assert state.source == "default"


def test_runtime_name_uses_environment_before_state(tmp_path):
    path = tmp_path / "speaker_name.env"
    write_state("Kitchen", path=str(path))
    assert runtime_name(environ={ENV_VAR: "Living Room"}, path=str(path)) == "Living Room"


def test_runtime_room_env_first_then_state_then_empty(tmp_path):
    path = tmp_path / "speaker_name.env"
    write_state("Kitchen", "FromFile", path=str(path))
    # Env wins.
    assert runtime_room(environ={ENV_VAR_ROOM: "FromEnv"}, path=str(path)) == "FromEnv"
    # Blank env falls through to the file.
    assert runtime_room(environ={ENV_VAR_ROOM: "  "}, path=str(path)) == "FromFile"
    # No env, no file → "".
    assert runtime_room(environ={}, path=str(tmp_path / "missing.env")) == ""


def test_raop_instance_name_candidate_strips_mac_prefix():
    candidates = _display_name_candidates(
        "AABBCCDDEEFF@Living Room._raop._tcp.local.",
        "_raop._tcp.local.",
    )
    assert "Living Room" in candidates
