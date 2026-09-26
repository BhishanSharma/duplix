"""The Setup sidebar's cap table writes ``configs/shift_limits.json``.

Every test works on a copy of the shipped file, so the real one is never
touched, and the module's cached defaults are restored afterwards.
"""

import json
import shutil
from pathlib import Path

import pytest

from src.allocator import caps
from src.schemas import Role

ROOT = Path(__file__).resolve().parents[1]
LIMITS = ROOT / "configs" / "shift_limits.json"


@pytest.fixture
def limits(tmp_path, monkeypatch):
    path = tmp_path / "shift_limits.json"
    shutil.copy(LIMITS, path)
    monkeypatch.setattr(caps, "_CONFIG_PATH", path)
    monkeypatch.setattr(caps, "_defaults", caps._load_defaults_from_disk())
    return path


def band(path, shift, role):
    return json.loads(path.read_text(encoding="utf-8"))["shifts"][shift][role]


def test_dump_keeps_the_hand_written_layout():
    text = LIMITS.read_text(encoding="utf-8")
    assert caps._dump_defaults(json.loads(text)) == text


def test_raise_cap_leaves_min_and_target(limits):
    before = band(limits, "M", "STAFF")
    notes = caps.set_default_caps({"M": {"STAFF": 26}})
    assert notes == []
    assert band(limits, "M", "STAFF") == {**before, "max": 26}
    assert caps.get_band("M", Role.STAFF)["max"] == 26  # cache reloaded


def test_cap_below_target_lowers_min_and_target(limits):
    notes = caps.set_default_caps({"N": {"STAFF": 18}})
    assert band(limits, "N", "STAFF") == {"min": 18, "target": 18, "max": 18}
    assert notes == ["N/STAFF: preferred 19 and target 20 lowered to 18"]


@pytest.mark.parametrize("payload", [
    {"X": {"STAFF": 20}},        # unknown shift
    {"M": {"AM": 20}},           # unknown role
    {"M": {"STAFF": 0}},         # not positive
    {"M": {"STAFF": 22.5}},      # not whole
    {"M": {"STAFF": True}},
    {"M": {"ZC": 30}},           # ZC above STAFF
])
def test_bad_caps_are_rejected_before_writing(limits, payload):
    before = limits.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        caps.set_default_caps(payload)
    assert limits.read_text(encoding="utf-8") == before
