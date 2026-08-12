"""
Capture QC — per-section validator for manual Rentfaster map.json captures
==========================================================================
Run this on every capture file the moment it comes off the browser, before
ingesting anything. It catches the two failure modes that a monthly capture
can't recover from after the fact:

  1. Truncation. The server caps a response at 500 listings regardless of the
     'search.max' field claiming 800. A section that comes back at/near 500 was
     probably cut off and has to be re-drawn smaller.
  2. Filter drift. The property-type checkboxes silently change between draws.
     A dropped type is invisible in the coverage number -- the response is
     "complete" for whatever filter was actually active -- so it has to be
     checked against the expected set explicitly.

Convention (CLAUDE.md section 4): Apartment + Townhouse + Fourplex, Condo Unit
and Triplex excluded (Triplex dropped 2026-08-11, not really multifamily).

Accepts two file shapes:
  - A single raw map.json payload (one draw/pan capture, the original workflow).
  - A combined file from tools/rf_console_capture.js: {"captures": [payload, ...]}
    -- everything the console helper captured across a whole panning session in
    one file. Each sub-capture is QC'd individually, same as separate files.

Usage:
    python src/check_capture.py data/raw/2026-08-11/*.json
    # or
    from check_capture import check_file, check_section_set
"""

import json
import sys
from collections import Counter
from pathlib import Path

# Documented per-response server cap. 'search.max' claims 800; it lies.
HARD_CAP = 500
# Draw smaller than this and a re-draw is never needed; between here and the cap
# is the ambiguous zone where 500-vs-500 can't be distinguished from truncation.
SAFE_TOTAL = 450

EXPECTED_TYPES = {"Apartment", "Townhouse", "Fourplex"}
BANNED_TYPES = {"Condo Unit"}

CITY_NAMES = {
    "2": "Edmonton", "43": "St. Albert", "33": "Sherwood Park",
    "34": "Spruce Grove", "39": "Leduc", "31": "Fort Saskatchewan",
    "36": "Beaumont",
}


def _analyze_payload(data, label):
    """QC one raw map.json payload dict. Prints a readable report, returns findings."""
    listings = data.get("listings") or []
    n = len(listings)
    ids = [x.get("id") for x in listings]
    n_unique = len(set(ids))

    total = data.get("total")
    total2 = data.get("total2")
    # total2 tracks the actual listings array when the two diverge (confirmed
    # 2026-08-10); total appears to count a wider area than the drawn/visible shape.
    effective_total = min([t for t in (total, total2) if t is not None], default=None)

    search = data.get("search") or {}
    active_types = set(search.get("type") or [])

    problems, notes = [], []

    # ---- truncation
    if n >= HARD_CAP:
        problems.append(
            f"AT CAP: {n} listings returned (cap {HARD_CAP}). This capture is "
            f"almost certainly truncated -- narrow the area/filter and split in two."
        )
    elif effective_total and effective_total > SAFE_TOTAL:
        problems.append(
            f"NEAR CAP: effective total {effective_total} is within {HARD_CAP - SAFE_TOTAL} "
            f"of the cap. Narrow it for headroom."
        )

    if effective_total is not None and n < effective_total:
        problems.append(
            f"SHORT: got {n} listings but the view reports {effective_total}. "
            f"{effective_total - n} missing."
        )

    if n_unique != n:
        notes.append(f"{n - n_unique} duplicate listing ids within this capture (deduped on ingest)")

    # ---- filter drift
    missing = EXPECTED_TYPES - active_types
    extra = active_types - EXPECTED_TYPES
    if missing:
        problems.append(
            f"FILTER GAP: {', '.join(sorted(missing))} not in the active filter. "
            f"Those property types are invisible for this whole capture -- redo it."
        )
    if extra & BANNED_TYPES:
        problems.append(
            f"FILTER GAP: {', '.join(sorted(extra & BANNED_TYPES))} included against "
            f"convention (condo units are not market rent) -- redo or filter on ingest."
        )
    elif extra:
        notes.append(f"unexpected type(s) in filter: {', '.join(sorted(extra))}")

    # ---- composition
    by_city = Counter(CITY_NAMES.get(str(x.get("city_id")), f"city_id={x.get('city_id')}")
                      for x in listings)
    by_type = Counter(x.get("type") for x in listings)

    lat = [float(x["latitude"]) for x in listings if x.get("latitude")]
    lon = [float(x["longitude"]) for x in listings if x.get("longitude")]
    bbox = (min(lat), min(lon), max(lat), max(lon)) if lat else None

    # ---- report
    print(f"\n=== {label} ===")
    print(f"listings: {n} ({n_unique} unique) | cap {HARD_CAP} | "
          f"headroom {HARD_CAP - n}")
    print(f"total: {total} | total2: {total2} | effective: {effective_total}")
    print(f"filter: {', '.join(sorted(active_types)) or '(none reported)'}")
    print(f"types seen: {dict(by_type)}")
    print(f"cities: {dict(by_city)}")
    if bbox:
        print(f"listing bbox: lat {bbox[0]:.4f}..{bbox[2]:.4f}  lon {bbox[1]:.4f}..{bbox[3]:.4f}")
    if search.get("area"):
        print(f"drawn/visible area: {search['area']}")

    for p in problems:
        print(f"  [PROBLEM] {p}")
    for nt in notes:
        print(f"  [note] {nt}")
    if not problems:
        print("  OK -- under cap, filter matches convention.")

    return {
        "file": label, "n": n, "n_unique": n_unique, "ids": set(ids),
        "total": total, "total2": total2, "effective_total": effective_total,
        "active_types": active_types, "by_city": by_city, "by_type": by_type,
        "bbox": bbox, "problems": problems, "notes": notes,
    }


def check_file(path):
    """QC one file on disk. Returns a list of per-capture findings dicts --
    length 1 for a normal single-payload file, or one entry per sub-capture
    for a combined {"captures": [...]} file from rf_console_capture.js."""
    path = Path(path)
    raw = json.loads(path.read_text())

    if isinstance(raw, dict) and isinstance(raw.get("captures"), list):
        results = [
            _analyze_payload(payload, f"{path.name}#{i}")
            for i, payload in enumerate(raw["captures"], 1)
        ]
        all_ids, overlap = set(), 0
        for r in results:
            overlap += len(all_ids & r["ids"])
            all_ids |= r["ids"]
        print(f"\n--- {path.name}: {len(results)} captures in this file, "
              f"{len(all_ids)} unique listings, {overlap} overlap between them ---")
        return results

    return [_analyze_payload(raw, path.name)]


def check_section_set(paths):
    """QC a whole snapshot's worth of files and report cross-file overlap.
    Each file may itself contain multiple captures (see check_file); all of
    them are flattened into one pool for the overlap/coverage summary."""
    results = [r for p in paths for r in check_file(p)]
    if len(results) < 2:
        return results

    all_ids, overlap = set(), 0
    for r in results:
        overlap += len(all_ids & r["ids"])
        all_ids |= r["ids"]

    total_rows = sum(r["n"] for r in results)
    print(f"\n=== full set ({len(results)} captures across {len(paths)} file(s)) ===")
    print(f"rows: {total_rows} | unique listings: {len(all_ids)} | "
          f"cross-capture overlap: {overlap}")
    cities = Counter()
    for r in results:
        cities.update(r["by_city"])
    print(f"cities: {dict(cities)}")
    flagged = [r["file"] for r in results if r["problems"]]
    if flagged:
        print(f"captures needing attention: {', '.join(flagged)}")
    else:
        print("all captures clean")
    return results


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    check_section_set(args)
