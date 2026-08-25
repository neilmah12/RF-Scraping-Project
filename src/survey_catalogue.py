"""Catalogue rental market surveys: the units observed, and the comp graph.

    python3 src/survey_catalogue.py --survey data/surveys/2026-08-25_castle_harbour.csv \
        --detail data/raw/detail/rf_detail_*.json

Two outputs, appended to rather than overwritten, because the value is
cumulative:

`survey_units.csv`   one row per (survey, building, unit type) -- what was
                     observed: SF, rent, $/SF, incentive, and whether the
                     RentFaster capture agrees.
`survey_comps.csv`   one row per (survey, subject, comp) -- THE COMP GRAPH.
                     Which buildings were judged comparable to which, by whom,
                     when, and why.

Why the graph is the point
--------------------------
Comparability is driven by suite condition and finish, and no dataset records
that. CoStar Star Rating does not: on 726 matched listings it separates rent
overall, but within the 1960-79 vintage the 2-star and 3-star medians are both
$1,149 -- it is mostly encoding age. `Year Renov` is 1% populated.

Marketing copy cannot substitute. "Fully renovated boutique suites" says
nothing about when or to what degree, and the worst stock still advertises
itself as highly amenitized. Only the checkable facts carry: flooring type,
in-suite versus shared laundry, air conditioning, square feet, rent.

What does encode condition is an experienced judgment that two buildings could
command similar rents. Every survey produces those judgments and normally
throws them away. Stored, they accumulate into a labelled comparability
network -- which is both directly usable ("what did I last compare this to")
and the training signal for learning which cheap features predict the call.

Nothing here analyses the graph. It exists to stop the data being lost.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import pathlib
import re

UNIT_FIELDS = [
    "survey_id", "survey_date", "surveyor", "purpose",
    "role", "building_name", "address", "rentfaster_id", "building_id",
    "unit_type", "beds", "sqft", "rent", "rent_psf",
    "incentive_noted", "incentive_source",
    "capture_sqft", "capture_rent", "agrees_with_capture", "notes",
]
COMP_FIELDS = [
    "survey_id", "survey_date", "surveyor", "purpose",
    "subject_address", "subject_building_id",
    "comp_address", "comp_building_id", "comp_rentfaster_id",
    "included", "comp_basis", "condition_call", "notes",
]


def load_captures(patterns: list[str]) -> dict:
    """Newest detail capture per listing id."""
    newest: dict[str, dict] = {}
    for pattern in patterns:
        for path in glob.glob(pattern):
            for capture in json.load(open(path)).get("captures", []):
                listing = capture.get("listing_id")
                if not listing:
                    continue
                if listing not in newest or capture.get("ts", "") > newest[listing].get("ts", ""):
                    newest[listing] = capture
    return newest


def capture_units(capture) -> list[dict]:
    entity = (capture.get("data") or {}).get("mainEntity") or {}
    out = []
    for place in (entity.get("containsPlace") or []):
        price = (place.get("potentialAction") or {}).get("price")
        out.append({
            "beds": place.get("numberOfBedrooms"),
            "sqft": (place.get("floorSize") or {}).get("value"),
            "rent": float(price) if price else None,
        })
    return out


def listing_id_from(value) -> str:
    """Accept a bare id or a RentFaster URL."""
    text = str(value or "").strip()
    if text.isdigit():
        return text
    found = re.search(r"-(\d+)(?:\?|$)", text)
    return found.group(1) if found else ""


def beds_from(unit_type) -> str:
    text = str(unit_type or "").lower()
    if "bach" in text or "studio" in text:
        return "0"
    found = re.search(r"(\d+)\s*bed", text)
    return found.group(1) if found else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survey", required=True, help="survey CSV (see data/surveys/TEMPLATE.csv)")
    parser.add_argument("--detail", nargs="*", default=[], help="rf_detail_*.json capture files")
    parser.add_argument("--outdir", default="data/rf_data")
    args = parser.parse_args()

    rows = list(csv.DictReader(open(args.survey)))
    captures = load_captures(args.detail)

    survey_id = (rows[0].get("survey_id") or "").strip() if rows else ""
    units, comps, seen_comp = [], [], set()

    for row in rows:
        listing = listing_id_from(row.get("rentfaster_id") or row.get("link"))
        capture = captures.get(listing)
        sqft = row.get("sqft") or ""
        rent = row.get("rent") or ""

        cap_sqft = cap_rent = agrees = ""
        if capture:
            want = beds_from(row.get("unit_type"))
            for unit in capture_units(capture):
                if want and str(unit["beds"]) == want:
                    cap_sqft, cap_rent = unit["sqft"], unit["rent"]
                    try:
                        agrees = "Y" if (float(sqft) == float(cap_sqft or 0)
                                         and abs(float(rent) - float(cap_rent or 0)) < 0.5) else "N"
                    except ValueError:
                        agrees = ""
                    break

        psf = row.get("rent_psf") or ""
        if not psf and sqft and rent:
            try:
                psf = round(float(rent) / float(sqft), 2)
            except (ValueError, ZeroDivisionError):
                psf = ""

        units.append({
            "survey_id": survey_id, "survey_date": row.get("survey_date", ""),
            "surveyor": row.get("surveyor", ""), "purpose": row.get("purpose", ""),
            "role": row.get("role", ""), "building_name": row.get("building_name", ""),
            "address": row.get("address", ""), "rentfaster_id": listing,
            "building_id": row.get("building_id", ""),
            "unit_type": row.get("unit_type", ""), "beds": beds_from(row.get("unit_type")),
            "sqft": sqft, "rent": rent, "rent_psf": psf,
            "incentive_noted": row.get("incentive_noted", ""),
            "incentive_source": row.get("incentive_source", ""),
            "capture_sqft": cap_sqft, "capture_rent": cap_rent,
            "agrees_with_capture": agrees, "notes": row.get("notes", ""),
        })

    subject = next((r for r in rows if (r.get("role") or "").lower() == "subject"), None)
    for row in rows:
        if row is subject or (row.get("role") or "").lower() == "subject":
            continue
        key = (survey_id, row.get("address"))
        if key in seen_comp:
            continue
        seen_comp.add(key)
        comps.append({
            "survey_id": survey_id, "survey_date": row.get("survey_date", ""),
            "surveyor": row.get("surveyor", ""), "purpose": row.get("purpose", ""),
            "subject_address": (subject or {}).get("address", ""),
            "subject_building_id": (subject or {}).get("building_id", ""),
            "comp_address": row.get("address", ""),
            "comp_building_id": row.get("building_id", ""),
            "comp_rentfaster_id": listing_id_from(row.get("rentfaster_id") or row.get("link")),
            "included": row.get("included", "Y"),
            "comp_basis": row.get("comp_basis", ""),
            "condition_call": row.get("condition_call", ""),
            "notes": row.get("notes", ""),
        })

    outdir = pathlib.Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for name, fields, data in [("survey_units.csv", UNIT_FIELDS, units),
                               ("survey_comps.csv", COMP_FIELDS, comps)]:
        path = outdir / name
        exists = path.exists()
        # Append: the catalogue is cumulative and a re-run must not wipe history.
        with open(path, "a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\r\n")
            if not exists:
                writer.writeheader()
            writer.writerows(data)
        print(f"appended {len(data):3d} rows to {path}")

    checked = [u for u in units if u["agrees_with_capture"]]
    if checked:
        agree = sum(1 for u in checked if u["agrees_with_capture"] == "Y")
        print(f"cross-check vs RentFaster capture: {agree}/{len(checked)} agree on both SF and rent")


if __name__ == "__main__":
    main()
