"""
Rentfaster map.json Ingest — Step 1 of the Rentfaster/Inventory integration pipeline
=====================================================================================
Designed for Google Colab or local Python. No scraping: you paste/save map.json
payloads captured manually from the browser console (Network tab -> map.json ->
Response -> save to file). Multiple payload files per snapshot are supported and
encouraged (grab enough zoomed-in quadrants/drawn areas to beat the real ~500
listing cap per response -- the payload's own 'search.max' field claims 800,
but two independent captures confirmed the server actually truncates at 500
regardless). Coverage QC is area-based: a draw is complete when it returned
exactly total2 listings (under the 500 cap); any oversized/truncated draw must
have its area covered by complete draws or the run warns. Also accepts the
{"captures": [...]} export format. Warns on property-type filter drift.

Outputs (parquet, with CSV mirrors for Excel):
  listings_master.parquet   one row per listing_id (current state + first/last seen)
  snapshots.parquet         append-only: one row per listing per snapshot date
  ingest_log.csv            coverage/QC per run
  city_totals.csv           metro-wide listing counts per city per snapshot,
                             from the payload's top-level 'cities' field --
                             independent of this run's quadrant capture coverage

Each row carries baths_lo/baths_hi and promotion fields (has_promo, n_promotions,
promotions_raw, promo_rent_special, promo_other, promo_move_in_gift) in addition to
beds/price. promo_changes() flags listings whose incentive status flips (added/
dropped) between snapshots -- landlords often pull a promo right before changing
rent, so this is tracked as its own leading signal, separate from rent_changes().

user_id is the poster's account ID; user_listings_in_snapshot is the count of
listings that same user_id has live in the current snapshot -- a cheap portfolio-
size signal (a handful of accounts typically account for a large share of
listings -- property management companies, not individual landlords).

rent_confidence has four tiers (refined 2026-08-04, driven by beds_lo==beds_hi,
not raw units count): direct > certain > inferred > blended. See normalize_listing()
for the full rule and its documented caveat.

Usage in Colab:
  from rentfaster_ingest import ingest_snapshot
  ingest_snapshot(["map_nw.json", "map_ne.json", "map_sw.json"],
                  snapshot_date="2026-07-30", data_dir="/content/drive/MyDrive/rf_data")
"""

import json
import re
import os
from datetime import date
from pathlib import Path

import pandas as pd

try:  # parquet if available (Colab has pyarrow); CSV fallback otherwise
    import pyarrow  # noqa: F401
    _PQ = True
except ImportError:
    _PQ = False

def _save(df, path: "Path"):
    if _PQ:
        df.to_parquet(path, index=False)
    else:
        df.to_csv(path.with_suffix(".csv"), index=False)

def _load(path: "Path"):
    if _PQ and path.exists():
        return pd.read_parquet(path)
    csv = path.with_suffix(".csv")
    return pd.read_csv(csv, dtype={"snapshot_date": str}) if csv.exists() else None

# ---------------------------------------------------------------- helpers

BED_MAP = {"studio": 0, "bachelor": 0}

def parse_beds(val):
    """'studio' -> (0, False); '1' -> (1, False); '2+den' -> (2, True); None -> (None, False)"""
    if val is None or str(val).strip() == "":
        return None, False
    s = str(val).strip().lower()
    has_den = "+den" in s
    s = s.replace("+den", "").strip()
    if s in BED_MAP:
        return BED_MAP[s], has_den
    try:
        return int(float(s)), has_den
    except ValueError:
        return None, has_den

def parse_price(val):
    if val is None:
        return None
    try:
        p = float(str(val).replace(",", "").replace("$", ""))
    except ValueError:
        return None
    return p if p > 100 else None  # guards junk like price "1"

def parse_baths(val):
    """'1' -> 1.0; '2.5' -> 2.5; None/'' -> None. Half-baths are common, unlike beds."""
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None

# Known promotion codes observed in payloads as of 2026-07-30 (documented in
# CLAUDE.md). Any code not in this map still gets counted via has_promo /
# n_promotions / promotions_raw, it just won't get its own boolean column.
KNOWN_PROMO_CODES = {
    "rent_special": "promo_rent_special",
    "other_promotion": "promo_other",
    "move_in_gift": "promo_move_in_gift",
}

def address_from_slug(link):
    """'/properties/8217-130-ave-edmonton-358143' -> '8217 130 ave' (city+id stripped).
    Returns None for generic slugs like 'rentals-edmonton-635135'."""
    if not link:
        return None
    slug = link.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)               # strip trailing listing id
    slug = re.sub(r"-(edmonton|st-albert|sherwood-park|spruce-grove|leduc|"
                  r"fort-saskatchewan|beaumont)$", "", slug)
    if slug in ("rentals", ""):
        return None
    return slug.replace("-", " ").upper()

def normalize_listing(raw):
    beds_lo, den_lo = parse_beds(raw.get("beds"))
    beds_hi, den_hi = parse_beds(raw.get("beds2"))
    price_lo = parse_price(raw.get("price"))
    price_hi = parse_price(raw.get("price2"))
    baths_lo = parse_baths(raw.get("baths"))
    baths_hi = parse_baths(raw.get("baths2"))
    n_types = raw.get("units")

    # promotions and active_and_upcoming_promotions have been identical in
    # every payload seen so far (documented 2026-07-30); use whichever key
    # is present rather than `or`-chaining, so a real empty list on one
    # field isn't mistaken for "missing" and overridden by the other.
    if "promotions" in raw:
        promo_list = raw.get("promotions") or []
    else:
        promo_list = raw.get("active_and_upcoming_promotions") or []
    promo_flags = {col: (code in promo_list) for code, col in KNOWN_PROMO_CODES.items()}

    beds_hi_resolved = beds_hi if beds_hi is not None else beds_lo

    # Suite-type rent inference (refined 2026-08-04, supersedes the
    # units-only rule from 2026-07-30): confidence is driven by whether
    # beds_lo == beds_hi, not raw units count. units>=3 with a single bed
    # count (e.g. three differently-sized 2BR floorplans) is NOT ambiguous
    # -- the price range genuinely belongs to that one bed count. Blending
    # is only correct when the bed count itself spans a real range.
    #   units==1                              -> direct   (single suite type, no ambiguity at all)
    #   beds_lo == beds_hi (any units)        -> certain  (one bed count, price range = that bed count's range)
    #   beds_lo != beds_hi and units<=2       -> inferred (clean two-type split: price->beds, price2->beds2)
    #   beds_lo != beds_hi and units>=3        -> blended  (can't resolve which price maps to which bed count)
    #
    # Caveat (found 2026-08-04 checking a real payload): beds_hi is None in
    # some units>=2 listings that DO have a price2 -- i.e. a real price
    # spread with no reported second bed count. Sampled cases were all
    # consistent with "same bed count, different floorplan sizes" (supports
    # treating it as certain, not blended), but map.json alone can't fully
    # rule out a genuinely differing bed count that the site just didn't
    # report -- unconfirmed until cross-checked against detail-page JSON.
    if n_types == 1:
        conf = "direct"
    elif beds_lo is not None and beds_lo == beds_hi_resolved:
        conf = "certain"
    elif n_types == 2 and beds_hi is not None:
        conf = "inferred"
    else:
        conf = "blended"

    return {
        "listing_id": raw.get("id"),
        "user_id": raw.get("userId"),
        "city": raw.get("city"),
        "city_id": pd.to_numeric(raw.get("city_id"), errors="coerce"),
        "community": raw.get("community") or None,
        "latitude": raw.get("latitude"),
        "longitude": raw.get("longitude"),
        "type": raw.get("type"),
        "address_intro": (raw.get("intro") or "").strip() or None,
        "address_slug": address_from_slug(raw.get("link")),
        "link": raw.get("link"),
        "title": (raw.get("title") or "").strip()[:200],
        "listed_date": raw.get("date"),          # posted/renewed date, NOT DOM origin
        "availability": raw.get("availability"),
        "avail_date": raw.get("a"),
        "n_suite_types": n_types,
        "beds_lo": beds_lo, "beds_lo_den": den_lo,
        "beds_hi": beds_hi if beds_hi is not None else beds_lo,
        "beds_hi_den": den_hi,
        "price_lo": price_lo,
        "price_hi": price_hi if price_hi is not None else price_lo,
        "rent_confidence": conf,
        "baths_lo": baths_lo,
        "baths_hi": baths_hi if baths_hi is not None else baths_lo,
        "has_promo": bool(promo_list),
        "n_promotions": len(promo_list),
        "promotions_raw": ",".join(promo_list),
        **promo_flags,
        "verified": raw.get("personaVerified"),
    }

# ---------------------------------------------------------------- core

def load_payloads(path):
    """Return a list of map.json payload dicts from one file.

    Accepts a raw single map.json response, a bare list of listings, or a
    capture-tool export of the form {"captures": [payload, payload, ...]}.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("captures"), list):
        return data["captures"]
    if isinstance(data, list):
        return [{"listings": data}]
    return [data]

def load_payload(path):
    """Back-compat: merge every payload in a file into (listings, total, cities)."""
    ps = load_payloads(path)
    listings = [x for p in ps for x in (p.get("listings") or [])]
    totals = [p.get("total") for p in ps if p.get("total")]
    cities = next((p["cities"] for p in ps if p.get("cities")), [])
    return listings, (max(totals) if totals else None), cities

# Real per-response cap (confirmed 2026-08-10). A draw at/over this is truncated.
RF_CAP = 500
ALLOWED_TYPES = {"Apartment", "Townhouse"}  # Triplex/Fourplex dropped 2026-11-04

def _bbox(payload):
    """search.area is 'lat_hi,lon_hi,lat_lo,lon_lo' -> (lat_lo, lat_hi, lon_lo, lon_hi)."""
    try:
        n, e, s_, w = map(float, payload["search"]["area"].split(","))
        return (min(s_, n), max(s_, n), min(w, e), max(w, e))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

def _draw_complete(payload):
    """A draw is complete when it returned exactly what the site says it holds
    and stayed under the cap. total2 is the authoritative count (matches the
    listings array when the draw is small enough); when a draw is too big,
    total2 falls back to the inflated 'total', so n != total2 means 'oversized
    draw', not necessarily 'rows missing' -- coverage is judged by area below."""
    n = len(payload.get("listings") or [])
    t = payload.get("total2", payload.get("total"))
    if t is None:
        return n < RF_CAP
    return n < RF_CAP and n == t

def coverage_report(payloads, step=0.001):
    """Area-based coverage QC across all draws in a snapshot.

    Every draw with n != total2 (or n >= cap) is 'incomplete'. An incomplete
    draw is resolved if its whole bounding box lies inside the union of
    complete draws. Returns a dict; cells_uncovered_pct is the share of
    incomplete-draw area NOT covered by any complete draw (0 = fully covered).
    """
    import numpy as np
    complete = [b for p in payloads if _draw_complete(p) and (b := _bbox(p))]
    incomplete = [p for p in payloads if not _draw_complete(p)]
    inc_boxes = [_bbox(p) for p in incomplete]
    n_no_area = sum(b is None for b in inc_boxes)
    cells = unc = 0
    for b in filter(None, inc_boxes):
        la = np.arange(b[0], b[1], step); lo = np.arange(b[2], b[3], step)
        g_la, g_lo = np.meshgrid(la, lo, indexing="ij")
        covered = np.zeros(g_la.shape, dtype=bool)
        for c in complete:
            covered |= (g_la >= c[0]) & (g_la <= c[1]) & (g_lo >= c[2]) & (g_lo <= c[3])
        cells += covered.size; unc += int((~covered).sum())
    types_bad = sorted({
        str(sorted(p["search"]["type"])) for p in payloads
        if isinstance(p.get("search", {}).get("type"), list)
        and set(p["search"]["type"]) != ALLOWED_TYPES})
    return {
        "n_draws": len(payloads), "n_incomplete": len(incomplete),
        "n_incomplete_no_area": n_no_area,
        "uncovered_pct": round(unc / cells, 4) if cells else 0.0,
        "filter_drift": types_bad,
    }

def ingest_snapshot(payload_files, snapshot_date=None, data_dir="rf_data",
                    metro_city_ids=(2, 43, 33, 34, 39, 31, 36)):
    """Merge one or more map.json files into one snapshot, dedupe, append to store."""
    snapshot_date = snapshot_date or date.today().isoformat()
    data_dir = Path(data_dir); data_dir.mkdir(parents=True, exist_ok=True)

    rows, payloads, cities_seen = [], [], {}
    for p in payload_files:
        for pl in load_payloads(p):
            payloads.append(pl)
            rows.extend(normalize_listing(x) for x in (pl.get("listings") or []))
            for c in (pl.get("cities") or []):  # metro-wide; last one wins
                if c.get("city"):
                    cities_seen[c["city"]] = c.get("listings")

    df = pd.DataFrame(rows)
    n_raw = len(df)
    df = df.drop_duplicates(subset="listing_id", keep="first")
    df = df[df["listing_id"].notna()]
    df["snapshot_date"] = snapshot_date
    n_unique = len(df)

    # Portfolio footprint: how many units this poster has live in this
    # snapshot. Cheap, already-present field (user_id) made filterable/
    # sortable directly rather than requiring a pivot table in Excel.
    df["user_listings_in_snapshot"] = df.groupby("user_id")["listing_id"].transform("count")

    cov = coverage_report(payloads)
    print(f"[{snapshot_date}] files={len(payload_files)} draws={cov['n_draws']} "
          f"raw_rows={n_raw} unique={n_unique} incomplete_draws={cov['n_incomplete']} "
          f"uncovered_area={cov['uncovered_pct']:.1%}")
    if cov["uncovered_pct"] > 0 or cov["n_incomplete_no_area"]:
        print("  WARNING: some area is only covered by incomplete (oversized/truncated) "
              "draws. Redraw it in smaller boxes (aim for total2 <= ~450).")
    if cov["filter_drift"]:
        print(f"  WARNING: property-type filter drift in draws: {cov['filter_drift']} "
              f"(expected {sorted(ALLOWED_TYPES)})")
    odd = sorted(set(df["type"].dropna()) - ALLOWED_TYPES) if "type" in df else []
    if odd:
        print(f"  WARNING: unexpected listing types in data: {odd}")

    # ---- append to snapshots (idempotent per snapshot_date)
    snap_path = data_dir / "snapshots.parquet"
    existing = _load(snap_path)
    if existing is not None:
        existing = existing[existing["snapshot_date"] != snapshot_date]  # re-run safe
        snaps = pd.concat([existing, df], ignore_index=True)
    else:
        snaps = df
    _save(snaps, snap_path)

    # ---- rebuild master (first_seen / last_seen / active flag)
    grp = snaps.groupby("listing_id")["snapshot_date"]
    seen = grp.agg(first_seen="min", last_seen="max", n_snapshots="count")
    latest = (snaps.sort_values("snapshot_date")
                   .drop_duplicates("listing_id", keep="last")
                   .set_index("listing_id"))
    master = latest.join(seen)
    latest_snap = snaps["snapshot_date"].max()
    master["active"] = master["last_seen"] == latest_snap
    master = master.reset_index()

    _save(master, data_dir / "listings_master.parquet")
    master.to_csv(data_dir / "listings_master.csv", index=False)

    # ---- run log
    log_path = data_dir / "ingest_log.csv"
    log_row = pd.DataFrame([{
        "snapshot_date": snapshot_date, "files": len(payload_files),
        "draws": cov["n_draws"], "incomplete_draws": cov["n_incomplete"],
        "raw_rows": n_raw, "unique_listings": n_unique,
        "uncovered_area_pct": cov["uncovered_pct"],
        "filter_drift": ";".join(cov["filter_drift"]),
        "run_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    }])
    if log_path.exists():
        prev = pd.read_csv(log_path, dtype={"snapshot_date": str})
        prev = prev[prev["snapshot_date"] != snapshot_date]  # re-run safe
        log_row = pd.concat([prev, log_row], ignore_index=True)
    log_row.to_csv(log_path, index=False)

    # ---- city-wide totals (independent of this run's quadrant capture coverage)
    if cities_seen:
        city_rows = pd.DataFrame([
            {"snapshot_date": snapshot_date, "city": city, "site_reported_listings": n}
            for city, n in cities_seen.items()
        ])
        city_path = data_dir / "city_totals.csv"
        if city_path.exists():
            existing_city = pd.read_csv(city_path)
            existing_city = existing_city[existing_city["snapshot_date"] != snapshot_date]
            city_rows = pd.concat([existing_city, city_rows], ignore_index=True)
        city_rows.to_csv(city_path, index=False)

    return master

# ---------------------------------------------------------------- rent-change signal

def rent_changes(data_dir="rf_data"):
    """Same listing_id, price_lo change between consecutive snapshots.
    Cuts on a lingering listing = softness signal."""
    snaps = _load(Path(data_dir) / "snapshots.parquet")
    snaps = snaps.sort_values(["listing_id", "snapshot_date"])
    snaps["prev_price_lo"] = snaps.groupby("listing_id")["price_lo"].shift()
    snaps["prev_snapshot"] = snaps.groupby("listing_id")["snapshot_date"].shift()
    chg = snaps[snaps["prev_price_lo"].notna()
                & (snaps["price_lo"] != snaps["prev_price_lo"])].copy()
    chg["delta"] = chg["price_lo"] - chg["prev_price_lo"]
    cols = ["listing_id", "address_slug", "community", "type",
            "prev_snapshot", "snapshot_date", "prev_price_lo", "price_lo", "delta"]
    return chg[cols]

# ---------------------------------------------------------------- promotion-change signal

def promo_changes(data_dir="rf_data"):
    """Same listing_id, has_promo flips (added or dropped) between consecutive
    snapshots. Landlords commonly pull an incentive right before a rent change
    (up or down) — this is a leading indicator, not just a coincident one, so
    flag it separately from rent_changes rather than folding it in."""
    snaps = _load(Path(data_dir) / "snapshots.parquet")
    snaps = snaps.sort_values(["listing_id", "snapshot_date"])
    snaps["prev_has_promo"] = snaps.groupby("listing_id")["has_promo"].shift()
    snaps["prev_promotions_raw"] = snaps.groupby("listing_id")["promotions_raw"].shift()
    snaps["prev_snapshot"] = snaps.groupby("listing_id")["snapshot_date"].shift()
    chg = snaps[snaps["prev_has_promo"].notna()
                & (snaps["has_promo"] != snaps["prev_has_promo"])].copy()
    chg["change"] = chg["has_promo"].map({True: "added", False: "dropped"})
    cols = ["listing_id", "address_slug", "community", "type", "change",
            "prev_snapshot", "snapshot_date",
            "prev_promotions_raw", "promotions_raw", "price_lo"]
    return chg[cols]

if __name__ == "__main__":
    import sys
    files = sys.argv[1:] or ["map.json"]
    ingest_snapshot(files)
