"""Uploads in other formats and with minor layout changes still read the
same (2026-10-10)."""

import io
from datetime import date, time

import openpyxl
import pytest

from src.config import load_config
from src.io.readers import (
    _parse_time,
    _resolve_date_columns,
    read_sv_portal,
    workbook_from_bytes,
)
from src.io.upload_formats import UnreadableUpload, sniff, to_xlsx_bytes

CFG = load_config("configs/config.yml")

HEADER = ["DATE", "FLT", "TYPE", "DEP", "ARR", "STD", "Booked Pax"]
ROWS = [
    [date(2026, 9, 26), 101, "J", "DEL", "BOM", time(5, 30), "0/0/180"],
    [date(2026, 9, 26), 202, "J", "BLR", "SIN", time(13, 25), "0/0/150"],
]


def _xlsx(rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _read(data):
    return sorted(
        (r.flight_id, r.departure, r.arrival, r.dep_time, r.date)
        for r in read_sv_portal(workbook_from_bytes(to_xlsx_bytes(data)), CFG)
    )


EXPECTED = _read(_xlsx([HEADER, *ROWS]))


def test_baseline_reads_flight_numbers_without_dot_zero():
    assert EXPECTED[0][0] == "101"


@pytest.mark.parametrize("delim", [",", "\t", ";"])
def test_csv_and_tsv(delim):
    lines = [delim.join(HEADER)] + [
        delim.join(["09/26/2026", str(r[1]), "J", r[3], r[4],
                    r[5].strftime("%H:%M"), r[6]])
        for r in ROWS
    ]
    data = "\n".join(lines).encode()
    assert sniff(data) == "text"
    assert _read(data) == EXPECTED


def test_html_table_saved_as_xls():
    cells = lambda r: "".join(f"<td>{c}</td>" for c in r)  # noqa: E731
    body = "".join(
        f"<tr>{cells(['09/26/2026', r[1], 'J', r[3], r[4], r[5].strftime('%H:%M'), r[6]])}</tr>"
        for r in ROWS
    )
    data = f"<html><table><tr>{cells(HEADER)}</tr>{body}</table></html>".encode()
    assert sniff(data) == "html"
    assert _read(data) == EXPECTED


def test_relabelled_headers_and_title_rows():
    relabelled = ["Flight Date", "Flight No.", "Type", "dep", "Arr", "STD (LT)", "booked_pax"]
    data = _xlsx([["CLC flight export"], [], relabelled, *ROWS])
    assert _read(data) == EXPECTED


def test_missing_required_column_is_refused_with_its_name():
    data = _xlsx([[h if h != "STD" else "Notes" for h in HEADER], *ROWS])
    with pytest.raises(ValueError, match="departure time"):
        _read(data)


def test_garbage_is_refused():
    with pytest.raises(UnreadableUpload):
        to_xlsx_bytes(b"")


@pytest.mark.parametrize("raw", [
    "05:30", "5:30", "05:30:00", "05.30", "0530", "530", "5:30 AM",
    0.22916666666666666, 530, time(5, 30),
])
def test_time_shapes(raw):
    assert _parse_time(raw) == time(5, 30)


@pytest.mark.parametrize("labels", [
    ["Mon,31Aug", "Tue,1Sep", "Wed,2Sep"],
    ["Mon, 31 Aug", "Tue, 1 Sep", "Wed, 2 Sep"],
    ["31-Aug", "1-Sep", "2-Sep"],
    ["31 Aug", "1 Sep", "2 Sep"],
    ["31/08", "01/09", "02/09"],
    ["08/31/2026", "09/01/2026", "09/02/2026"],
    ["31-Aug-26", "1-Sep-26", "2-Sep-26"],
])
def test_roster_date_header_shapes(labels):
    got = _resolve_date_columns(["S.no", "Name", "IGA", *labels], "%a,%d%b", 2026)
    assert list(got.values()) == [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)]


# ---------- roster year is worked out, not configured (2026-10-11) ----------

def _roster_wb(labels, modified):
    from datetime import datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ID", "STAFF NAME", *labels])
    ws.append(["10001", "TEST PERSON", *(["M"] * len(labels))])
    wb.properties.modified = datetime(*modified)
    return wb


def _roster_dates(wb):
    from src.io.readers import read_staff_roster
    (row,) = read_staff_roster(wb, CFG)
    return sorted(row.status_by_date)


def test_config_has_no_fixed_roster_year():
    assert CFG.allocation.cycle_year is None


def test_weekday_headers_cross_new_year_without_config():
    # 1 Jan 2027 is a Friday: read as 2027 although nothing says so.
    wb = _roster_wb(["Thu,31Dec", "Fri,1Jan", "Sat,2Jan"], (2026, 12, 20))
    assert _roster_dates(wb) == [date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)]


def test_plain_headers_take_the_files_year():
    wb = _roster_wb(["30-Mar", "31-Mar", "1-Apr"], (2028, 3, 1))
    assert _roster_dates(wb) == [date(2028, 3, 30), date(2028, 3, 31), date(2028, 4, 1)]
