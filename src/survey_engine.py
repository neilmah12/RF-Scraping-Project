"""Rental survey engine: confirmed comps -> survey workbook (Phase 1 of .plans/rental-survey-engine-spec.md).

    # 1. queue the comps' detail pages for the userscript (run in Neil's browser)
    python3 src/survey_engine.py queue data/surveys/<id>/buildings.csv --out detail_queue_survey_<id>.csv

    # 1b. after the run: pull the comps' hero images out to read image-only offers (userscript v2.0)
    python3 src/survey_engine.py images data/surveys/<id>/ --images ../rf-data/raw/*/images/*.json --out <dir>

    # 2. build the workbook from the spec plus whatever detail captures exist
    python3 src/survey_engine.py build data/surveys/<id>/ \\
        --detail ../rf-data/raw/*/detail/*.json --map ../rf-data/raw/*/map/*.json \\
        --queue-log ../rf-data/raw/*/queue_log/*.csv --as-of 2026-11-24 --out survey.xlsx

A survey folder holds:
  buildings.csv     one row per building: role (subject|comp), rentfaster (URL or id, blank for
                    a building with no listing), building_name, area, address, product,
                    year_built, developer, notes. Any non-blank value overrides the capture.
                    Optional incentive, months_free, lease_term, cash_off: Neil's last word for this
                    survey (phone call, secret shop); overrides listing text and image.
  data/image_reads.csv (shared by all surveys) offers read from listing images: listing_id,
                    image_url, read_on, offer, months_free, lease_term, cash_off, read_by, notes.
                    offer 'none' = read, no offer in the image (stops it being flagged again).
                    months_free 'free_until:YYYY-MM-DD' = "don't pay until <date>", valued at the
                    survey's as-of date.
                    Keyed by image URL, so a reading stops applying when the banner changes.
  manual_suites.csv optional: suite rows for buildings without a listing or with phone /
                    secret-shopper / rent-roll / website rents. Columns: building_name, suite,
                    unit_type, sf, rent, incentive, months_free, lease_term, cash_off, status,
                    parking, parking_monthly, utilities, notes, link, source, observed_date.
                    A building with manual rows uses ONLY its manual rows.
  settings.json     optional: {"title": "...", "parking_in_net": false, "stale_days": 14}

Output tabs: Survey (the client sheet, laid out for Survey_Map's parser: title row 1, headers
row 2, data from row 3, merged building cells, subject row in dark fill), Flags, Call List,
Intel (change log and summary stats; internal, copied to a client only by choice).

Rules (spec section 7), each a decision recorded in CLAUDE.md:
  * net rent = base - (base x months free + cash off) / lease term (+ parking when parking_in_net).
    Lease term defaults to 12 months. The incentive is held as NUMBERS in helper columns right
    of Link, so the Excel formula never searches text (Fort Sask's SEARCH("1 month free") missed
    every other phrasing). Edit a helper cell and the net rent follows.
  * "net effective" / "already includes the incentive" language: the advertised rent IS net,
    nothing is deducted. "Amortized over X months" does NOT mean that.
  * several listed units of one floor plan (same beds, baths, sqft) collapse to one row at the
    lower-median asking rent (a rent actually listed), with the count and range in Notes.
  * No Vacancy suites -> Status Waitlist (rent kept); Call for Availability -> Status blank,
    yellow, and a call question; listing gone -> Off Market at its last captured rents, pink.
  * a rent older than stale_days (default 14) at the as-of date is flagged.
  * room shares are excluded and flagged. Nothing here ever contacts rentfaster.ca.
  * offers: listing TEXT beats the listing IMAGE. An image reading applies only while the listing
    still shows that exact image, never after a text offer has disappeared under an unchanged
    banner (likely leftover), and is flagged to confirm once older than stale_days.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import rentfaster_detail_ingest as rdi  # noqa: E402
from parse_rf_detail import find_parking  # noqa: E402

NAVY, WHITE, BAND, YELLOW, PINK = "FF002060", "FFFFFFFF", "FFEDEDED", "FFFFFF00", "FFFFC7CE"
SAFE_PREFIX = "https://www.rentfaster.ca/properties/"
ID_RE = re.compile(r"(\d{5,7})(?:[/?#]|$)")
NET_EFFECTIVE_RE = re.compile(r"net[- ]effective|already (?:includes?|reflects?)[^.]{0,40}incentive|"
                              r"incentive[^.]{0,30}already (?:included|applied|reflected)", re.I)
ROOM_SHARE_RE = re.compile(r"room ?share|roommate|shared (?:house|home|accommodation)|room for rent", re.I)
TERM_RE = re.compile(r"(\d{1,2})[- ]?(?:month|mo)s?\.?[- ]lease|lease (?:length|term)\s*:?\s*(\d{1,2})|"
                     r"on (?:a |an )?(\d{1,2})[- ]?(?:month|mo)", re.I)
BEDWORD_RE = {0: r"\b(bachelor|studio)\b", 1: r"\b(1|one)[- ]?(bed|bdrm|bedroom|br)\b",
              2: r"\b(2|two)[- ]?(bed|bdrm|bedroom|br)\b", 3: r"\b(3|three)[- ]?(bed|bdrm|bedroom|br)\b",
              4: r"\b(4|four)[- ]?(bed|bdrm|bedroom|br)\b"}

COLUMNS = ["Building Name", "Area", "Address", "Product", "Year Built", "Suite", "Unit Type", "SF",
           "Base Rent", "Rent PSF", "Incentive", "Net Rent", "Net Rent PSF", "Status", "Parking",
           "Parking ($/mo)", "Utilities", "Developer / Manager", "Notes", "Link",
           "Free Months", "Lease Term (mo)", "Cash Off ($)"]
BUILDING_COLS = ["Building Name", "Area", "Address", "Product", "Year Built", "Developer / Manager", "Link"]
OPTIONAL = ["Area", "Suite", "Developer / Manager", "Notes"]  # dropped when empty for every row


# ----------------------------------------------------------------------------- inputs
def listing_id(value) -> str:
    m = ID_RE.search(str(value or "").strip())
    return m.group(1) if m else ""


def read_csv(path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8-sig") as f:
        return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]


def num(v):
    try:
        s = str(v).replace("$", "").replace(",", "").strip()
        return float(s) if s not in ("", "nan", "None") else None
    except ValueError:
        return None


def load_captures(files) -> dict[str, list[dict]]:
    """listing_id -> raw captures, oldest first, deduped on (listing_id, ts)."""
    out, seen = {}, set()
    for f in files:
        for c in (json.load(open(f, encoding="utf-8")).get("captures") or []):
            key = (str(c.get("listing_id")), c.get("ts"))
            if key in seen:
                continue
            seen.add(key)
            out.setdefault(key[0], []).append(c)
    for v in out.values():
        v.sort(key=lambda c: c.get("ts") or "")
    return out


def load_map_history(files, ids) -> dict[str, list[dict]]:
    """listing_id -> [{date, price_lo, price_hi, beds_lo, beds_hi, units, type, community, promos}]."""
    ids, out = set(ids), {}
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        caps = d.get("captures") if isinstance(d, dict) and "captures" in d else [d]
        m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(f).name) or re.search(r"(\d{4})(\d{2})(\d{2})", Path(f).name)
        day = "-".join(m.groups()) if m and len(m.groups()) == 3 else (m.group(1) if m else "")
        got = {}
        for c in caps:
            for x in c.get("listings") or []:
                i = str(x.get("ref_id") or x.get("id"))
                if i in ids and i not in got:
                    got[i] = {"date": day, "price_lo": num(x.get("price")), "price_hi": num(x.get("price2")),
                              "beds_lo": x.get("beds"), "beds_hi": x.get("beds2"), "units": x.get("units"),
                              "type": x.get("type"), "community": x.get("community"),
                              "promos": ",".join(x.get("promotions") or [])}
        for i, r in got.items():
            out.setdefault(i, []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r["date"])
    return out


def load_images(files) -> dict[str, dict]:
    """listing_id -> newest hero image record from userscript v2.0 `rf_images_*.json` files."""
    out = {}
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        if d.get("kind") != "rentfaster_images":
            continue
        for im in d.get("images") or []:
            for i in {str(x) for x in (im.get("listing_ids") or [])} | {str(im.get("listing_id"))}:
                if i not in out or (im.get("ts") or "") > (out[i].get("ts") or ""):
                    out[i] = im
    return out


def load_queue_status(files) -> dict[str, dict]:
    """listing_id -> latest queue-log row (gone / captured / ...)."""
    out = {}
    for f in files:
        for r in read_csv(f):
            i = r.get("listing_id", "")
            if i and (i not in out or r.get("ts", "") >= out[i].get("ts", "")):
                out[i] = r
    return out


# ----------------------------------------------------------------------------- per-capture parsing
def bed_label(beds) -> str:
    if beds is None or (isinstance(beds, float) and pd.isna(beds)):
        return ""
    b = int(beds)
    return "Bachelor" if b == 0 else f"{b} Bed"


def unit_type(beds, baths) -> str:
    b = bed_label(beds)
    if not b:
        return ""
    if baths is None or pd.isna(baths) or baths == 0:
        return b
    ba = int(baths) if float(baths).is_integer() else baths
    return f"{b}/{ba} Bath"


def lease_term(text) -> int | None:
    if re.search(r"\b(one|1|a)[- ]year lease", text or "", re.I):
        return 12
    for m in TERM_RE.finditer(text or ""):
        v = next((g for g in m.groups() if g), None)
        if v and 3 <= int(v) <= 36:
            return int(v)
    return None


INC_SENT_RE = re.compile(
    r"months?(?: of rent)? (?:of )?free|free (?:\w+ )?months?|free (?:rent|first)|first month|\$\s?\d[\d,]*[^.!\n]{0,25}?(?:off|credit|discount|rebate|cash back)"
    r"|save \$|\d\s*% (?:off|reduction)|move[- ]in (?:bonus|credit|gift)|gift card"
    r"|free (?:parking|internet|wifi|cable|utilit)", re.I)
PCT_FIRST_RE = re.compile(r"(\d{1,3})\s*%\s*(?:off|reduction)[^.!\n]{0,40}first month|first month[^.!\n]{0,30}?(\d{1,3})\s*%\s*off", re.I)
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "last": 1, "first": 1, "a": 1}
MONTHS_FALLBACK_RE = re.compile(r"\b(\d|one|two|three|four|last|first|a)\s+months?(?: of rent)?\s+(?:of\s+)?(?:rent\s+)?free"
                                r"|free\s+(\d|one|two|three|four)\s+months?", re.I)
CHROME_RE = re.compile(r"^.*(?:Units Unavailable|Contact Landlord|Add to My Faves|Street View)\s*", re.I)
HALF_RE = re.compile(r"\b(half|1/2|0?\.5)\s*(?:a\s*)?months?\b[^.!\n]{0,20}free", re.I)


def incentive_sentence(text) -> str:
    """The sentence that states the offer. Promo boxes often carry page chrome ('+ 37 Photos',
    'Contact Landlord') around the real line, and descriptions carry ad copy, so only a sentence
    that names concrete terms (months free, $ off, % off, free perk) counts."""
    text = str(text or "").replace("&amp;", "&")
    text = re.sub(r"(?<=[A-Z]{2})(?=[A-Z][a-z])", ". ", text)   # 'LEASEWhat' -> 'LEASE. What'
    text = re.sub(r"(?<=[a-z])(?=[A-Z][a-z]{2,})", ". ", text)   # 'FreeThese' -> 'Free. These'
    for sent in re.split(r"(?<=[.!?])\s+|\n|\s\|\s|\u201c|\u201d", text):
        sent = CHROME_RE.sub("", sent)
        m = INC_SENT_RE.search(sent)
        if m:
            sent = " ".join(sent.split())
            if len(sent) > 90:
                i = max(0, sent.lower().find(m.group(0).lower()) - 40)
                sent = ("..." if i else "") + sent[i:i + 90].rsplit(" ", 1)[0] + "..."
            return sent.strip(" -:\u2022")
    return ""


def incentive_from_capture(c) -> dict:
    """Numeric incentive terms plus display text, from the promo box, slogan and description."""
    me = ((c.get("data") or {}).get("mainEntity") or {}) if isinstance(c.get("data"), dict) else {}
    promos = (c.get("promotions") or {}).get("promos") or []
    promo_txt = "\n".join("\n".join([p.get("headline") or "", p.get("body") or "",
                                     *[f"{k}: {v}" for k, v in (p.get("fields") or {}).items()]]) for p in promos)
    slogan, desc = me.get("slogan") or "", me.get("description") or ""
    fp = c.get("floorplans") if isinstance(c.get("floorplans"), dict) else {}
    fp_txt = re.sub(r"<[^>]+>", "\n", "\n".join(l for u in fp.get("units") or [] for l in u.get("lines") or []))
    source, sent = "", ""
    un_txt = "\n".join(str(u.get("unit_notes") or "") for u in c.get("units_js") or [])
    for src, t in (("promo box", promo_txt), ("floor plans", fp_txt), ("suite notes", un_txt), ("slogan", slogan), ("description", desc)):
        sent = incentive_sentence(t)
        if sent:
            source = src
            break
    terms = rdi.parse_promo_terms(sent) if sent else {}
    months, dollars = terms.get("months_free"), terms.get("promo_dollars")
    pm = PCT_FIRST_RE.search(sent)
    if pm and not months:
        months = int(pm.group(1) or pm.group(2)) / 100
    if HALF_RE.search(sent) and not months:
        months = 0.5
    fb = MONTHS_FALLBACK_RE.search(sent)
    if fb and not months:
        w = (fb.group(1) or fb.group(2)).lower()
        months = float(WORDNUM.get(w, w))
    term = lease_term(sent) or (lease_term(promo_txt) if source == "promo box" else None)
    net_eff = bool(NET_EFFECTIVE_RE.search(" ".join([promo_txt, slogan, desc])))
    flagged_only = bool(promos) and not sent
    return {"display": sent, "source": source, "months_free": months, "cash_off": dollars, "term": term,
            "net_effective": net_eff, "vague": bool(sent) and not months and not dollars,
            "flagged_only": flagged_only}


# ----------------------------------------------------------------------------- image offers
def hero_of(c) -> str:
    """The hero image URL a capture showed. Rentfaster image URLs carry a version hash
    (18343073.v.ed9e...jpg), so a new banner arrives under a new URL."""
    me = ((c.get("data") or {}).get("mainEntity") or {}) if isinstance(c.get("data"), dict) else {}
    im = me.get("image")
    if isinstance(im, list):
        im = im[0] if im else None
    if isinstance(im, dict):
        im = im.get("url") or im.get("contentUrl")
    im = im or (c.get("listing_js") or {}).get("slide") or ""
    return str(im).split("?")[0]


def load_image_reads(path) -> dict[str, dict]:
    """image_url -> the reading of that exact image (latest read wins)."""
    out = {}
    for r in read_csv(path):
        u = (r.get("image_url") or "").split("?")[0]
        if u and (u not in out or r.get("read_on", "") >= out[u].get("read_on", "")):
            out[u] = r
    return out


def has_offer(i) -> bool:
    return bool(i.get("months_free") or i.get("cash_off") or i.get("display"))


def resolve_offer(inc, hist, lid, reads, as_of, stale_days, flag) -> dict:
    """Text beats image (Neil, 2026-11-24: the text is usually more accurate and the banner often
    outlives the offer). An image reading only applies to the exact image it was read from."""
    latest = hist[-1]
    hero = hero_of(latest)
    rd = reads.get(hero)
    if has_offer(inc):
        if rd and (num(rd.get("months_free")) or 0, num(rd.get("cash_off")) or 0) != (inc.get("months_free") or 0, inc.get("cash_off") or 0):
            flag("incentive", f"Text offer '{inc['display']}' used. The listing image (read {rd.get('read_on')}) says "
                              f"'{rd.get('offer')}'. Text wins; confirm which is current", call=True)
        return inc
    before = [c for c in hist[:-1] if has_offer(incentive_from_capture(c))]
    if rd and not (rd.get("offer") or "").strip() or (rd and (rd.get("offer") or "").strip().lower() in ("none", "no offer")):
        return dict(inc, image_read_none=True)  # read, and the image carries no offer: nothing to do
    if rd:
        rd = dict(rd)
        until = re.match(r"free_until:(\d{4}-\d{2}-\d{2})", str(rd.get("months_free") or ""))
        if until:  # 'don't pay until Jan 1': the value shrinks daily, so price it at the as-of date
            days = (date.fromisoformat(until.group(1)) - as_of).days
            rd["months_free"] = round(max(days, 0) / 30.4, 2)
            if days <= 0:
                flag("incentive", f"Image offer '{rd.get('offer')}' ran until {until.group(1)}: expired at {as_of}, not applied")
                return inc
            flag("incentive", f"Image offer '{rd.get('offer')}' is worth {rd['months_free']} months free for a move-in on {as_of} "
                              f"and shrinks daily (free until {until.group(1)})")
        if before and hero_of(before[-1]) == hero:
            flag("incentive", f"Text offer gone since {before[-1]['ts'][:10]} but the unchanged banner still shows "
                              f"'{rd.get('offer')}'. Likely a leftover banner: NOT applied, confirm by call", call=True)
            return inc
        try:
            age = (as_of - date.fromisoformat(rd.get("read_on", "")[:10])).days
        except ValueError:
            age = 0
        if age > stale_days:
            flag("incentive", f"Image offer '{rd.get('offer')}' read {age} days ago (image unchanged since): confirm it still runs", call=True)
        out = dict(inc, display=f"{rd.get('offer')} (listing image)", source="listing image",
                   months_free=num(rd.get("months_free")), cash_off=num(rd.get("cash_off")),
                   term=num(rd.get("lease_term")), vague=False, flagged_only=False)
        if not out["term"] and (out["months_free"] or out["cash_off"]):
            flag("incentive", "Image offer has no lease term: amortized over 12 months by default", call=True)
        return out
    old = sorted((r for u, r in reads.items() if str(r.get("listing_id")) == str(lid)), key=lambda r: r.get("read_on", ""))
    if old:
        flag("incentive", f"Hero image changed since '{old[-1].get('offer')}' was read on {old[-1].get('read_on')}: "
                          f"NOT applied. Read the new image", call=True)
    return inc


def capture_suites(c) -> pd.DataFrame:
    """Suite rows for one capture via the ingest's own parser (hygiene flags included)."""
    tmp = {"captures": [c]}
    p = Path(_scratch()) / "one.json"
    p.write_text(json.dumps(tmp), encoding="utf-8")
    _, S, _, _ = rdi.parse_detail_file(str(p))
    return S


_SCRATCH = None


def _scratch():
    global _SCRATCH
    if _SCRATCH is None:
        import tempfile
        _SCRATCH = tempfile.mkdtemp(prefix="survey_engine_")
    return _SCRATCH


def clean_plan_name(note) -> str:
    """'The Hinton ( 1 Bedroom | 1 Bathroom)' -> 'The Hinton'; 'Cali 1 - 1BR+Den - 1BTH' -> 'Cali 1'."""
    n = re.sub(r"\s*\(.*?\)\s*", " ", str(note or ""))
    n = re.split(r"\s+-\s+\d\s*BR", n, flags=re.I)[0]
    n = " ".join(n.split())
    if re.match(r"^(multiple units matching|unit\s)", n, re.I):
        return ""
    # Generic labels ('2 Bedroom', 'Bachelor', 'Two Bedroom Two Bath') only repeat Unit Type.
    # Kept: anything else, e.g. 'Jr 1 Bedroom', '1 Bedroom + Den', a developer's plan name.
    if GENERIC_PLAN_RE.match(n) or re.fullmatch(r"\d{1,3}", n) or len(n) > 40:
        return ""  # generic, a bare number, or prose (prose goes to Notes, see suite_prose)
    return n


def suite_prose(note) -> str:
    """A unit note that is a sentence rather than a name ('Bright 1 bedroom unit with vinyl plank
    flooring'): kept for Notes, since flooring and finish words are the only condition evidence."""
    n = " ".join(str(note or "").split())
    return (n[:90] + ("..." if len(n) > 90 else "")) if len(n) > 40 else ""


_NUMW = r"(?:\d|one|two|three|four|five)"
GENERIC_PLAN_RE = re.compile(
    rf"^(?:bachelor|studio|{_NUMW}\s*(?:bed(?:room)?s?|bdrms?|br))"
    rf"(?:\s*[,/&-]?\s*{_NUMW}(?:\.5)?\s*(?:bath(?:room)?s?|ba))?\s*(?:suite|apartment|unit|townhouse)?s?$", re.I)


def unit_notes_by_index(c, S=None) -> dict[int, str]:
    """v1.8 `units_js` names each suite's floor plan ('The Hinton', 'Blend', 'Jr 1 Bedroom').
    Its order is not the JSON-LD order, so each suite is matched on bed count and rent
    (then square feet), each units_js entry used once."""
    units = [dict(u, _used=False) for u in (c.get("units_js") or [])]
    out = {}
    if S is None or S.empty or not units:
        return out
    def ub(u):
        m = re.match(r"(\d+)", str(u.get("beds") or ""))
        return 0 if re.search(r"bach|studio", str(u.get("beds") or ""), re.I) else (int(m.group(1)) if m else None)
    for _, r in S.iterrows():
        cands = [u for u in units if not u["_used"] and ub(u) == r["beds"] and num(u.get("price")) == r["rent"]]
        if len(cands) > 1 and pd.notna(r["sqft"]):
            cands = [u for u in cands if num(u.get("sq_feet")) == r["sqft"]] or cands
        if cands:
            cands[0]["_used"] = True
            raw = cands[0].get("unit_notes")
            name = clean_plan_name(raw) or (("note:" + suite_prose(raw)) if suite_prose(raw) else "")
            if name:
                out[int(r["suite_idx"])] = name
    return out


def collapse(S: pd.DataFrame, notes: dict[int, str]) -> list[dict]:
    """One row per floor plan (beds, baths, sqft). Several listed units -> median rent + note."""
    rows = []
    if S.empty:
        return rows
    S = S.copy()
    S["sq_key"] = S["sqft"].round(0)
    for (beds, baths, sq), g in S.groupby(["beds", "baths", "sq_key"], dropna=False, sort=True):
        ok = g[g["asking_ok"]]
        use = ok if len(ok) else g[g["rent_clean"].notna()]
        rents = use["rent_clean"].dropna().tolist()
        flags = sorted({f for f in g["rent_flag"] if f})
        avail = set(g["available_flag"])
        status = "Available" if avail & {"immediate", "dated"} else ("Waitlist" if "none" in avail else "")
        label_notes = sorted({notes[i] for i in g["suite_idx"] if i in notes and not notes[i].startswith("note:")})
        prose = sorted({notes[i][5:] for i in g["suite_idx"] if i in notes and notes[i].startswith("note:")})
        unit_names = [n for n in g["suite_name"] if n and not re.match(r"^\d+ beds?,", n)]
        note = prose[:1]
        if len(g) > 1:
            note.append(f"{len(g)} units listed" + (f", ${min(rents):,.0f}-${max(rents):,.0f}" if len(rents) > 1 and min(rents) != max(rents) else ""))
        if "call" in avail:
            note.append("Call for availability")
        rows.append({
            "beds": beds, "baths": baths, "sqft": None if pd.isna(sq) else float(sq),
            "rent": statistics.median_low(rents) if rents else None,
            "status": status, "call": "call" in avail, "flags": flags,
            "utilities": "; ".join(sorted({str(u) for u in g["utilities_included"] if u and str(u) != "nan"})),
            "suite": " / ".join(label_notes) if label_notes else (unit_names[0].replace("  ", " ") if len(g) == 1 and unit_names else ""),
            "notes": "; ".join(note),
        })
    return rows


# ----------------------------------------------------------------------------- building assembly
def assemble(spec_dir: Path, detail_files, map_files, queue_files, as_of: date, stale_days: int, image_files=(),
             image_reads=None):
    buildings = read_csv(spec_dir / "buildings.csv")
    manual = read_csv(spec_dir / "manual_suites.csv")
    if not buildings:
        raise SystemExit(f"no buildings.csv in {spec_dir}")
    ids = [listing_id(b.get("rentfaster")) for b in buildings]
    caps = load_captures(detail_files)
    mapsh = load_map_history(map_files, [i for i in ids if i])
    qlog = load_queue_status(queue_files)
    imgs = load_images(image_files)
    reads = load_image_reads(image_reads) if image_reads else {}
    out, flags, calls = [], [], []

    for b, lid in zip(buildings, ids):
        role = (b.get("role") or "comp").lower()
        mrows = [m for m in manual if m.get("building_name", "").lower() == b.get("building_name", "").lower()]
        hist = caps.get(lid, []) if lid else []
        latest = hist[-1] if hist else None
        me = ((latest or {}).get("data") or {}).get("mainEntity") or {} if latest and isinstance(latest.get("data"), dict) else {}
        bf = rdi._building_facts(latest) if latest else {}
        mh = mapsh.get(lid, [])
        name = b.get("building_name") or me.get("name") or ""
        addr = b.get("address") or ((me.get("address") or {}).get("streetAddress") or "").split(",")[0]
        product = b.get("product") or (mh[-1]["type"] if mh else "") or \
            next((u.get("type") for u in (latest or {}).get("units_js") or [] if u.get("type")), "")
        bld = {"role": role, "lid": lid, "name": name, "area": b.get("area") or (mh[-1]["community"] if mh else ""),
               "address": addr, "product": product,
               "year_built": b.get("year_built") or (bf.get("year_built") or ""),
               "developer": b.get("developer") or _manager(me),
               "link": b.get("rentfaster") if lid else (mrows[0].get("link") if mrows else ""),
               "phone": me.get("telephone") or ((me.get("parentOrganization") or {}).get("telephone") or ""),
               "captured_at": (latest or {}).get("ts", "")[:10], "rows": [], "off_market": False,
               "n_units": bf.get("n_units_building"), "notes": b.get("notes", "")}
        if lid and not bld["link"].startswith("http"):
            bld["link"] = f"https://www.rentfaster.ca/{lid}"
        qs = []

        def flag(kind, msg, call=False):
            flags.append({"Building": name, "Listing ID": lid, "Flag": kind, "Detail": msg})
            if call:
                qs.append(msg)

        if mrows:
            for m in mrows:
                bld["rows"].append({
                    "suite": m.get("suite", ""), "unit_type": m.get("unit_type", ""), "sqft": num(m.get("sf")),
                    "rent": num(m.get("rent")), "incentive": m.get("incentive", ""),
                    "months_free": num(m.get("months_free")), "term": num(m.get("lease_term")),
                    "cash_off": num(m.get("cash_off")), "status": m.get("status", ""),
                    "parking": m.get("parking", ""), "parking_monthly": num(m.get("parking_monthly")),
                    "utilities": m.get("utilities", ""), "notes": m.get("notes", ""), "call": False})
            src = {m.get("source", "") for m in mrows} - {""}
            flag("source", f"{len(mrows)} manual rows ({', '.join(sorted(src)) or 'source not stated'})")
        elif lid and not latest:
            flag("not captured", "No detail capture for this listing yet: run the survey queue", call=False)
        elif latest:
            desc = me.get("description") or ""
            if ROOM_SHARE_RE.search(" ".join([desc, me.get("name") or "", me.get("slogan") or ""])):
                flag("room share", "Listing reads as a room share: excluded from the survey")
                continue
            S = capture_suites(latest)
            inc = resolve_offer(incentive_from_capture(latest), hist, lid, reads, as_of, stale_days, flag)
            if b.get("incentive") or b.get("months_free") or b.get("cash_off"):
                # Neil's last word for THIS survey (a phone call, a secret shop). Image readings
                # belong in data/image_reads.csv instead, so they expire with the image.
                inc = {"display": b.get("incentive", ""), "source": "entered by hand", "net_effective": False,
                       "months_free": num(b.get("months_free")), "cash_off": num(b.get("cash_off")),
                       "term": num(b.get("lease_term")), "vague": False, "flagged_only": False}
                flag("incentive", f"Incentive entered by hand: '{inc['display']}' (overrides listing text and image)")
            ptypes, psnip, prate = find_parking(desc)
            parking_txt = clean_parking(bf.get("parking_text")) or ptypes.replace("|", ", ")
            gone = qlog.get(lid, {}).get("status") == "gone" and qlog[lid].get("ts", "") > latest.get("ts", "")
            bld["off_market"] = gone
            for r in collapse(S, unit_notes_by_index(latest, S)):
                months = 0 if inc["net_effective"] else inc["months_free"]
                bld["rows"].append({
                    "suite": r["suite"], "unit_type": unit_type(r["beds"], r["baths"]), "beds": r["beds"],
                    "sqft": r["sqft"], "rent": r["rent"],
                    "incentive": ("Net effective (in rent) " if inc["net_effective"] else "") + inc["display"],
                    "months_free": months, "term": inc["term"] or (12 if (months or inc["cash_off"]) else None),
                    "cash_off": None if inc["net_effective"] else inc["cash_off"],
                    "status": "Off Market" if gone else r["status"], "parking": parking_txt,
                    "parking_monthly": num(prate), "utilities": r["utilities"] or "None listed",
                    "notes": "; ".join(x for x in [r["notes"], ", ".join(r["flags"]) and f"rent flag: {', '.join(r['flags'])}"] if x),
                    "call": r["call"]})
                if r["call"]:
                    flag("call for availability", f"{unit_type(r['beds'], r['baths'])}: listed as Call for Availability, confirm rent and vacancy", call=True)
                if r["sqft"] is None:
                    flag("missing sqft", f"{unit_type(r['beds'], r['baths'])}: no square footage on the listing", call=True)
                if r["flags"]:
                    flag("rent hygiene", f"{unit_type(r['beds'], r['baths'])}: {', '.join(r['flags'])}")
            # freshness
            age = (as_of - datetime.fromisoformat(latest["ts"][:10]).date()).days
            if gone:
                flag("off market", f"Listing gone at {qlog[lid]['ts'][:10]}; rents shown are the last captured ({bld['captured_at']})", call=True)
            elif age > stale_days:
                flag("stale", f"Rents captured {bld['captured_at']}, {age} days before {as_of}: refresh before delivery")
            # incentive quality
            if inc["net_effective"]:
                flag("incentive", "Listing says pricing is net effective: no deduction applied")
            elif inc.get("image_read_none"):
                pass  # hero image already read: no offer in it
            elif not inc["display"] and not inc["months_free"] and not inc["cash_off"]:
                hero = me.get("image") or ((latest.get("listing_js") or {}).get("slide") or "")
                if lid in imgs:
                    flag("image check", f"No incentive in the listing text; hero image captured {imgs[lid].get('ts', '')[:10]}. "
                                        f"Read it (`images` command) and enter any offer in buildings.csv")
                else:
                    flag("image check", f"No incentive in the listing text and no hero image captured (userscript v2.0 saves it). "
                                        f"Check the image, offers are often only there: {hero}")
            elif inc["flagged_only"]:
                flag("incentive", "Promo box present but no readable terms: check the listing (image-only offers happen)", call=True)
            elif inc["vague"]:
                flag("incentive", f"Incentive without numeric terms ({inc['source']}): '{inc['display'][:90]}'", call=True)
            elif (inc["months_free"] or inc["cash_off"]) and not inc["term"] and inc.get("source") != "listing image":
                flag("incentive", "Incentive has no stated lease term: amortized over 12 months by default", call=True)
            # missing suite types
            got = {int(r["beds"]) for r in bld["rows"] if r.get("beds") is not None and not pd.isna(r.get("beds"))}
            for k, pat in BEDWORD_RE.items():
                if k not in got and re.search(pat, desc, re.I):
                    flag("missing suite type", f"Description mentions {bed_label(k) or 'Bachelor'} but none is advertised now", call=True)
            if mh:
                for m in mh:
                    for side in ("beds_lo", "beds_hi"):
                        bv = str(m[side] or "").lower()
                        bn = 0 if "studio" in bv or "bach" in bv else (int(re.match(r"\d+", bv).group()) if re.match(r"\d+", bv) else None)
                        if bn is not None and bn not in got:
                            flag("missing suite type", f"Map pull {m['date']} showed a {bed_label(bn)} range; none advertised now", call=True)
                            got.add(bn)
            if bld["n_units"] and bld["n_units"] >= 20 and len({r.get('beds') for r in bld['rows']}) == 1:
                flag("suite mix", f"{bld['n_units']} units but one bed type advertised: mix likely incomplete", call=True)
        if qs:
            calls.append({"Building": name, "Address": addr, "Phone": bld["phone"],
                          "Manager": bld["developer"], "Last capture": bld["captured_at"],
                          "Ask": " | ".join(dict.fromkeys(qs))})
        bld["history"] = hist
        bld["map_history"] = mh
        out.append(bld)
    return out, flags, calls


# ----------------------------------------------------------------------------- intel
def change_log(bld) -> list[dict]:
    rows = []
    for m0, m1 in zip(bld["map_history"], bld["map_history"][1:]):
        if m0["price_lo"] and m1["price_lo"] and (m0["price_lo"], m0["price_hi"]) != (m1["price_lo"], m1["price_hi"]):
            rows.append({"Building": bld["name"], "From": m0["date"], "To": m1["date"], "Source": "map",
                         "Change": f"range ${m0['price_lo'] or 0:,.0f}-{m0['price_hi'] or m0['price_lo'] or 0:,.0f} -> "
                                   f"${m1['price_lo'] or 0:,.0f}-{m1['price_hi'] or m1['price_lo'] or 0:,.0f}"})
        if bool(m0["promos"]) != bool(m1["promos"]):
            rows.append({"Building": bld["name"], "From": m0["date"], "To": m1["date"], "Source": "map",
                         "Change": "incentive flag " + ("added: " + m1["promos"] if m1["promos"] else "dropped")})
    hist = bld["history"]
    for c0, c1 in zip(hist, hist[1:]):
        s0 = {(r["unit_type"], r["sqft"]): r["rent"] for r in _rows_for(c0)}
        s1 = {(r["unit_type"], r["sqft"]): r["rent"] for r in _rows_for(c1)}
        d0, d1 = c0["ts"][:10], c1["ts"][:10]
        for k in s1.keys() - s0.keys():
            rows.append({"Building": bld["name"], "From": d0, "To": d1, "Source": "detail", "Change": f"{k[0]} {k[1] or ''}sf appeared at ${s1[k] or 0:,.0f}"})
        for k in s0.keys() - s1.keys():
            rows.append({"Building": bld["name"], "From": d0, "To": d1, "Source": "detail", "Change": f"{k[0]} {k[1] or ''}sf no longer listed (was ${s0[k] or 0:,.0f})"})
        for k in s0.keys() & s1.keys():
            if s0[k] and s1[k] and s0[k] != s1[k]:
                rows.append({"Building": bld["name"], "From": d0, "To": d1, "Source": "detail",
                             "Change": f"{k[0]} {k[1] or ''}sf ${s0[k]:,.0f} -> ${s1[k]:,.0f} ({(s1[k] - s0[k]) / s0[k]:+.1%})"})
        if hero_of(c0) and hero_of(c1) and hero_of(c0) != hero_of(c1):
            rows.append({"Building": bld["name"], "From": d0, "To": d1, "Source": "detail",
                         "Change": "hero image changed (any offer read from the old image no longer applies)"})
        i0, i1 = incentive_from_capture(c0)["display"], incentive_from_capture(c1)["display"]
        if i0 != i1:
            rows.append({"Building": bld["name"], "From": d0, "To": d1, "Source": "detail",
                         "Change": f"incentive '{i0 or 'none'}' -> '{i1 or 'none'}'"})
    return rows


def _rows_for(c):
    return [{"unit_type": unit_type(r["beds"], r["baths"]), "sqft": r["sqft"], "rent": r["rent"]}
            for r in collapse(capture_suites(c), {})]


def summary(blds, parking_in_net) -> list[dict]:
    """Unweighted average of building averages, per bed count, comps only."""
    per = {}
    for b in blds:
        if b["role"] == "subject" or b["off_market"]:
            continue
        for r in b["rows"]:
            k = r["unit_type"].split("/")[0] if r["unit_type"] else ""
            if not k or r["rent"] is None:
                continue
            net = r["rent"] - ((r["rent"] * (r["months_free"] or 0) + (r["cash_off"] or 0)) / (r["term"] or 12))
            per.setdefault(k, {}).setdefault(b["name"], []).append(
                (r["rent"], r["sqft"], net, bool(r["months_free"] or r["cash_off"])))
    out = []
    for k in sorted(per, key=lambda x: (x != "Bachelor", x)):
        bavg = [(statistics.mean(v[0] for v in vs), statistics.mean([v[1] for v in vs if v[1]] or [0]) or None,
                 statistics.mean(v[2] for v in vs), any(v[3] for v in vs)) for vs in per[k].values()]
        rents = [x[0] for x in bavg]
        psf = [x[0] / x[1] for x in bavg if x[1]]
        out.append({"Unit Type": k, "Buildings": len(bavg), "Avg Base Rent": round(statistics.mean(rents)),
                    "Median Base Rent": round(statistics.median(rents)),
                    "Avg Rent PSF": round(statistics.mean(psf), 2) if psf else None,
                    "Avg Net Rent": round(statistics.mean(x[2] for x in bavg)),
                    "Share With Incentive": f"{sum(x[3] for x in bavg) / len(bavg):.0%}"})
    return out


# ----------------------------------------------------------------------------- workbook
def write_workbook(blds, flags, calls, settings, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Survey"
    fill = lambda c: PatternFill("solid", fgColor=c)  # noqa: E731
    thin = Side(style="thin", color="FFBFBFBF")
    has = {c: any(_cell_value(b, r, c) not in ("", None) for b in blds for r in b["rows"]) for c in OPTIONAL}
    cols = [c for c in COLUMNS if c not in OPTIONAL or has[c]]
    ci = {c: i + 1 for i, c in enumerate(cols)}
    L = {c: get_column_letter(ci[c]) for c in cols}

    last_visible = ci["Link"]
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_visible)
    ws.cell(1, 1, settings.get("title", "Rental Market Survey")).font = Font(bold=True, size=14)
    ws.cell(1, 1).alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 54
    for c in cols:
        cell = ws.cell(2, ci[c], c)
        helper = ci[c] > last_visible
        cell.fill = fill("FF7F7F7F" if helper else NAVY)
        cell.font = Font(bold=True, color=WHITE)
        cell.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 32

    row, band = 3, 0
    order = sorted(blds, key=lambda b: b["role"] != "comp")  # comps first, subject last (Hopewell layout)
    for b in order:
        if not b["rows"]:
            continue
        subj = b["role"] == "subject"
        bg = NAVY if subj else (BAND if band % 2 == 0 else WHITE)
        band += 0 if subj else 1
        first = row
        for r in b["rows"]:
            for c in cols:
                v = _cell_value(b, r, c) if (c not in BUILDING_COLS or row == first) else None
                cell = ws.cell(row, ci[c], v)
                cell.fill = fill(PINK if (b["off_market"] and not subj) else bg)
                cell.font = Font(color=WHITE if subj else "FF000000")
                cell.border = Border(bottom=thin)
                cell.alignment = Alignment(vertical="center", wrap_text=c in ("Notes", "Incentive", "Building Name", "Parking", "Utilities"))
            R = row
            ws[f"{L['Rent PSF']}{R}"] = f'=IF(AND(ISNUMBER({L["Base Rent"]}{R}),ISNUMBER({L["SF"]}{R}),N({L["SF"]}{R})>0),{L["Base Rent"]}{R}/{L["SF"]}{R},"")'
            park = f'+N({L["Parking ($/mo)"]}{R})' if settings.get("parking_in_net") else ""
            ws[f"{L['Net Rent']}{R}"] = (f'=IF(ISNUMBER({L["Base Rent"]}{R}),{L["Base Rent"]}{R}-IF(N({L["Lease Term (mo)"]}{R})>0,'
                                        f'({L["Base Rent"]}{R}*N({L["Free Months"]}{R})+N({L["Cash Off ($)"]}{R}))/{L["Lease Term (mo)"]}{R},0){park},"")')
            ws[f"{L['Net Rent PSF']}{R}"] = f'=IF(AND(ISNUMBER({L["Net Rent"]}{R}),ISNUMBER({L["SF"]}{R}),N({L["SF"]}{R})>0),{L["Net Rent"]}{R}/{L["SF"]}{R},"")'
            if not subj:  # yellow = missing data on a comp
                for c in ("SF", "Base Rent"):
                    if _cell_value(b, r, c) in ("", None):
                        ws[f"{L[c]}{R}"].fill = fill(YELLOW)
                if r.get("call") and not r.get("status"):
                    ws[f"{L['Status']}{R}"].fill = fill(YELLOW)
            row += 1
        if row - first > 1:
            for c in BUILDING_COLS:
                if c in ci:
                    ws.merge_cells(start_row=first, start_column=ci[c], end_row=row - 1, end_column=ci[c])
        if b["link"]:
            ws[f"{L['Link']}{first}"].hyperlink = b["link"]

    fmt = {"SF": "#,##0", "Base Rent": "$#,##0", "Rent PSF": "$0.00", "Net Rent": "$#,##0",
           "Net Rent PSF": "$0.00", "Parking ($/mo)": "$#,##0", "Cash Off ($)": "$#,##0"}
    width = {"Building Name": 24, "Address": 22, "Incentive": 30, "Notes": 30, "Link": 34, "Parking": 18,
             "Utilities": 18, "Developer / Manager": 18, "Unit Type": 15, "Suite": 14}
    for c in cols:
        ws.column_dimensions[L[c]].width = width.get(c, 11)
        for r in range(3, row):
            if c in fmt:
                ws[f"{L[c]}{r}"].number_format = fmt[c]
    ws.freeze_panes = "B3"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:2"
    ws.print_area = f"A1:{L['Link']}{row - 1}"

    def table(title, rows, widths=None):
        t = wb.create_sheet(title)
        if not rows:
            t["A1"] = "Nothing to report."
            return
        keys = list(rows[0].keys())
        for j, k in enumerate(keys, 1):
            cell = t.cell(1, j, k)
            cell.fill, cell.font = fill(NAVY), Font(bold=True, color=WHITE)
            t.column_dimensions[get_column_letter(j)].width = (widths or {}).get(k, 18)
        for i, r in enumerate(rows, 2):
            for j, k in enumerate(keys, 1):
                t.cell(i, j, r.get(k)).alignment = Alignment(wrap_text=True, vertical="top")
        t.freeze_panes = "A2"

    table("Flags", flags, {"Detail": 90, "Building": 28})
    table("Call List", calls, {"Ask": 100, "Building": 28, "Address": 24})
    intel = wb.create_sheet("Intel")
    intel["A1"] = "Internal. Drafted from captured history; copy to a client only by choice."
    intel["A1"].font = Font(italic=True)
    r0 = 3
    stats = summary(blds, settings.get("parking_in_net"))
    for block_title, rows in (("Market summary (comps only, unweighted average of building averages)", stats),
                              ("Change log", [x for b in blds for x in change_log(b)])):
        intel.cell(r0, 1, block_title).font = Font(bold=True)
        r0 += 1
        if not rows:
            intel.cell(r0, 1, "None yet: needs two or more captures of the same listing.")
            r0 += 2
            continue
        keys = list(rows[0].keys())
        for j, k in enumerate(keys, 1):
            c = intel.cell(r0, j, k)
            c.fill, c.font = fill(NAVY), Font(bold=True, color=WHITE)
        for rr in rows:
            r0 += 1
            for j, k in enumerate(keys, 1):
                intel.cell(r0, j, rr[k])
        r0 += 3
    for j, w in enumerate([28, 14, 14, 10, 70, 18, 18], 1):
        intel.column_dimensions[get_column_letter(j)].width = w
    wb.save(path)


def _cell_value(b, r, c):
    return {
        "Building Name": b["name"], "Area": b["area"], "Address": b["address"], "Product": b["product"],
        "Year Built": _int_or(b["year_built"]), "Suite": r.get("suite") or "", "Unit Type": r.get("unit_type"),
        "SF": r.get("sqft"), "Base Rent": r.get("rent"), "Incentive": r.get("incentive") or "",
        "Status": r.get("status") or "", "Parking": r.get("parking") or "", "Parking ($/mo)": r.get("parking_monthly"),
        "Utilities": r.get("utilities") or "", "Developer / Manager": b["developer"],
        "Notes": "; ".join(x for x in [b["notes"] if r is b["rows"][0] else "", r.get("notes")] if x),
        "Link": b["link"], "Free Months": r.get("months_free") or None,
        "Lease Term (mo)": r.get("term"), "Cash Off ($)": r.get("cash_off"),
    }.get(c)


def clean_parking(t) -> str:
    """The page's parking block, minus the yes/no scaffolding and anything past it."""
    t = re.split(r"\|\s*(?:Explore Similar Rentals|Property Managed By|Contact Landlord)", str(t or ""))[0]
    t = re.sub(r"Parking Available\s*\|\s*(Yes|No)\s*(\|\|\s*(Yes|No))?", "", t)
    t = re.sub(r"Other Parking Details\s*\|", "", t)
    t = re.sub(r"Pets (&amp;|&) Parking", "", t)
    t = " ".join(x.strip(" |") for x in t.split("||") if x.strip(" |"))
    return t[:80] + ("..." if len(t) > 80 else "")


def _manager(me) -> str:
    n = ((me.get("parentOrganization") or {}).get("name") or "").strip()
    return "" if re.match(r"^\d", n) else n  # a bare civic address is not a manager


def _int_or(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return v or ""


# ----------------------------------------------------------------------------- CLI
def cmd_queue(a):
    rows = read_csv(a.buildings)
    q, manual = [], []
    for r in rows:
        lid, url = listing_id(r.get("rentfaster")), (r.get("rentfaster") or "").split("?")[0]
        if not lid:
            continue
        (q if url.startswith(SAFE_PREFIX) else manual).append({"listing_id": lid, "url": url, "priority": 1,
                                                               "building": r.get("building_name", "")})
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, ["listing_id", "url", "priority"], extrasaction="ignore", lineterminator="\r\n")
        w.writeheader()
        w.writerows(q)
    print(f"[queue] {len(q)} pages -> {a.out}")
    for m in manual:
        print(f"[queue] open by hand (no /properties/ URL, the queue only follows those): "
              f"{m['building']} {m['url'] or m['listing_id']}")


def cmd_build(a):
    spec = Path(a.spec)
    settings = json.loads((spec / "settings.json").read_text()) if (spec / "settings.json").exists() else {}
    exp = lambda pats: sorted({f for p in (pats or []) for f in glob.glob(p)})  # noqa: E731
    as_of = date.fromisoformat(a.as_of) if a.as_of else date.today()
    blds, flags, calls = assemble(spec, exp(a.detail), exp(a.map), exp(a.queue_log), as_of,
                                  int(settings.get("stale_days", 14)), exp(a.images), a.image_reads)
    write_workbook(blds, flags, calls, settings, a.out)
    recalculated = recalc(a.out)
    n = sum(len(b["rows"]) for b in blds)
    print(f"[build] {len(blds)} buildings, {n} suite rows, {len(flags)} flags, {len(calls)} calls -> {a.out}"
          + ("" if recalculated else "  (NOT recalculated: open and save in Excel before uploading to Survey_Map)"))


def recalc(path) -> bool:
    """Store calculated values next to the formulas. openpyxl writes formulas with no cached
    result, and Survey_Map reads Net Rent from the cell value, so an un-recalculated file shows
    no net rents there (verified 2026-11-24 with Survey_Map's own parser). LibreOffice headless
    recalculates on load and saves the results; formatting, merges and links survive."""
    import shutil
    import subprocess
    import tempfile
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return False
    out = tempfile.mkdtemp(prefix="recalc_")
    r = subprocess.run([exe, "--headless", "--calc", "--convert-to", "xlsx:Calc MS Excel 2007 XML",
                        "--outdir", out, str(path)], capture_output=True, timeout=180)
    produced = Path(out) / Path(path).name
    if r.returncode != 0 or not produced.exists():
        return False
    shutil.move(str(produced), str(path))
    return True


def cmd_images(a):
    """Write each comp's hero image to a folder so it can be read (by Neil, or by Claude in the
    session) and any image-only offer typed into buildings.csv."""
    import base64
    exp = lambda pats: sorted({f for p in (pats or []) for f in glob.glob(p)})  # noqa: E731
    imgs = load_images(exp(a.images))
    reads = load_image_reads(a.image_reads)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for r in read_csv(Path(a.spec) / "buildings.csv"):
        lid = listing_id(r.get("rentfaster"))
        if not lid:
            continue
        im = imgs.get(lid)
        name = re.sub(r"[^A-Za-z0-9]+", "_", r.get("building_name") or lid).strip("_")
        if not im:
            print(f"[images] {lid} {r.get('building_name', '')}: no hero image captured")
            continue
        path = out / f"{lid}_{name}.jpg"
        path.write_bytes(base64.b64decode(im["jpeg_b64"]))
        rd = reads.get((im.get("url") or "").split("?")[0])
        state = f"already read {rd.get('read_on')}: '{rd.get('offer')}'" if rd else "NEEDS READING (add a row to data/image_reads.csv)"
        print(f"[images] {lid} {r.get('building_name', '')}: {path} ({im.get('w')}x{im.get('h')}, captured {im.get('ts', '')[:10]}) {state}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rental survey engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue")
    q.add_argument("buildings")
    q.add_argument("--out", required=True)
    b = sub.add_parser("build")
    b.add_argument("spec")
    b.add_argument("--detail", nargs="*")
    b.add_argument("--map", nargs="*")
    b.add_argument("--queue-log", nargs="*")
    b.add_argument("--images", nargs="*", help="rf_images_*.json from userscript v2.0")
    b.add_argument("--image-reads", default=str(Path(__file__).resolve().parent.parent / "data" / "image_reads.csv"),
                   help="offers read from listing images, keyed by image URL (default data/image_reads.csv)")
    b.add_argument("--as-of")
    b.add_argument("--out", required=True)
    im = sub.add_parser("images", help="write comps' hero images to a folder for reading")
    im.add_argument("spec")
    im.add_argument("--images", nargs="*", required=True)
    im.add_argument("--out", required=True)
    im.add_argument("--image-reads", default=str(Path(__file__).resolve().parent.parent / "data" / "image_reads.csv"))
    a = ap.parse_args(argv)
    {"queue": cmd_queue, "build": cmd_build, "images": cmd_images}[a.cmd](a)


if __name__ == "__main__":
    main()
