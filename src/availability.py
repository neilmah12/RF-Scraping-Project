"""Parse RentFaster's `Availability Date` into a date and a vacancy flag.

DUPLICATED FILE. An identical copy lives in property-merge-pipeline as
`tools/availability.py`, because the two repos have no shared package and both
need this rule: map.json feeds the monthly snapshot there, listing-detail
captures feed the parsers here, and the two must agree or the same building
reads differently depending on which file you opened. Change one, change both,
and run `python3 src/availability.py` in each -- the self-check at the bottom
is the same table on both sides.

The values
----------
RentFaster returns display text, not a date. Measured across the 2026-08-13
map snapshot (1,270 of 1,290 listings populated, 27 distinct values) and the
listing-detail captures:

    Immediate       the common case, 1,019 of 1,270 in the map snapshot
    Sep 01, 2026    detail pages carry the year
    Sep 01          map.json does NOT -- see the year rule below
    Negotiable      not a date and not a vacancy claim
    No Vacancy      an explicit statement of none available

All 27 map values parse. Anything else is returned unresolved rather than
guessed at, and callers are expected to report it.

The year rule for yearless dates
--------------------------------
map.json drops the year, so "Sep 01" has to be resolved against the capture
date. The obvious rule -- a month-day already past means next year -- is wrong
near the boundary: a listing captured Aug 13 showing "Aug 01" is a suite that
came available two weeks ago and has not rented, not one available in eleven
and a half months. That is precisely the softness signal worth catching, and
the naive rule pushes it a year into the future where it reads as a building
with no current vacancy.

So a yearless date within PAST_WINDOW days before the capture stays in the
current year and comes back already past. Beyond that it rolls forward. The
2026-08-13 snapshot happens to contain no past dates (its earliest is Aug 14),
so this is a correctness guard rather than something the sample forced.
"""
from __future__ import annotations

import datetime
import re

PAST_WINDOW = datetime.timedelta(days=60)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Text that is not a date. "No Vacancy" is a real statement of none available;
# "Negotiable" says nothing either way and must not be read as a vacancy.
KEYWORDS = {"immediate": "immediate", "no vacancy": "none", "negotiable": ""}

_PATTERN = re.compile(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?$")


def parse(value, capture_date: str = "") -> tuple:
    """-> (ISO date or "", flag). Flag is immediate / dated / none / "".

    `dated` covers past dates as well as future ones. Use `is_available_now`
    to collapse that into a yes/no rather than comparing strings at the call
    site.
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    if text.lower() in KEYWORDS:
        return "", KEYWORDS[text.lower()]

    found = _PATTERN.match(text)
    if not found:
        return "", ""
    month = MONTHS.get(found.group(1).lower())
    if not month:
        return "", ""
    day = int(found.group(2))

    if found.group(3):
        year = int(found.group(3))
    else:
        today = _as_date(capture_date)
        year = today.year
        try:
            candidate = datetime.date(year, month, day)
        except ValueError:
            return "", ""
        if candidate < today - PAST_WINDOW:
            year += 1

    try:
        return datetime.date(year, month, day).isoformat(), "dated"
    except ValueError:
        return "", ""


def is_available_now(date: str, flag: str, capture_date: str = "") -> bool:
    """True when the suite is available at capture time.

    A date on or before the capture is available now, same as "Immediate" --
    the distinction between "empty since Aug 01" and "empty today" is one the
    caller can still make from `available_date`, but for a vacancy read they
    are the same state.
    """
    if flag == "immediate":
        return True
    if flag == "dated" and date:
        return datetime.date.fromisoformat(date) <= _as_date(capture_date)
    return False


def signal(rows: list[tuple], capture_date: str = "") -> tuple:
    """Roll (date, flag) pairs for one listing into a vacancy statement.

    -> (signal, n available now, earliest future date)

    `current`       at least one advertised suite type is available now
    `none_current`  every advertised suite type is future-dated, or the
                    listing states No Vacancy
    `""`            nothing parseable -- never read as either of the above

    The asymmetry is the point. `current` proves vacancy exists and says
    NOTHING about how much: the count is suite TYPES, not units, so one row
    may be a single empty suite or twelve. Read it as a floor of one.
    `none_current` is the more informative value -- nothing available today --
    but only across suite types the building chose to advertise, and a
    building with no listing at all is absent from the data entirely and must
    never be read as full.
    """
    now = sum(1 for date, flag in rows if is_available_now(date, flag, capture_date))
    future = sorted(
        date for date, flag in rows
        if flag == "dated" and date and not is_available_now(date, flag, capture_date)
    )
    if now:
        return "current", now, (future[0] if future else "")
    if future or any(flag == "none" for _, flag in rows):
        return "none_current", 0, (future[0] if future else "")
    return "", 0, ""


def _as_date(value: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return datetime.date.today()


CASES = [
    # (value, capture date, expected date, expected flag)
    ("Immediate", "2026-08-13", "", "immediate"),
    ("Sep 01, 2026", "2026-08-13", "2026-09-01", "dated"),
    ("Sept 15, 2026", "2026-08-13", "2026-09-15", "dated"),
    ("Oct. 01, 2026", "2026-08-13", "2026-10-01", "dated"),
    ("No Vacancy", "2026-08-13", "", "none"),
    ("Negotiable", "2026-08-13", "", ""),
    # yearless: map.json drops the year
    ("Sep 01", "2026-08-13", "2026-09-01", "dated"),
    ("Aug 14", "2026-08-13", "2026-08-14", "dated"),
    ("Dec 01", "2026-08-13", "2026-12-01", "dated"),
    ("Jan 01", "2026-08-13", "2027-01-01", "dated"),   # far past -> next year
    ("Aug 01", "2026-08-13", "2026-08-01", "dated"),   # just past -> stays put
    ("Jun 20", "2026-08-13", "2026-06-20", "dated"),   # inside the window
    ("Jun 10", "2026-08-13", "2027-06-10", "dated"),   # outside it
    # rejected rather than guessed
    ("Feb 30, 2026", "2026-08-13", "", ""),
    ("Xyz 01, 2026", "2026-08-13", "", ""),
    ("Available now", "2026-08-13", "", ""),
    ("", "2026-08-13", "", ""),
    (None, "2026-08-13", "", ""),
]


def _self_check() -> int:
    failures = 0
    for value, date, want_date, want_flag in CASES:
        got = parse(value, date)
        ok = got == (want_date, want_flag)
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {str(value)!r:16s} -> {got}"
              + ("" if ok else f"   expected {(want_date, want_flag)}"))

    checks = [
        (signal([parse("Immediate", "2026-08-13")], "2026-08-13"), ("current", 1, "")),
        (signal([parse("Sep 01", "2026-08-13")], "2026-08-13"), ("none_current", 0, "2026-09-01")),
        (signal([parse("Aug 01", "2026-08-13"), parse("Sep 01", "2026-08-13")], "2026-08-13"),
         ("current", 1, "2026-09-01")),
        (signal([parse("No Vacancy", "2026-08-13")], "2026-08-13"), ("none_current", 0, "")),
        (signal([parse("Available now", "2026-08-13")], "2026-08-13"), ("", 0, "")),
        (signal([], "2026-08-13"), ("", 0, "")),
    ]
    for got, want in checks:
        ok = got == want
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} signal -> {got}" + ("" if ok else f"   expected {want}"))
    print("PASS" if not failures else f"{failures} FAILURE(S)")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _self_check() else 0)
