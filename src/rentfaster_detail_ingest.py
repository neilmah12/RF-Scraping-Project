"""
Rentfaster listing-detail ingest (companion to rentfaster_ingest.py)
====================================================================
Reads `rf_detail_*.json` files exported by the browser userscript that records the
structured data (schema.org ld+json) and the Promotions block of each listing
page you open. Manual browsing only: this ingests whatever you saved, nothing is
fetched. It is a sample of the pages you chose to look at, NOT a market sample.

File shape:
  {"kind": "rentfaster_detail", "captured_at": ..., "captures": [
     {"ts", "url", "listing_id", "source",
      "promotions": {"raw", "promos": [{type, headline, body, fields}], "html"},
      "data": {schema.org ItemPage -> mainEntity: ApartmentComplex with
               containsPlace[] (one entry per suite group), priceRange, geo, address,
               amenityFeature[], parentOrganization, ...}} ...]}

Outputs (parquet with CSV fallback, same convention as the map ingest), keyed on
(listing_id, captured_at) so a re-run of the same file replaces, never duplicates,
and repeat visits to a page are kept as separate captures:
  detail_listings   one row per page capture: address, geo, priceRange, landlord,
                    amenities, pets, n suites, promo roll-up
  detail_suites     one row per containsPlace entry: beds, baths, sqft, rent,
                    availability, utilities included. This is the per-suite-type
                    rent that map.json cannot give for 'blended'/'inferred' listings
  detail_promos     one row per promo entry: type, headline, terms text, valid
                    from/until, months_free / cash_off parsed from the text

Promo caveats: the Promotions block carries the landlord's terms and a 'Valid from'
date (often old, e.g. 2023/2024, i.e. a standing offer) and sometimes an expiry
inside the text ("valid on or before August 1, 2026"). promo_expired_at_capture is
set when that date is earlier than the capture date.

Usage:
  from rentfaster_detail_ingest import ingest_detail, compare_with_map
  ingest_detail(["rf_detail_2026-11-04.json"], data_dir="rf_data")
  compare_with_map("rf_data")     # detail suite rents vs map.json price range / tier
"""
import json
import re
from pathlib import Path

import pandas as pd

from rentfaster_ingest import _save, _load, parse_promo_terms

_DATE_RE = re.compile(r"([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})")
_EXPIRY_RE = re.compile(
    r"(?:on\s+or\s+before|until|through|expires?(?:\s+on)?|ends?(?:\s+on)?|valid\s+to)\s+"
    r"([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})", re.I)


def _to_date(s):
    try:
        return pd.to_datetime(s.replace(".", ""), format="mixed")
    except (ValueError, TypeError, AttributeError):
        return pd.NaT


def _price_range(s):
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", s or "")]
    return (min(nums), max(nums)) if nums else (None, None)


def _prop(entry, name):
    for a in entry.get("additionalProperty") or []:
        if a.get("name") == name:
            return a.get("value")
    return None


def _suite_rows(listing_id, captured_at, me):
    rows = []
    for k, p in enumerate(me.get("containsPlace") or []):
        price = ((p.get("potentialAction") or {}).get("price"))
        sqft = (p.get("floorSize") or {}).get("value") or _prop(p, "Square Feet")
        full = p.get("numberOfFullBathrooms") or 0
        part = p.get("numberOfPartialBathrooms") or 0
        rows.append({
            "listing_id": listing_id, "captured_at": captured_at, "suite_idx": k,
            "suite_name": (p.get("name") or "").strip(),
            "grouped_units": str(p.get("description") or "").lower().startswith("multiple units"),
            "suite_desc": p.get("description"),
            "unit_type": p.get("@type"),
            "beds": p.get("numberOfBedrooms"),
            "baths": full + 0.5 * part,
            "sqft": pd.to_numeric(sqft, errors="coerce"),
            "rent": pd.to_numeric(price, errors="coerce"),
            "availability": _prop(p, "Availability Date"),
            "utilities_included": _prop(p, "Utilities Included"),
        })
    return rows


def _promo_rows(listing_id, captured_at, promos):
    rows = []
    cap = pd.to_datetime(captured_at, utc=True).tz_localize(None)
    for k, pr in enumerate(promos or []):
        fields = pr.get("fields") or {}
        text = " ".join(x for x in [pr.get("headline"), pr.get("body"),
                                    *[str(v) for v in fields.values()]] if x)
        vf = next((v for kk, v in fields.items() if "valid from" in kk.lower()), None)
        vu = next((v for kk, v in fields.items() if kk.lower() in ("valid until", "valid")), None)
        m = _DATE_RE.search(vf or "")
        valid_from = _to_date(m.group(1)) if m else pd.NaT
        exp_txt = _DATE_RE.search(vu or "") or _EXPIRY_RE.search(text)
        expires = _to_date(exp_txt.group(1)) if exp_txt else pd.NaT
        terms = parse_promo_terms(text)
        rows.append({
            "listing_id": listing_id, "captured_at": captured_at, "promo_idx": k,
            "promo_type": pr.get("type"), "headline": (pr.get("headline") or "")[:500],
            "body": (pr.get("body") or "")[:500], "fields_json": json.dumps(fields),
            "valid_from": valid_from, "expires": expires,
            "age_days_at_capture": (cap - valid_from).days if pd.notna(valid_from) else None,
            "expired_at_capture": bool(pd.notna(expires) and expires < cap),
            **{k2: terms[k2] for k2 in ("special_type", "months_free", "months_free_upto",
                                         "promo_dollars", "promo_components")},
        })
    return rows


def parse_detail_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    caps = data.get("captures") if isinstance(data, dict) else data
    L, S, P = [], [], []
    for c in caps or []:
        try:
            lid = int(c.get("listing_id"))
        except (TypeError, ValueError):
            continue
        at = c.get("ts")
        d = c.get("data") if isinstance(c.get("data"), dict) else {}
        me = d.get("mainEntity") or {}
        addr = me.get("address") or {}
        geo = me.get("geo") or {}
        org = me.get("parentOrganization") or {}
        pr_lo, pr_hi = _price_range(me.get("priceRange"))
        suites = _suite_rows(lid, at, me)
        promos = _promo_rows(lid, at, (c.get("promotions") or {}).get("promos"))
        amen = [a.get("name") for a in me.get("amenityFeature") or []
                if str(a.get("value")).lower() == "true"]
        L.append({
            "listing_id": lid, "captured_at": at, "url": c.get("url"), "source": c.get("source"),
            "name": me.get("name"), "slogan": (me.get("slogan") or "").strip(),
            "street": addr.get("streetAddress"), "city": addr.get("addressLocality"),
            "postal_code": addr.get("postalCode"),
            "latitude": pd.to_numeric(geo.get("latitude"), errors="coerce"),
            "longitude": pd.to_numeric(geo.get("longitude"), errors="coerce"),
            "price_range_raw": me.get("priceRange"), "price_range_lo": pr_lo, "price_range_hi": pr_hi,
            "n_suite_entries": len(suites),
            "n_distinct_beds": len({s["beds"] for s in suites if s["beds"] is not None}),
            "landlord": org.get("name"), "landlord_url": org.get("url"),
            "pets": (me.get("petsAllowed") or "").replace("&amp;", "&"),
            "smoking": me.get("smokingAllowed"),
            "building_description": (me.get("description") or "")[:500],
            "amenities": "; ".join(amen), "n_amenities": len(amen),
            "n_promos": len(promos),
            "promo_types": ",".join(sorted({p["promo_type"] for p in promos if p["promo_type"]})),
            "promo_months_free": max([p["months_free"] for p in promos if p["months_free"] is not None], default=None),
            "promo_any_expired_at_capture": any(p["expired_at_capture"] for p in promos),
        })
        S.extend(suites); P.extend(promos)
    return pd.DataFrame(L), pd.DataFrame(S), pd.DataFrame(P)


def _merge(existing, new, keys):
    if existing is None or existing.empty:
        return new
    if new.empty:
        return existing
    drop = existing.merge(new[keys].drop_duplicates(), on=keys, how="left", indicator=True)
    keep = existing[(drop["_merge"] == "left_only").values]
    return pd.concat([keep, new], ignore_index=True)


def ingest_detail(detail_files, data_dir="rf_data", verbose=True):
    data_dir = Path(data_dir); data_dir.mkdir(parents=True, exist_ok=True)
    parts = [parse_detail_file(f) for f in detail_files]
    L = pd.concat([p[0] for p in parts], ignore_index=True)
    S = pd.concat([p[1] for p in parts], ignore_index=True)
    P = pd.concat([p[2] for p in parts], ignore_index=True)
    out = {}
    for name, df, keys in [("detail_listings", L, ["listing_id", "captured_at"]),
                           ("detail_suites", S, ["listing_id", "captured_at"]),
                           ("detail_promos", P, ["listing_id", "captured_at"])]:
        path = data_dir / f"{name}.parquet"
        merged = _merge(_load(path), df, keys)
        _save(merged, path)
        merged.to_csv(data_dir / f"{name}.csv", index=False)  # Excel mirror
        out[name] = merged
    if verbose:
        print(f"[detail] files={len(detail_files)} captures={len(L)} unique_listings={L.listing_id.nunique()} "
              f"suite_rows={len(S)} promo_rows={len(P)} "
              f"| store now: {len(out['detail_listings'])} captures, {len(out['detail_suites'])} suites, "
              f"{len(out['detail_promos'])} promos")
    return out


def compare_with_map(data_dir="rf_data", tol=1.0):
    """Check detail pages against the map.json snapshot nearest in time to each capture.

    Returns a DataFrame, one row per detail capture found in snapshots:
      price_lo/hi (map) vs suite min/max rent (detail), n_suite_types (map 'units')
      vs n_suite_entries (detail), and rent_confidence, so you can see how often the
      map tier was right and how many blended listings the detail resolves."""
    data_dir = Path(data_dir)
    L = _load(data_dir / "detail_listings.parquet")
    S = _load(data_dir / "detail_suites.parquet")
    snaps = _load(data_dir / "snapshots.parquet")
    if L is None or S is None or snaps is None:
        raise FileNotFoundError("need detail_listings, detail_suites and snapshots in data_dir")
    L = L.copy()
    L["cap_date"] = pd.to_datetime(L["captured_at"], utc=True).dt.tz_localize(None).dt.normalize()
    snaps = snaps.copy()
    snaps["snap_date"] = pd.to_datetime(snaps["snapshot_date"])
    rows = []
    for r in L.itertuples():
        s = snaps[snaps.listing_id == r.listing_id]
        if s.empty:
            continue
        s = s.iloc[(s.snap_date - r.cap_date).abs().argsort()[:1]].iloc[0]
        su = S[(S.listing_id == r.listing_id) & (S.captured_at == r.captured_at)]
        rows.append({
            "listing_id": r.listing_id, "captured_at": r.captured_at, "map_snapshot": s.snapshot_date,
            "days_apart": abs((s.snap_date - r.cap_date).days),
            "rent_confidence": s.rent_confidence, "map_units": s.n_suite_types,
            "detail_suite_entries": len(su), "detail_distinct_beds": su.beds.nunique(),
            "map_price_lo": s.price_lo, "map_price_hi": s.price_hi,
            "detail_rent_min": su.rent.min(), "detail_rent_max": su.rent.max(),
            "lo_match": abs((s.price_lo or 0) - (su.rent.min() or 0)) <= tol,
            "hi_match": abs((s.price_hi or 0) - (su.rent.max() or 0)) <= tol,
            "map_beds": f"{s.beds_lo}-{s.beds_hi}",
            "detail_beds": f"{su.beds.min()}-{su.beds.max()}",
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    ingest_detail(sys.argv[1:] or ["rf_detail.json"])
