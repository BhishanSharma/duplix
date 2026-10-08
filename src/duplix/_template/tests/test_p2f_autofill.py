from src.allocator.p2f_autofill import (
    Candidate, autofill_handlers, handlers_needed, has_confirmed_p2f_licence,
)


def C(eid, role="STAFF", shift="M", lic="P2F/Corendon"):
    return Candidate(eid, eid.title(), role, shift, lic)


def test_licence_text():
    assert has_confirmed_p2f_licence("P2F/Corendon")
    assert has_confirmed_p2f_licence("P2F/")
    assert not has_confirmed_p2f_licence("/Corendon")
    assert not has_confirmed_p2f_licence("/")
    assert not has_confirmed_p2f_licence(None)
    assert not has_confirmed_p2f_licence("P2F (auto from roster cell)")


def test_picks_first_valid_on_matching_shift():
    pool = [C("a", shift="A"), C("b"), C("c")]
    assert [c.employee_id for c in autofill_handlers(pool, {"M": 1})["M"]] == ["b"]


def test_skips_zc_am_unlicensed_off_and_wrong_shift():
    pool = [C("zc", role="ZC"), C("am", role="AM"), C("nolic", lic="/Corendon"),
            C("off", shift=None), C("m1", shift="M1")]
    assert autofill_handlers(pool, {"M": 1}) == {}


def test_leaves_unfillable_shift_empty():
    out = autofill_handlers([C("a", shift="A")], {"M": 1, "A": 1})
    assert set(out) == {"A"}


def test_excluded_person_is_skipped():
    pool = [C("a"), C("b")]
    assert autofill_handlers(pool, {"M": 1}, excluded_ids={"a"})["M"][0].employee_id == "b"
    assert autofill_handlers([C("a")], {"M": 1}, excluded_ids={"a"}) == {}


def test_handlers_needed_is_one_per_eight():
    assert [handlers_needed(n) for n in (0, 1, 8, 9, 16, 17)] == [0, 1, 1, 2, 2, 3]


def test_multiple_handlers_for_a_busy_shift():
    pool = [C("a"), C("b"), C("c")]
    out = autofill_handlers(pool, {"M": handlers_needed(17)})
    assert [c.employee_id for c in out["M"]] == ["a", "b", "c"]


def test_only_the_shortfall_is_filled_and_manual_picks_are_kept():
    pool = [C("a"), C("b"), C("c")]
    out = autofill_handlers(pool, {"M": 2}, already={"M": ["b"]})
    assert [c.employee_id for c in out["M"]] == ["a"]
    assert autofill_handlers(pool, {"M": 1}, already={"M": ["b"]}) == {}


def test_too_few_qualified_fills_what_exists():
    out = autofill_handlers([C("a")], {"M": 3})
    assert [c.employee_id for c in out["M"]] == ["a"]
