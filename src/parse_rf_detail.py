"""Parse RentFaster listing-detail captures into two tidy CSVs.

    python3 src/parse_rf_detail.py data/raw/detail/rf_detail_*.json \
        --outdir data/rf_data

Input is whatever `tools/rf_detail_capture.user.js` downloads. Several files
can be passed at once; captures are deduped on listing id, keeping the most
recent by capture timestamp.

Output
------
`rf_detail_listings.csv`  one row per listing, including the FULL description
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
import csv
import json
import pathlib
import re

LISTING_FIELDS = [
    "listing_id", "capture_date", "name", "slogan", "manager", "manager_phone",
    "manager_url", "street_address", "locality", "postal_code", "latitude",
    "longitude", "price_range", "phone", "pets_allowed", "smoking_allowed",
    "n_suite_types", "n_amenities", "amenities",
    "incentive_detected", "incentive_kinds", "incentive_snippet",
    "description", "image", "url", "canonical_url",
]
SUITE_FIELDS = [
    "listing_id", "capture_date", "name", "street_address", "postal_code",
    "latitude", "longitude", "manager", "suite_index", "beds", "baths_full",
    "baths_partial", "rent", "sqft", "rent_per_sqft", "availability",
    "utilities_included", "suite_label", "suite_description",
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
    "dollars off": r"\$\s?\d[\d,]*\s*(off|discount|credit|rebate|cash back)"
                   r"|\b(save|discount of)\b\s*\$\s?\d",
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

        listings.append({
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
            "description": entity.get("description"),
            "image": entity.get("image"), "url": capture.get("url"),
            "canonical_url": entity.get("url"),
        })

        for i, place in enumerate(places, start=1):
            props = _properties(place)
            rent = _number((place.get("potentialAction") or {}).get("price"))
            sqft = _number((place.get("floorSize") or {}).get("value")) or _number(props.get("Square Feet"))
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
                "availability": props.get("Availability Date"),
                "utilities_included": props.get("Utilities Included"),
                "suite_label": place.get("name"),
                "suite_description": place.get("description"),
                "incentive_detected": "Y" if kinds else "N",
                "incentive_kinds": "|".join(kinds),
                "url": capture.get("url"),
            })
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
    print(f"  incentive in text  {with_incentive}/{len(listings)}")
    if suites:
        print(f"  square feet        {with_sqft}/{len(suites)} ({100*with_sqft/len(suites):.0f}%)")
        print(f"  utilities included {with_utils}/{len(suites)} ({100*with_utils/len(suites):.0f}%)")
    print(f"Wrote {outdir}/rf_detail_listings.csv and rf_detail_suites.csv")


if __name__ == "__main__":
    main()
