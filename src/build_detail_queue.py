"""
Build the monthly listing-detail queue (which listing pages are worth opening)
==============================================================================
Reads the stored snapshots (and detail captures, if any) and writes a prioritised
CSV of listing URLs for the detail-capture userscript to walk through. It makes no
requests; it only decides WHICH pages deserve a look, so a monthly pass is a few
hundred pages instead of every listing.

Priority (lower number = open first):
  1  priority list     listing_id in an optional file you maintain (buildings being underwritten)
  2  new+ambiguous     first appeared in the latest snapshot AND rent_confidence blended/inferred
  3  ambiguous+moved   blended/inferred AND price range or suite-type count changed vs the previous snapshot
  4  ambiguous         blended/inferred with no detail capture in the last `stale_days`
  5  new, clear rent   new direct/certain listings (detail adds sqft, utilities, promo terms, not the rent)
  (everything else is left out; `include_all=True` appends it at priority 6)
`max_pages` cuts the sheet at a daily/monthly cap after sorting by priority.

A listing is skipped if it already has a detail capture newer than `fresh_days`
(default 30), unless it is on the priority list.

Usage:
  from build_detail_queue import build_queue
  q = build_queue("rf_data", out_csv="rf_data/detail_queue_2026-11-04.csv")
  # optional: build_queue("rf_data", priority_ids_file="underwriting_ids.txt")
"""
from pathlib import Path

import pandas as pd

BASE = "https://www.rentfaster.ca"


def _read(data_dir, name, **kw):
    p = Path(data_dir) / name
    return pd.read_csv(p, dtype={"listing_id": str}, **kw) if p.exists() else None


def build_queue(data_dir="rf_data", out_csv=None, snapshot_date=None, fresh_days=30,
                stale_days=90, priority_ids_file=None, include_all=False, seed=2026, max_pages=None):
    snaps = _read(data_dir, "snapshots.csv")
    if snaps is None or snaps.empty:
        raise FileNotFoundError(f"no snapshots.csv in {data_dir}")
    dates = sorted(snaps["snapshot_date"].unique())
    snapshot_date = snapshot_date or dates[-1]
    prev_dates = [d for d in dates if d < snapshot_date]
    prev_date = prev_dates[-1] if prev_dates else None

    cur = snaps[snaps["snapshot_date"] == snapshot_date].drop_duplicates("listing_id").set_index("listing_id")
    prev = (snaps[snaps["snapshot_date"] == prev_date].drop_duplicates("listing_id").set_index("listing_id")
            if prev_date else pd.DataFrame())
    first_seen = snaps.groupby("listing_id")["snapshot_date"].min()

    # last detail capture per listing
    det = _read(data_dir, "detail_listings.csv")
    last_detail = {}
    if det is not None and not det.empty:
        det["_t"] = pd.to_datetime(det["captured_at"], utc=True, errors="coerce")
        last_detail = det.groupby("listing_id")["_t"].max().to_dict()
    ref = pd.Timestamp(snapshot_date, tz="UTC")

    prio_ids = set()
    if priority_ids_file:
        prio_ids = {l.strip() for l in Path(priority_ids_file).read_text().splitlines() if l.strip()}

    rows = []
    for lid, r in cur.iterrows():
        age = (ref - last_detail[lid]).days if lid in last_detail and pd.notna(last_detail[lid]) else None
        ambiguous = r.get("rent_confidence") in ("blended", "inferred")
        is_new = first_seen.get(lid) == snapshot_date and prev_date is not None
        moved = False
        if lid in prev.index:
            p = prev.loc[lid]
            moved = (r["price_lo"] != p["price_lo"]) or (r["price_hi"] != p["price_hi"]) \
                or (r["n_suite_types"] != p["n_suite_types"])
        if lid in prio_ids:
            pr, why = 1, "priority list"
        elif age is not None and age < fresh_days:
            continue
        elif is_new and ambiguous:
            pr, why = 2, "new, blended/inferred"
        elif ambiguous and moved:
            pr, why = 3, "blended/inferred, range changed"
        elif ambiguous and (age is None or age >= stale_days):
            pr, why = 4, "blended/inferred, no recent detail"
        elif is_new:
            pr, why = 5, "new, clear rent"
        elif include_all:
            pr, why = 6, "other"
        else:
            continue
        link = r.get("link") or ""
        rows.append({
            "priority": pr, "reason": why, "listing_id": lid,
            "url": BASE + link if str(link).startswith("/") else link,
            "address": r.get("address_intro") or r.get("address_slug"),
            "city": r.get("city"), "community": r.get("community"), "user_id": r.get("user_id"),
            "type": r.get("type"), "rent_confidence": r.get("rent_confidence"),
            "price_lo": r.get("price_lo"), "price_hi": r.get("price_hi"),
            "n_suite_types": r.get("n_suite_types"), "first_seen": first_seen.get(lid),
            "days_since_detail": age,
        })
    q = pd.DataFrame(rows)
    if q.empty:
        print(f"[queue] nothing to queue for {snapshot_date}")
        return q
    # random order inside a priority band so a poster's listings are not hit back to back
    q = (q.sample(frac=1, random_state=seed).sort_values("priority", kind="stable").reset_index(drop=True))
    if max_pages:
        q = q.head(max_pages)
    q.insert(0, "queue_pos", range(1, len(q) + 1))
    print(f"[queue] {snapshot_date} vs {prev_date}: {len(q)} pages | "
          + ", ".join(f"P{k}={v}" for k, v in q["priority"].value_counts().sort_index().items()))
    if out_csv:
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        q.to_csv(out_csv, index=False)
    return q
