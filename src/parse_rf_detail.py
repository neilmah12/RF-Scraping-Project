"""Parse RentFaster listing-detail captures into two tidy CSVs.

    python3 src/parse_rf_detail.py data/raw/detail/rf_detail_*.json \
        --outdir data/rf_data

Input is whatever `tools/rf_detail_capture.user.js` downloads. Several files
can be passed at once; captures are deduped on listing id, keeping the most
recent by capture timestamp.

Output
------
`rf_detail_listings.csv`  one row per listing
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
    "longitude", "price_range", "phone", "pets_allowed", "n_suite_types",
    "n_amenities", "amenities", "image", "url",
]
SUITE_FIELDS = [
    "listing_id", "capture_date", "name", "street_address", "postal_code",
    "latitude", "longitude", "manager", "suite_index", "beds", "baths_full",
    "baths_partial", "rent", "sqft", "rent_per_sqft", "availability",
    "utilities_included", "suite_label", "url",
]


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
            "n_suite_types": len(places), "n_amenities": len(amenities),
            "amenities": "|".join(amenities), "image": entity.get("image"),
            "url": capture.get("url"),
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
                "suite_label": place.get("name"), "url": capture.get("url"),
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
    print(f"{len(listings)} listings, {len(suites)} suite types")
    if suites:
        print(f"  square feet        {with_sqft}/{len(suites)} ({100*with_sqft/len(suites):.0f}%)")
        print(f"  utilities included {with_utils}/{len(suites)} ({100*with_utils/len(suites):.0f}%)")
    print(f"Wrote {outdir}/rf_detail_listings.csv and rf_detail_suites.csv")


if __name__ == "__main__":
    main()
