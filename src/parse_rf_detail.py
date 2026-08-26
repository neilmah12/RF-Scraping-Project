"""Parse RentFaster listing-detail captures into two tidy CSVs.

    python3 src/parse_rf_detail.py data/raw/detail/rf_detail_*.json \
        --outdir data/rf_data

Input is whatever `tools/rf_detail_capture.user.js` downloads. Several files
can be passed at once; captures are deduped on listing id, keeping the most
recent by capture timestamp.

Output
------
`rf_detail_listings.csv`  one row per listing, the FULL description, and
                          incentive + parking fields derived from it
`rf_detail_suites.csv`    one row per advertised suite type  <- the useful one

Why the suite file matters (measured on the 2026-08-19 sample, 21 listings /
103 suites):

    bedrooms, bathrooms, rent   100%
    SQUARE FEET                  98%
    Utilities Included           75%

Square footage is the largest gap in the proforma inputs -- FileMaker suite
sizes run 8-19% populated and CoStar's `Average Unit SF` is 28% and dropped in
config.py. Here it is 98%, per suite type, tied to a bed count AND a rent, so
rent per square foot becomes computable for the first time.

It also collapses map.json's rent-confidence tiers. 12 of those 21 listings
were `blended` -- a price range with no way to know which price belongs to
which bed count. The detail states it outright, so every captured listing
becomes equivalent to `direct`.

Caveats carried into the output
-------------------------------
- `containsPlace` is what is CURRENTLY ADVERTISED, not the building's suite
  mix. One capture showed a single 1-bed while its own description mentioned
  bachelor, 1 and 2 bedroom units. Treat a row as a rent observation, not a
  rent roll line.
- `vacancy_signal` is directional, not a vacancy rate. `current` means at
  least one advertised suite type is available now and says NOTHING about how
  many units that is. `none_current` means every advertised suite type is
  future-dated, which is evidence of no vacancy today -- but only across the
  suite types the building chose to advertise. A building with no listing at
  all is absent from this file entirely and must never be read as full.
- `parentOrganization` is the property MANAGER, which is not always the owner.
  It corroborated Inventory's Owner Company on 11 of 15 comparable listings;
  the misses include a building Zen Residential manages for an individual.
- Rents move between captures, so `capture_date` is carried on every row and
  must not be folded into a map.json snapshot date.
- `incentive_detected` is a heuristic over the ad copy, not ground truth. The
  full `description` is written out verbatim so the rules can be improved and
  re-run later without recapturing anything.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import availability  # noqa: E402  -- see its header, duplicated across repos

LISTING_FIELDS = [
    "listing_id", "capture_date", "name", "slogan", "manager", "manager_phone",
    "manager_url", "street_address", "locality", "postal_code", "latitude",
    "longitude", "price_range", "phone", "pets_allowed", "smoking_allowed",
    "n_suite_types", "n_amenities", "amenities",
    "vacancy_signal", "suite_types_immediate", "earliest_available",
    "incentive_detected", "incentive_kinds", "incentive_snippet",
    "promo_count", "promo_types", "promo_headlines", "promo_body",
    "promo_discount", "promo_discount_amount", "promo_lease_length",
    "promo_valid", "promo_other_fields",
    "parking_types", "parking_rate_monthly", "parking_rate_text",
    "description", "promo_raw", "image", "url", "canonical_url",
]
SUITE_FIELDS = [
    "listing_id", "capture_date", "name", "street_address", "postal_code",
    "latitude", "longitude", "manager", "suite_index", "beds", "baths_full",
    "baths_partial", "rent", "sqft", "rent_per_sqft", "availability",
    "available_date", "available_immediate",
    "utilities_included", "unit_number", "suite_label", "suite_description",
    "incentive_detected", "incentive_kinds", "url",
]


# --- incentive extraction ---------------------------------------------------
# map.json's `promotions` is a tag list only -- it flags THAT an incentive
# exists, never its terms, and it misses incentives written into the ad copy
# rather than entered in the promotions box. Measured on the 2026-08-19 sample:
# 2 of 21 listings advertise an incentive in the text that the structured flag
# does not catch ("Get up to 2 months free", "1 MONTH FREE RENT PLUS FREE
# INTERNET & CABLE"). Seven carried the flag with no matching text.
#
# So the two signals are COMPLEMENTARY, not redundant -- use both, and treat
# neither as complete. Terms matter for a proforma: two months free is roughly
# a 16% effective discount, $500 off is nearer 3%.
#
# These patterns are a convenience layer. The full `description` is written out
# verbatim so any of this can be re-derived later with better rules, without
# recapturing anything.
INCENTIVE_PATTERNS = {
    "months free": r"\b(\d+|one|two|half|1/2)\s*(month|months|mo)\b[^.]{0,30}\bfree\b"
                   r"|\bfree\b[^.]{0,20}\b(month|months)\b",
    # Allows words between the amount and the keyword. The first version
    # required them adjacent and so missed "$750 MOVE-IN CREDIT" on G17
    # Apartments -- a real $750 incentive that never reached the workbook.
    "dollars off": r"\$\s?\d[\d,.]*[^.\n]{0,25}?\b(off|discount|credit|rebate|cash back)\b"
                   r"|\b(save|discount of)\b\s*\$\s?\d",
    "free rent period": r"\b(don'?t|do not)\s+pay\s+rent\b|\brent\s+free\b|\bfree\s+rent\b",
    "reduced deposit": r"\b(deposit)\b[^.]{0,40}\$\s?\d|\$\s?\d[\d,]*\s*(security\s*)?deposit"
                       r"|\breduced\b[^.]{0,15}\bdeposit\b",
    "gift card": r"\bgift\s*card\b|\bvisa\s*card\b",
    "free parking or utilities": r"\bfree\b[^.]{0,20}\b(parking|internet|wifi|cable|utilit)",
    "promo or special": r"\b(promo|promotion|special offer|limited time|move[- ]in bonus|incentive)\b",
}


def find_incentives(*texts) -> tuple[list[str], str]:
    """Which incentive patterns fire, plus a snippet of the first match."""
    blob = " ".join(str(t or "") for t in texts)
    lowered = blob.lower()
    kinds, snippet = [], ""
    for label, pattern in INCENTIVE_PATTERNS.items():
        match = re.search(pattern, lowered)
        if not match:
            continue
        kinds.append(label)
        if not snippet:
            start = max(0, match.start() - 50)
            snippet = " ".join(blob[start:match.end() + 60].split())
    return kinds, snippet


# --- promotions (from the rendered page, not the JSON-LD) -------------------
# The JSON-LD folds promo copy into `description` and drops every structured
# part. G17 Apartments advertises "Discount: $750.00 off / Lease length: 12
# months / Valid from: Mar 01, 2026" in a labelled box; none of those labels
# survive, only a run-on sentence. So `rf_detail_capture.user.js` v1.1 reads
# the box from the DOM and stores it under a `promotions` key.
#
# Captures made before v1.1 have no `promotions` key and fall back to the
# text heuristic, which is weaker -- it missed the G17 discount entirely.
PROMO_FIELDS = ["promo_count", "promo_types", "promo_headlines", "promo_body",
                "promo_discount", "promo_discount_amount", "promo_lease_length",
                "promo_valid", "promo_other_fields", "promo_raw"]


def flatten_promotions(promotions) -> dict:
    """Promo blocks -> flat columns. Empty dict shape when there are none."""
    blank = {f: "" for f in PROMO_FIELDS}
    blank["promo_count"] = 0
    if not promotions or not promotions.get("promos"):
        if promotions and promotions.get("raw"):
            blank["promo_raw"] = promotions["raw"]
        return blank

    promos = promotions["promos"]
    discounts, amounts, lease, valid, other = [], [], [], [], []
    for promo in promos:
        fields = promo.get("fields") or {}
        for key, value in fields.items():
            low = key.lower()
            if "discount" in low:
                discounts.append(value)
                found = re.search(r"\$\s?(\d[\d,.]*)", value)
                if found:
                    try:
                        amounts.append(float(found.group(1).replace(",", "").rstrip(".")))
                    except ValueError:
                        pass
            elif "lease" in low:
                lease.append(value)
            elif "valid" in low:
                valid.append(f"{key}: {value}")
            elif low not in ("https", "http"):
                other.append(f"{key}: {value}")

    return {
        "promo_count": len(promos),
        "promo_types": "|".join(p.get("type", "") for p in promos),
        "promo_headlines": " | ".join(p.get("headline", "") for p in promos if p.get("headline")),
        "promo_body": " | ".join(p.get("body", "") for p in promos if p.get("body")),
        "promo_discount": " | ".join(discounts),
        "promo_discount_amount": max(amounts) if amounts else "",
        "promo_lease_length": " | ".join(lease),
        "promo_valid": " | ".join(valid),
        "promo_other_fields": " | ".join(other),
        "promo_raw": promotions.get("raw", ""),
    }


# --- parking ----------------------------------------------------------------
# Parking is almost entirely absent from the structured payload: on the
# 2026-08-19 sample only 1 of 21 listings carried a parking amenity tag
# ("Guest Parking"), while 14 described parking in the ad copy. Rates appear
# only in the text -- 3 of 21 quoted one, ranging from $10/mth for an outdoor
# stall to $195/mth underground.
#
# That range is material. Inventory already holds Parking Type at 43% and
# stall counts at 49%, but carries NO rate field anywhere, so this is the only
# source for it. On a 100-stall building the difference between $10 and $195
# is roughly $220k of annual revenue.
#
# Type is extracted as a cross-check on Inventory's Parking Type rather than a
# replacement -- Inventory's is the more reliable of the two when present.
PARKING_WORD = r"(?:parking|parkade|stall|garage|carport)"
PARKING_TYPES = {
    "underground": r"\bunderground\b|\bparkade\b",
    "surface": r"\b(surface|outdoor|open)\b",
    "covered": r"\bcovered\b",
    "heated": r"\bheated\b",
    "energized": r"\b(energized|plug[- ]?in)\b",
    "garage": r"\bgarage\b",
    "tandem": r"\btandem\b",
    "assigned": r"\b(assigned|titled)\b",
}
_RATE = re.compile(
    r"[^.\n]{0,90}\$\s?\d[\d,.]*\s*(?:/|per\s*)?\s*(?:mth|month|mo\b)[^.\n]{0,45}",
    re.I,
)


def find_parking(description) -> tuple[str, str, str]:
    """(types, rate snippet, monthly rate) from the ad copy.

    Every candidate segment must contain an actual parking word, so "nearby
    parks and trails for outdoor activities" is not read as surface parking --
    a real false positive in the first pass over this sample.
    """
    text = str(description or "")
    if not text:
        return "", "", ""

    types = []
    for label, pattern in PARKING_TYPES.items():
        for segment in re.finditer(
            r"[^.\n]{0,70}" + PARKING_WORD + r"[^.\n]{0,70}", text, re.I
        ):
            if re.search(pattern, segment.group(0), re.I):
                types.append(label)
                break

    snippet, monthly = "", ""
    for match in _RATE.finditer(text):
        segment = " ".join(match.group(0).split())
        if not re.search(PARKING_WORD, segment, re.I):
            continue
        snippet = segment[:200]
        # Pick the amount NEAREST a parking word, not the smallest. A segment
        # like "Parking, Pets & Storage: Parking is $195 per month" also holds
        # a $35 pet fee, and taking min() reported the pet fee as the parking
        # rate on the first pass over this sample.
        anchors = [m.start() for m in re.finditer(PARKING_WORD, segment, re.I)]
        best, best_distance = None, None
        for money in re.finditer(r"\$\s?(\d[\d,.]*)", segment):
            try:
                value = float(money.group(1).replace(",", "").rstrip("."))
            except ValueError:
                continue
            distance = min((abs(money.start() - a) for a in anchors), default=10**6)
            if best_distance is None or distance < best_distance:
                best, best_distance = value, distance
        monthly = str(best) if best is not None else ""
        break
    return "|".join(types), snippet, monthly


def load(paths: list[str]) -> dict[str, dict]:
    """Newest capture per listing id, across any number of download files."""
    newest: dict[str, dict] = {}
    for path in paths:
        payload = json.load(open(path))
        for capture in payload.get("captures", []):
            listing_id = capture.get("listing_id")
            if not listing_id:
                continue
            previous = newest.get(listing_id)
            if previous is None or capture.get("ts", "") > previous.get("ts", ""):
                newest[listing_id] = capture
    return newest


def _number(value):
    if value is None:
        return None
    text = re.sub(r"[^0-9.]", "", str(value))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _properties(node) -> dict:
    return {p.get("name"): p.get("value") for p in (node.get("additionalProperty") or [])}


def rows(captures: dict[str, dict]):
    listings, suites = [], []
    for listing_id, capture in sorted(captures.items()):
        entity = (capture.get("data") or {}).get("mainEntity") or {}
        address = entity.get("address") or {}
        geo = entity.get("geo") or {}
        manager = entity.get("parentOrganization") or {}
        amenities = [a.get("name") for a in (entity.get("amenityFeature") or []) if a.get("name")]
        places = entity.get("containsPlace") or []
        date = (capture.get("ts") or "")[:10]
        kinds, snippet = find_incentives(entity.get("slogan"), entity.get("description"))
        park_types, park_text, park_rate = find_parking(entity.get("description"))
        promo = flatten_promotions(capture.get("promotions"))

        row = {
            "listing_id": listing_id, "capture_date": date,
            "name": entity.get("name"), "slogan": entity.get("slogan"),
            "manager": manager.get("name"), "manager_phone": manager.get("telephone"),
            "manager_url": manager.get("url"),
            "street_address": address.get("streetAddress"),
            "locality": address.get("addressLocality"),
            "postal_code": address.get("postalCode"),
            "latitude": geo.get("latitude"), "longitude": geo.get("longitude"),
            "price_range": entity.get("priceRange"), "phone": entity.get("telephone"),
            "pets_allowed": entity.get("petsAllowed"),
            "smoking_allowed": entity.get("smokingAllowed"),
            "n_suite_types": len(places), "n_amenities": len(amenities),
            "amenities": "|".join(amenities),
            "incentive_detected": "Y" if kinds else "N",
            "incentive_kinds": "|".join(kinds), "incentive_snippet": snippet,
            "parking_types": park_types, "parking_rate_monthly": park_rate,
            "parking_rate_text": park_text,
            "description": entity.get("description"),
            "image": entity.get("image"), "url": capture.get("url"),
            "canonical_url": entity.get("url"),
        }
        row.update(promo)
        listings.append(row)

        parsed_availability = []
        for i, place in enumerate(places, start=1):
            props = _properties(place)
            rent = _number((place.get("potentialAction") or {}).get("price"))
            sqft = _number((place.get("floorSize") or {}).get("value")) or _number(props.get("Square Feet"))
            raw_availability = props.get("Availability Date")
            available_date, available_flag = availability.parse(raw_availability, date)
            suites.append({
                "listing_id": listing_id, "capture_date": date,
                "name": entity.get("name"),
                "street_address": address.get("streetAddress"),
                "postal_code": address.get("postalCode"),
                "latitude": geo.get("latitude"), "longitude": geo.get("longitude"),
                "manager": manager.get("name"), "suite_index": i,
                "beds": place.get("numberOfBedrooms"),
                "baths_full": place.get("numberOfFullBathrooms"),
                "baths_partial": place.get("numberOfPartialBathrooms"),
                "rent": rent, "sqft": sqft,
                "rent_per_sqft": round(rent / sqft, 3) if rent and sqft else None,
                "availability": raw_availability,
                "available_date": available_date,
                "available_immediate": (
                    "Y" if availability.is_available_now(available_date, available_flag, date)
                    else {"dated": "N", "none": "none"}.get(available_flag, "")),
                "utilities_included": props.get("Utilities Included"),
                # containsPlace `name` is sometimes "Unit 785-205" and
                # sometimes "1 bed, 1 bath, $999" -- the unit number is only
                # present on listings that advertise individual units.
                "unit_number": (re.match(r"\s*Unit\s+(\S+)", str(place.get("name") or ""))
                                or [None, ""])[1] if re.match(r"\s*Unit\s+", str(place.get("name") or "")) else "",
                "suite_label": place.get("name"),
                "suite_description": place.get("description"),
                "incentive_detected": "Y" if kinds else "N",
                "incentive_kinds": "|".join(kinds),
                "url": capture.get("url"),
            })
            parsed_availability.append((available_date, available_flag))

        signal, now, earliest = availability.signal(parsed_availability, date)
        row["vacancy_signal"] = signal
        row["suite_types_immediate"] = now
        row["earliest_available"] = earliest
    return listings, suites


def write(path: pathlib.Path, fields: list[str], data: list[dict]) -> None:
    # CRLF to match the project's other Excel-consumable outputs.
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--outdir", default="data/rf_data")
    args = parser.parse_args()

    captures = load(args.files)
    listings, suites = rows(captures)

    outdir = pathlib.Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    write(outdir / "rf_detail_listings.csv", LISTING_FIELDS, listings)
    write(outdir / "rf_detail_suites.csv", SUITE_FIELDS, suites)

    with_sqft = sum(1 for s in suites if s["sqft"])
    with_utils = sum(1 for s in suites if s["utilities_included"])
    with_incentive = sum(1 for l in listings if l["incentive_detected"] == "Y")
    print(f"{len(listings)} listings, {len(suites)} suite types")
    with_parking = sum(1 for l in listings if l["parking_types"])
    with_rate = sum(1 for l in listings if l["parking_rate_monthly"])
    with_promo_box = sum(1 for l in listings if l["promo_count"])
    print(f"  promotions box     {with_promo_box}/{len(listings)}  (structured, from the page)")
    print(f"  incentive in text  {with_incentive}/{len(listings)}  (heuristic fallback)")
    print(f"  parking type       {with_parking}/{len(listings)}")
    print(f"  parking RATE       {with_rate}/{len(listings)}  (no other source has this)")
    if suites:
        print(f"  square feet        {with_sqft}/{len(suites)} ({100*with_sqft/len(suites):.0f}%)")
        print(f"  utilities included {with_utils}/{len(suites)} ({100*with_utils/len(suites):.0f}%)")
        resolved = sum(1 for s in suites if s["available_immediate"])
        print(f"  availability       {resolved}/{len(suites)} ({100*resolved/len(suites):.0f}%) resolved to a date or a flag")
        signals = collections.Counter(l["vacancy_signal"] or "unknown" for l in listings)
        print(f"  vacancy signal     " + ", ".join(f"{k} {v}" for k, v in signals.most_common()))

    # Any availability text the parser did not recognise. Surfaced rather than
    # left blank: a new phrasing ("Available now") would otherwise read as
    # missing data, and the fix is one entry in NOT_A_DATE.
    unparsed = collections.Counter(
        s["availability"] for s in suites
        if s["availability"] and not s["available_date"] and not s["available_immediate"]
    )
    if unparsed:
        print("  UNPARSED availability text -- add to NOT_A_DATE or the date pattern:")
        for text, count in unparsed.most_common():
            print(f"    {count:3d}  {text!r}")
    print(f"Wrote {outdir}/rf_detail_listings.csv and rf_detail_suites.csv")


if __name__ == "__main__":
    main()
