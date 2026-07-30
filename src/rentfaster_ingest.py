"""
Rentfaster map.json Ingest — Step 1 of the Rentfaster/Inventory integration pipeline
=====================================================================================
Designed for Google Colab or local Python. No scraping: you paste/save map.json
payloads captured manually from the browser console (Network tab -> map.json ->
Response -> save to file). Multiple payload files per snapshot are supported and
encouraged (grab 2-4 zoomed-in quadrants to beat the ~800 listing cap per response;
the script reports coverage vs the 'total' field).

Outputs (parquet, with CSV mirrors for Excel):
  listings_master.parquet   one row per listing_id (current state + first/last seen)
  snapshots.parquet         append-only: one row per listing per snapshot date
  ingest_log.csv            coverage/QC per run

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
    n_types = raw.get("units")

    # Suite-type rent inference (documented decision 2026-07-30):
    #   units==1 -> price maps to beds directly (confidence: direct)
    #   units==2 -> price->beds, price2->beds2 (confidence: inferred)
    #   units>=3 -> range only, no per-type mapping (confidence: range_only)
    if n_types == 1:
        conf = "direct"
    elif n_types == 2 and beds_hi is not None:
        conf = "inferred"
    else:
        conf = "range_only"

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
        "has_promo": bool(raw.get("active_and_upcoming_promotions")),
        "promos": ",".join(raw.get("active_and_upcoming_promotions") or []),
        "verified": raw.get("personaVerified"),
    }

# ---------------------------------------------------------------- core

def load_payload(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    listings = data.get("listings", data if isinstance(data, list) else [])
    total = data.get("total")
    return listings, total

def ingest_snapshot(payload_files, snapshot_date=None, data_dir="rf_data",
                    metro_city_ids=(2, 43, 33, 34, 39, 31, 36)):
    """Merge one or more map.json files into one snapshot, dedupe, append to store."""
    snapshot_date = snapshot_date or date.today().isoformat()
    data_dir = Path(data_dir); data_dir.mkdir(parents=True, exist_ok=True)

    rows, totals = [], []
    for p in payload_files:
        listings, total = load_payload(p)
        totals.append(total)
        rows.extend(normalize_listing(x) for x in listings)

    df = pd.DataFrame(rows)
    n_raw = len(df)
    df = df.drop_duplicates(subset="listing_id", keep="first")
    df = df[df["listing_id"].notna()]
    df["snapshot_date"] = snapshot_date
    n_unique = len(df)

    claimed_total = max([t for t in totals if t], default=None)
    coverage = round(n_unique / claimed_total, 3) if claimed_total else None
    print(f"[{snapshot_date}] files={len(payload_files)} raw_rows={n_raw} "
          f"unique={n_unique} site_total={claimed_total} coverage={coverage}")
    if coverage and coverage < 0.9:
        print("  WARNING: coverage <90% of site total. Capture more zoomed-in "
              "quadrant payloads and re-run this snapshot.")

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
        "raw_rows": n_raw, "unique_listings": n_unique,
        "site_total": claimed_total, "coverage": coverage,
        "run_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    }])
    if log_path.exists():
        log_row = pd.concat([pd.read_csv(log_path), log_row], ignore_index=True)
    log_row.to_csv(log_path, index=False)

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

if __name__ == "__main__":
    import sys
    files = sys.argv[1:] or ["map.json"]
    ingest_snapshot(files)
