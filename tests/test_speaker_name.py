from __future__ import annotations

import pytest

from jasper.speaker_name import (
    DEFAULT_SPEAKER_NAME,
    ENV_VAR,
    SpeakerNameError,
    read_state,
    runtime_name,
    validate_name,
    write_state,
)
from jasper.speaker_name_discovery import _display_name_candidates


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


def test_state_file_round_trips_spaces_and_apostrophes(tmp_path):
    path = tmp_path / "speaker_name.env"
    saved = write_state("Jasper's Room #2", str(path))

    assert saved == "Jasper's Room #2"
    assert path.read_text() == 'JASPER_SPEAKER_NAME="Jasper\'s Room #2"\n'
    state = read_state(str(path))
    assert state.name == "Jasper's Room #2"
    assert state.source == "state"


def test_missing_state_defaults_to_jts(tmp_path):
    state = read_state(str(tmp_path / "missing.env"))
    assert state.name == DEFAULT_SPEAKER_NAME
    assert state.source == "default"


def test_runtime_name_uses_environment_before_state(tmp_path):
    path = tmp_path / "speaker_name.env"
    write_state("Kitchen", str(path))
    assert runtime_name(environ={ENV_VAR: "Living Room"}, path=str(path)) == "Living Room"


def test_raop_instance_name_candidate_strips_mac_prefix():
    candidates = _display_name_candidates(
        "AABBCCDDEEFF@Living Room._raop._tcp.local.",
        "_raop._tcp.local.",
    )
    assert "Living Room" in candidates
