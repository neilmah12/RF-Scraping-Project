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

Rent hygiene (added 2026-11-23, from the 133-listing map vs detail comparison):
`rent` is kept exactly as the page states it. Beside it, per suite row:
  rent_clean     rent, but a rent of $0 (or less) is missing, not a price
  rent_flag      '' ok | 'zero' | 'jump' (>= 1.8x the highest rent of any suite with FEWER
                 beds while NOT meaningfully bigger: sqft ratio < 1.3, both sqft known; the
                 628725 3BR at $3,690 = 2x its $1,845 2BR with less sqft) |
                 'ppsf_high' (> $6.00 per sqft) | 'ppsf_low' (< $0.60 per sqft, sqft >= 200)
  available_flag immediate | dated | none | call | ''  (src/availability.py)
  asking_ok      rent_clean present, rent_flag empty AND not "No Vacancy". This is the column
                 for asking-rent statistics. A flag never deletes a row: the raw suite is kept
                 and the reason is on the row.
Listing rows add asking_lo / asking_hi (min / max rent over asking_ok suites), n_suites_flagged
and n_no_vacancy, so the raw `price_range_*` and the cleaned range sit side by side.

Floor Plans block (userscript v1.7+, `floorplans` key on a capture): one row per suite per
capture in `detail_floorplans`: beds, baths, rent, sqft, furnished, lease_term, availability,
utilities, deposit (text and number), the suite-group header, and the raw text lines. These
are the fields the JSON-LD does not carry. When a page has NO usable JSON-LD (source
`userscript:dom`), its suites are built from the Floor Plans block and marked
suite_source='floorplans' so the listing is not lost; otherwise suite_source='ld+json'.

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
import availability  # same rule as the map ingest, see its header

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
            "suite_source": "ld+json",
        })
    return rows


PPSF_HIGH, PPSF_LOW, JUMP_RATIO, JUMP_SQFT_RATIO = 6.0, 0.6, 1.8, 1.3


def _flag_suites(rows):
    """Add rent_clean / rent_flag / available_flag / asking_ok to the suite rows of ONE capture."""
    for r in rows:
        rent = r.get("rent")
        r["rent_clean"] = rent if (pd.notna(rent) and rent > 0) else None
        r["rent_flag"] = "zero" if (pd.notna(rent) and rent <= 0) else ""
        r["available_flag"] = availability.parse(r.get("availability"), str(r.get("captured_at") or "")[:10])[1]
    # jump vs a suite with fewer beds, only when both sqft are known and the bigger one is not bigger
    for r in rows:
        if r["rent_flag"] or r["rent_clean"] is None or pd.isna(r.get("beds")) or pd.isna(r.get("sqft")):
            continue
        smaller = [o for o in rows if o is not r and o["rent_clean"] is not None
                   and pd.notna(o.get("beds")) and o["beds"] < r["beds"] and pd.notna(o.get("sqft")) and o["sqft"] > 0]
        if not smaller:
            continue
        top = max(smaller, key=lambda o: o["rent_clean"])
        if r["rent_clean"] >= JUMP_RATIO * top["rent_clean"] and r["sqft"] / top["sqft"] < JUMP_SQFT_RATIO:
            r["rent_flag"] = "jump"
    for r in rows:
        if r["rent_flag"] or r["rent_clean"] is None:
            continue
        sq = r.get("sqft")
        if pd.notna(sq) and sq >= 200:
            ppsf = r["rent_clean"] / sq
            if ppsf > PPSF_HIGH:
                r["rent_flag"] = "ppsf_high"
            elif ppsf < PPSF_LOW:
                r["rent_flag"] = "ppsf_low"
    for r in rows:
        r["asking_ok"] = bool(r["rent_clean"] is not None and not r["rent_flag"] and r["available_flag"] != "none")
    return rows


def _beds_from_text(s):
    s = str(s or "").lower()
    if re.search(r"studio|bachelor", s):
        return 0
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def _money(s):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _floorplan_rows(listing_id, captured_at, fp, source):
    rows = []
    for k, u in enumerate((fp or {}).get("units") or []):
        pu = u.get("parsed") or {}
        rows.append({
            "listing_id": listing_id, "captured_at": captured_at, "fp_idx": k, "source": source,
            "key": u.get("key"), "unitname": u.get("unitname"),
            "group_header": " | ".join(u.get("group") or []),
            "beds": _beds_from_text(pu.get("beds") or u.get("unitname")),
            "baths": pd.to_numeric(pu.get("baths"), errors="coerce"),
            "rent": pd.to_numeric(pu.get("rent"), errors="coerce"),
            "sqft": pd.to_numeric(pu.get("sqft"), errors="coerce"),
            "furnished": pu.get("furnished") or None, "lease_term": pu.get("lease_term") or None,
            "availability": pu.get("availability") or None,
            "utilities_included": pu.get("utilities") or None,
            "deposit_text": pu.get("deposit") or None, "deposit": _money(pu.get("deposit")),
            "raw_lines": " / ".join((u.get("lines") or [])[:60]),
        })
    return rows


def _suites_from_floorplans(listing_id, captured_at, fp):
    """Suite rows built from the Floor Plans block, for a page with no usable JSON-LD."""
    rows = []
    for k, f in enumerate(_floorplan_rows(listing_id, captured_at, fp, "userscript:dom")):
        rows.append({
            "listing_id": listing_id, "captured_at": captured_at, "suite_idx": k,
            "suite_name": f["unitname"] or "", "grouped_units": False,
            "suite_desc": f["group_header"], "unit_type": None,
            "beds": f["beds"], "baths": f["baths"], "sqft": f["sqft"], "rent": f["rent"],
            "availability": f["availability"], "utilities_included": f["utilities_included"],
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


_RAW_YEAR = re.compile(r"Year Built\s*\n\s*(\d{4})")
_RAW_UNITS = re.compile(r"# of Units\s*\n\s*([\d,]+)")


def _building_facts(c):
    """Year built, unit count and parking from a capture.

    v1.8+ captures carry a `building` block read from the page. Older captures only have
    these where the facts happened to fall inside the 4,000-character promotions text, so
    that is used as a fallback and the source column says which one applied.
    """
    b = c.get("building") if isinstance(c.get("building"), dict) else None
    if b:
        return {"year_built": b.get("year_built"), "n_units_building": b.get("n_units"),
                "parking_text": b.get("parking") or None, "building_source": "block"}
    raw = (c.get("promotions") or {}).get("raw") or ""
    y, u = _RAW_YEAR.search(raw), _RAW_UNITS.search(raw)
    if y or u:
        return {"year_built": int(y.group(1)) if y else None,
                "n_units_building": int(u.group(1).replace(",", "")) if u else None,
                "parking_text": None, "building_source": "promo_raw"}
    return {"year_built": None, "n_units_building": None, "parking_text": None, "building_source": ""}


def parse_detail_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    caps = data.get("captures") if isinstance(data, dict) else data
    L, S, P, F = [], [], [], []
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
        fp = c.get("floorplans") if isinstance(c.get("floorplans"), dict) else None
        if not suites and fp:
            suites = _suites_from_floorplans(lid, at, fp)
            for s_ in suites:
                s_["suite_source"] = "floorplans"
        _flag_suites(suites)
        if fp:
            F.extend(_floorplan_rows(lid, at, fp, c.get("source")))
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
            "asking_lo": min([s["rent_clean"] for s in suites if s["asking_ok"]], default=None),
            "asking_hi": max([s["rent_clean"] for s in suites if s["asking_ok"]], default=None),
            "n_suites_flagged": sum(1 for s in suites if s["rent_flag"]),
            "n_no_vacancy": sum(1 for s in suites if s["available_flag"] == "none"),
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
            **_building_facts(c),
            "n_units_js": len(c["units_js"]) if isinstance(c.get("units_js"), list) else None,
        })
        S.extend(suites); P.extend(promos)
    return pd.DataFrame(L), pd.DataFrame(S), pd.DataFrame(P), pd.DataFrame(F)


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
    F = pd.concat([p[3] for p in parts], ignore_index=True)
    # Downloads from the userscript are CUMULATIVE (the browser store is not cleared between
    # sessions), so the same capture arrives in several files. Keep one copy of each row.
    L = L.drop_duplicates(["listing_id", "captured_at"], keep="last")
    S = S.drop_duplicates(["listing_id", "captured_at", "suite_idx"], keep="last")
    P = P.drop_duplicates(["listing_id", "captured_at", "promo_idx"], keep="last") if len(P) else P
    F = F.drop_duplicates(["listing_id", "captured_at", "fp_idx"], keep="last") if len(F) else F
    out = {}
    for name, df, keys in [("detail_listings", L, ["listing_id", "captured_at"]),
                           ("detail_suites", S, ["listing_id", "captured_at"]),
                           ("detail_promos", P, ["listing_id", "captured_at"]),
                           ("detail_floorplans", F, ["listing_id", "captured_at"])]:
        path = data_dir / f"{name}.parquet"
        merged = _merge(_load(path), df, keys)
        _save(merged, path)
        merged.to_csv(data_dir / f"{name}.csv", index=False)  # Excel mirror
        out[name] = merged
    if verbose:
        print(f"[detail] files={len(detail_files)} captures={len(L)} unique_listings={L.listing_id.nunique()} "
              f"suite_rows={len(S)} promo_rows={len(P)} floorplan_rows={len(F)} "
              f"| store now: {len(out['detail_listings'])} captures, {len(out['detail_suites'])} suites, "
              f"{len(out['detail_promos'])} promos, {len(out['detail_floorplans'])} floorplans")
        if len(S) and "rent_flag" in S:
            fl = S["rent_flag"].value_counts().to_dict()
            fl.pop("", None)
            print(f"[detail] rent flags in this load: {fl or 'none'} | asking_ok {int(S['asking_ok'].sum())}/{len(S)}"
                  f" | no-vacancy suites {int((S['available_flag'] == 'none').sum())}")
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


def _self_check():
    """Rules for rent_clean / rent_flag / asking_ok on synthetic suites. Run: python3 src/rentfaster_detail_ingest.py --check"""
    def suite(beds, sqft, rent, avail="Immediate"):
        return {"captured_at": "2026-11-23T12:00:00Z", "beds": beds, "sqft": sqft, "rent": rent, "availability": avail}
    cases = [
        ("zero rent is missing, not a price", [suite(2, 900, 0.0)], ["zero"], [False]),
        ("3BR double the 2BR with less sqft is a jump", [suite(2, 1185, 1845), suite(3, 1165, 3690)], ["", "jump"], [True, False]),
        ("3BR double the 2BR but much bigger is NOT a jump", [suite(2, 800, 1800), suite(3, 1500, 3600)], ["", ""], [True, True]),
        ("no sqft, no jump verdict", [suite(2, None, 1845), suite(3, None, 3690)], ["", ""], [True, True]),
        ("ppsf high", [suite(1, 400, 3000)], ["ppsf_high"], [False]),
        ("ppsf low", [suite(2, 900, 400)], ["ppsf_low"], [False]),
        ("No Vacancy stays in the data, leaves asking stats", [suite(1, 500, 1300, "No Vacancy")], [""], [False]),
        ("Call for Availability keeps its asking rent", [suite(1, 500, 1300, "Call for Availability")], [""], [True]),
        ("normal ladder", [suite(0, 400, 1100), suite(1, 600, 1350), suite(2, 900, 1700)], ["", "", ""], [True, True, True]),
    ]
    bad = 0
    for name, rows, want_flags, want_ok in cases:
        rows = _flag_suites([dict(r) for r in rows])
        got = ([r["rent_flag"] for r in rows], [r["asking_ok"] for r in rows])
        ok = got == (want_flags, want_ok)
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"  got {got} want {(want_flags, want_ok)}"))
    print("PASS" if not bad else f"{bad} FAILURE(S)")
    return bad


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--check"]:
        raise SystemExit(1 if _self_check() else 0)
    ingest_detail(sys.argv[1:] or ["rf_detail.json"])
