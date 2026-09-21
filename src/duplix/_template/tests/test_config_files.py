"""Sanity checks on the shipped config so a bad edit fails fast.

Uses only json / yaml (no pydantic or ortools) except one cross-check
against the shift tables in ``allocator/windows.py``.
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SHIFTS = {"M", "M1", "A", "A1", "N"}


def load_yaml():
    return yaml.safe_load((ROOT / "configs" / "config.yml").read_text(encoding="utf-8"))


def load_limits():
    return json.loads((ROOT / "configs" / "shift_limits.json").read_text(encoding="utf-8"))


def test_shift_limits_cover_every_shift_and_role():
    shifts = load_limits()["shifts"]
    assert set(shifts) == SHIFTS
    for shift, roles in shifts.items():
        assert set(roles) == {"STAFF", "ZC"}, shift


def test_shift_limit_bands_are_ordered_positive_ints():
    for shift, roles in load_limits()["shifts"].items():
        for role, band in roles.items():
            lo, target, hi = band["min"], band["target"], band["max"]
            for v in (lo, target, hi):
                assert isinstance(v, int) and v > 0, (shift, role)
            assert lo <= target <= hi, (shift, role, band)


def test_zc_bands_never_exceed_staff_bands():
    for shift, roles in load_limits()["shifts"].items():
        assert roles["ZC"]["max"] <= roles["STAFF"]["max"], shift


def test_solver_time_limit_is_positive():
    # config.yml warns that 0 makes CP-SAT search indefinitely.
    assert load_yaml()["solver"]["max_seconds"] > 0


def test_config_shifts_match_window_tables():
    from src.allocator.windows import SHIFT_NOMINAL_MIN

    assert set(load_yaml()["status"]["shifts"]) == set(SHIFT_NOMINAL_MIN) == SHIFTS


def test_gulf_drop_and_classification_lists_agree():
    cfg = load_yaml()
    assert set(cfg["io"]["sv_portal"]["routing_drop_dep_codes"]) == set(
        cfg["ops_class_gulf_dep_codes"]
    )


def test_international_codes_are_unique_three_letter_codes():
    codes = load_yaml()["io"]["sv_portal"]["international_airport_codes"]
    assert all(isinstance(c, str) and len(c) == 3 and c.isupper() for c in codes)
    assert len(codes) == len(set(codes))


def test_ops_class_letters_are_single_uppercase():
    for letter in load_yaml()["ops_class_by_aircraft_type"]:
        assert len(letter) == 1 and letter.isupper()
