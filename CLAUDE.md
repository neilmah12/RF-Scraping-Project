# Rentfaster Market Intelligence + Proforma Engine

Project reference file. Written 2026-07-30. Updated 2026-08-18.

**New session? Read `property-merge-pipeline/docs/HANDOFF.md` first.**

**Cross-repo:** listing-to-building matching, the durable-key design and the
Edmonton assessment work now live in the companion repo
`property-merge-pipeline` — see its `CLAUDE.md` and
`docs/PLANNING_2026-08-18.md`. Read those before starting anything in Step 3
below; that planning record supersedes Step 3's original design. This document is the persistent context
for all future sessions (Claude Code or claude.ai). Read it fully before making
changes. Update the Decision Log and Status sections as work progresses.

---

## 1. Purpose

Neil (Multifamily Associate, Edmonton AB commercial real estate) is building a
multifamily data platform on top of an existing linked inventory. This module adds:

1. **Live market rent tracking** from Rentfaster asking rents, snapshotted monthly
2. **Listing-to-building matching** against a 5,252-building inventory
3. **Signals layer**: below-market rent flags, listing activity, rent cuts, refi windows
4. **Proforma engine**: auto-populate suite-level proformas using subject + comp rents

Target accuracy is ~80%+ with iterative refinement. Perfect data is not expected
(CoStar suite mix gaps, estimated mortgage maturities, geocode noise).

## 2. Wider data architecture (context)

Existing linked system (built prior to this module):
- **FileMaker**: inventory base
- **City of Edmonton assessment data**: matched via legal description; account
  number is the forward key
- **Gettel**: sales history + mortgage registration data
- **Rentfaster**: this module (previously unintegrated)
- **CoStar**: merged into inventory (match confidence scoring already in place)

Key domain principle already established: 5+ unit Edmonton multifamily is assessed
on income approach (PGI x GIM), not physical condition. The "rent gap signal"
(Approach B: assessed value growth lagging market-area PGI/GIM trend) is the primary
acquisition prospecting flag; permits from the Open Data Portal split flagged
buildings into capex-required vs rent-lift pools. Rentfaster rents feed this.

## 3. Inventory workbook (input)

File pattern: `inventory_merge_*.xlsx`. Sheets: Inventory (4,746 rows x 78 cols; the 5,252 figure recorded earlier was stale),
Crosswalk, Sales (1,284 rows x 62 cols, includes geocoded lat/long + mortgage/refi
columns), CMB Reference, plus Review sheets.

Inventory completeness (as of 2026-07-29 file):
- Normalized address: 5,226 / 5,252
- Units: 4,958 | Year built: 5,192
- Any suite mix: 2,146 (~41%) — remainder needs submarket/vintage comp fallback
- Legal description normalized: 2,228
- No lat/long on Inventory (Sales sheet has it; pipeline geocodes Inventory fresh)

Sales sheet already contains: `Mortgage Maturity Date` (ESTIMATE: registration + 5yr),
`Months to Refi`, `Refi Window`, `Refi Risk Tier`, `Est. 5-Yr CMB Rate`.
Refi screening is therefore a JOIN, not a build.

## 4. Rentfaster data source

**No automated scraping.** Rentfaster has bot protection; the workflow is a manual
monthly console capture. Neil filters the map (Apartment + Townhouse + Fourplex;
Triplex and condo units excluded — Triplex dropped 2026-08-11, not really
multifamily; may revisit), opens DevTools Network tab (or uses the console
capture helper below), and saves map.json responses. Pipeline ingests whatever
files he saves. Keep it this way — do not add automated fetching: every
capture must still originate from Neil manually panning/zooming/drawing on the
map in a real browser session. Nothing may issue its own requests to
Rentfaster or drive the map without him.

### Listing-detail schema (DOCUMENTED 2026-08-18 — closes open question 5)

Rentfaster detail pages are **server-rendered with schema.org JSON-LD**, not a
JSON API. The payload never crosses fetch/XHR — it arrives inside the HTML:

    <script type="application/ld+json">
      {"@type":"ItemPage","mainEntity":{"@type":["LocalBusiness","ApartmentComplex"], ...}}

`mainEntity` carries:

| Field | Notes |
|---|---|
| `name` | building name, or the street address when the building is unnamed |
| `slogan` | often the building name, sometimes a promo line |
| `parentOrganization` | the property **MANAGER**, not necessarily the owner. Boardwalk and Mainstreet manage what they own; Zen Residential manages a building owned by an individual. Has `telephone`, `url` |
| `address` | `streetAddress`, `addressLocality`, `addressRegion`, **`postalCode`** |
| `geo` | latitude, longitude |
| `amenityFeature[]` | 13–18 structured amenities per listing |
| **`containsPlace[]`** | **one entry per advertised suite type** |
| | `numberOfBedrooms`, `numberOfFullBathrooms`, `numberOfPartialBathrooms` |
| | `potentialAction.price` — **rent for that suite type** |
| | `floorSize.value` — **SQUARE FEET** |
| | `additionalProperty` — Availability Date, Utilities Included |
| `priceRange`, `telephone`, `image`, `petsAllowed`, `description` | |

What this closes:

- **`blended` and `inferred` rent confidence.** map.json gives a range with no
  way to map price to bed count. `containsPlace` states it outright.
- **Square footage**, the largest gap in the proforma inputs (FileMaker suite
  sizes 8–19%, CoStar `Average Unit SF` 28% and dropped). Present on 4 of 5
  suites in the first sample, tied to a bed count and a rent.
- **Utilities Included**, which decides whether a quoted rent is net or gross.
- **Property manager**, corroborating Inventory's Owner Company on 3 of 4.

Caveats, from the first 4 captures:

- `containsPlace` is what is **currently advertised**, not the building's suite
  mix. One listing showed a single 1-bed while its own description mentioned
  bachelor, 1 and 2 bedroom options. It is a rent observation, not a rent roll.
- Rents move between captures. Two of four disagreed with the 2026-08-11 map
  capture taken 7 days earlier ($1,339 → $1,309; $1,099–1,250 → $999). Detail
  captures need their own capture date rather than being folded into the map
  snapshot.
**Field population, measured 2026-08-19 on 21 listings / 103 suite types:**

| Level | Field | Populated |
|---|---|---|
| listing | name, slogan, address, postalCode, geo, priceRange, telephone, petsAllowed, description, image, amenityFeature, containsPlace | **100%** |
| listing | parentOrganization | 90% |
| suite | bedrooms, bathrooms, rent | **100%** |
| suite | **floorSize (square feet)** | **98%** |
| suite | Utilities Included | 75% |
| suite | Availability Date | 100% (8/8 on the survey capture) |

Amenities run 3–30 per listing (median 18). Suites per listing run 1–17.

**Availability is a directional vacancy read, and the two values say
opposite things.** `Availability Date` arrives per suite type as display text:
"Immediate", "Sep 01, 2026", or in map.json the yearless "Sep 01".
`parse_rf_detail.py` keeps the raw string and adds `available_date` (ISO) and
`available_immediate`, then rolls them up per listing into `vacancy_signal`:

| Value | Means | Does NOT mean |
|---|---|---|
| `current` | at least one advertised suite type is available now | any particular amount of vacancy |
| `none_current` | every advertised suite type is future-dated | the building is full |
| `unknown` | nothing parseable | anything |

The asymmetry is the point. "Immediate" proves vacancy exists but the count is
suite TYPES, not units -- one row can be a single empty suite or twelve, so
read `suite_types_immediate` as a floor of one. A listing whose suites are all
future-dated is the more informative case: nothing is available today, which
is a real no-vacancy observation for the suite types being advertised.

Three limits that must travel with it. `containsPlace` is what is currently
advertised, so `none_current` is silent about suite types the building chose
not to list. A building with no listing at all is absent from the file
entirely and must never be read as full. And in map.json availability sits at
LISTING grain, not suite grain, so the all-future-dated inference needs a
detail capture -- the monthly map pull cannot produce it.

The rule lives in `src/availability.py`, duplicated byte-for-byte as
`tools/availability.py` in the companion repo. Neither repo can import from
the other, and a disagreement would mean the same building reading differently
depending on which file you opened. Both carry the same case table and a
self-check (`python3 src/availability.py`). Change one, change both.

Yearless map.json dates resolve against the capture date, but a month-day up
to 60 days past stays in the current year rather than rolling forward. A
listing captured Aug 13 advertising "Aug 01" is a suite empty for two weeks
that has not rented -- the strongest softness signal there is -- and the naive
rule would push it to 2027 and read it as no vacancy.

Unrecognised availability text is reported by the parser rather than left
blank, so a new phrasing surfaces instead of reading as missing data.

**Half the amenity vocabulary is noise.** Pooled over 30 listings, 12 entries
are *area* features rather than building features and are near-universal —
Bus, Playground/Park and Shopping Center are all 30/30, Bike Paths 28/30. They
carry no discriminating signal. Fridge and Oven/Stove at 26/30 are the same
story. **25 of 50 distinct amenities actually vary** and are worth keeping:
Dishwasher 20/30, Elevator 17, Balcony 16, Fitness Area 11, Air Conditioning 9,
Laundry In-Suite 9 versus Shared 10, and the flooring set — Carpeted 7,
Laminate 6, Hardwood 4, Luxury Vinyl Plank 3, Tile 3.

**Facts versus claims.** The prose cannot be trusted as evidence. "Fully
renovated boutique suites" states neither when nor to what degree, and the
worst stock still advertises itself as highly amenitized. Only checkable facts
carry weight: rent, square feet, beds, baths, utilities included, and the
binary amenity entries — flooring type especially, since it is the closest
thing to an observable proxy for suite condition. Treat `description` and
`slogan` as leads to verify, never as attributes.

**Incentives can be baked into the listing photo.** Confirmed 2026-08-25:
Beau Mills advertised "0.5 months free rent" in the listing *image*, with
nothing in the promo box (`promotions: null`) and nothing in the description.
No text or DOM extraction can reach that. The image URLs are captured, so OCR
remains possible later, but for now an image-only incentive is invisible to
the pipeline and only a human sees it. Record those in the survey catalogue
with `incentive_source = listing image`.

Two consequences:

- **Square footage is solved for any building with a listing.** 98% here
  against FileMaker's 8–19% and CoStar's dropped 28%, and it is per suite type
  tied to a bed count and a rent, so **rent per square foot is computable for
  the first time**. On that sample: bachelor $2.74, 1-bed $2.64, 2-bed $2.19,
  3-bed $1.96 — the expected gradient, which is itself a sanity check.
- **The rent-confidence tiers collapse.** Of the 21, map.json had rated 12
  `blended` and 5 `inferred`. Every one now has explicit per-suite rent, so
  all become equivalent to `direct`.

All 21 address-matched an Inventory building. Manager matched Owner Company on
11 of 15 comparable.

**Incentives in free text — the structured flag is incomplete.** `promotions`
in map.json is a tag list: it says *that* an incentive exists, never its terms,
and it misses incentives typed into the ad copy rather than the promotions box.
On the 21-listing sample, **2 advertise an incentive in the text that map.json
does not flag** ("Get up to 2 months free", "1 MONTH FREE RENT PLUS FREE
INTERNET & CABLE"), while 7 carry the flag with no matching text. The two
signals are **complementary, not redundant** — use both, trust neither alone.

Terms matter for underwriting: two months free is roughly a 16% effective
discount, $500 off is nearer 3%. `parse_rf_detail.py` writes the **full
description verbatim** plus heuristic `incentive_detected` / `incentive_kinds`
/ `incentive_snippet` columns, so the rules can be improved and re-run later
without recapturing anything.

**Parking lives in the text too, and rates exist nowhere else.** The structured
payload is nearly silent on parking — 1 of 21 listings carried a parking
amenity tag ("Guest Parking") while **14 of 21 described parking in the ad
copy**. Rates appear only there: 3 of 21 quoted one, spanning **$10/mth for an
outdoor stall to $195/mth underground**.

Inventory already holds `Parking Type` (43%) and `Parking Spaces` (49%), so
type extracted from the text is a **cross-check**, not a replacement —
Inventory's is more reliable where present. But Inventory has **no rate field
at any fill rate**, which makes the listing text the only source for it. On a
100-stall building that $10-to-$195 spread is roughly $220k of annual revenue,
so it is materially load-bearing for a proforma rather than a nice-to-have.

`parse_rf_detail.py` writes `parking_types`, `parking_rate_monthly` and
`parking_rate_text`. Two extraction traps found and fixed on this sample, both
worth knowing if the rules are ever revised: matching on the bare word "park"
reads "nearby parks and trails" as surface parking, and taking the smallest
dollar amount in a segment reported a $35 pet fee as the parking rate for a
building charging $195. Amounts are now chosen by proximity to a parking word,
and the raw snippet is always kept so a human can check.

### Listing-detail capture (two versions, added 2026-08-18)

**`tools/rf_detail_capture.user.js`** — Tampermonkey/Violentmonkey userscript,
the one to use for volume. Runs itself on every listing page opened, so
clicking links out of the match review workbook just works. Dedupes on
listing id, shows a badge with the running count, click to download. Needed
because opening a listing is a navigation and page memory dies each time, so
the console version would have to be re-pasted on all ~800 review rows.

**`tools/rf_detail_capture.js`** — console version, discovery mode. Still
useful for a one-off, and for re-running discovery if the page structure
changes. It is what documented the schema above: it scans inline JSON as well
as fetch/XHR, which is why it found the payload at all.

Neither fetches anything. Every page is one Neil opened himself; both only read
a document the browser already parsed.

#### Console version, discovery notes

Companion to the map hook, for browsing individual listing pages. Same rule:
it never fetches anything itself, it only keeps what the browser already
loaded while Neil clicks through listings manually.

Runs in **discovery mode** because the detail schema is still undocumented
(open question 5). Rather than matching a known shape it keeps anything
plausibly listing-related and reports the structure back via `rfdInspect(n)`,
which prints keys and types rather than values. Once a real capture exists the
schema goes in Section 4 and the filter can be narrowed.

Two things it does that the map hook does not, both necessary here:

- **Scans inline JSON in the loaded document**, not just fetch/XHR. If the
  detail page is server-rendered the payload never crosses the network hooks —
  it arrives inside the HTML. Reads the DOM the browser already parsed; issues
  no request.
- **Persists captures to localStorage**, because clicking between listings is a
  navigation and a full page load wipes in-memory state. The map hook lives on
  one page and never had this problem.

`rfdStatus()` / `rfdInspect(n)` / `rfdDownload()` / `rfdClear()`. Verified in a
Node harness: inline and network payloads both captured, re-pasting swaps hooks
without double-wrapping (native fetch called once per request), captures
survive across page loads, listing id parsed from the URL slug.

The best time to run it is during match review — the listings being verified
are the ones worth having detail for, so the data comes as a byproduct of work
already being done.

### Console capture helper (tools/rf_console_capture.js, added 2026-08-11)

Removes the manual "open Network tab, find the map.json request, Save
Response As" step per capture — it does not remove or automate the panning
itself. Paste the script into the DevTools console once per session; it hooks
`fetch`/`XMLHttpRequest` on the page and recognizes a response as a capture by
its shape (`listings` array + `search` object), not by URL, so it works
regardless of which transport the map uses. Every matching response the
browser receives while Neil pans/zooms/draws gets stored in memory with a
timestamp, unless it contains zero listing ids not already captured this
session (a slow pan/zoom fires a new map.json on nearly every small viewport
shift, mostly re-fetching listings already seen — those are silently dropped
and tallied in a skip counter rather than bloating the download and QC output
with pure duplicates; a response with even one new id is still kept in full).
`rfStatus()` shows what's accumulated so far plus the skip count;
`rfDownload()` downloads everything kept from the session as one combined
file (`rf_captures_<timestamp>.json`, shape `{"captures": [payload, ...]}`);
`rfClear()` wipes memory (captures, seen-id set, skip counter) without
needing a page reload. Captures live only in page memory — reloading the tab
loses anything not yet downloaded.

`check_capture.py` understands both shapes: a single raw map.json payload
(the original per-draw file) or a combined `{"captures": [...]}` file, QC'ing
each sub-capture individually and reporting cross-capture overlap same as it
already does across separate files. No dedup happens in the browser — the
existing id-based dedup in `ingest_snapshot` (and the overlap reporting in
`check_capture.py`) already handles it, same as overlapping hand-drawn
sections always have.

### map.json schema (documented 2026-07-30)

Per listing: `id`/`ref_id` (stable listing ID), `userId`, `latitude`, `longitude`,
`city`, `city_id` (2=Edmonton, 43=St. Albert, 33=Sherwood Park, 34=Spruce Grove,
39=Leduc, 31=Fort Sask, 36=Beaumont, 35=Stony Plain), `community`, `type`, `intro` (display
address, sometimes empty), `link` (slug contains civic address), `date`
(posted/renewed — NOT a days-on-market origin), `availability`/`a`, `units`
(count of distinct suite types advertised), `beds`/`beds2` (range, values incl
"studio", "1+den" etc.), `price`/`price2` (range, strings, junk values like "1"
occur), `promotions`, `personaVerified`, `f` + `mapRole`.

Also present per listing: `baths`/`baths2` (range, decimals for half-baths e.g.
"1.5", "2.5" — not currently in the Inventory workbook but kept in the export
since it's free and useful for QA/filtering even if dropped downstream), and
`promotions` (list of incentive codes; `active_and_upcoming_promotions` is a
duplicate field, confirmed identical to `promotions` in every payload sampled
so far — use `promotions` as primary, do not `or`-chain the two since a real
empty list on one must not fall back to the other).

Known promotion codes (sampled 2026-07-30, one payload, 500 listings, 213 had
a promo): `rent_special` (128), `other_promotion` (93), `move_in_gift` (5).
Any future/unseen code still gets counted (has_promo, n_promotions,
promotions_raw) even without its own named column.

Known quirks:
- **`f` is a display/promotion tier, NOT furnished** (tentative interpretation;
  same listing appears under multiple f values → dedupe on `id` mandatory).
  Confirmed 2026-07-30: `mapRole` (silver/highlighted) only ever appears when
  `f==2`; `f==0`/`f==1` always have `mapRole=None`. Consistent with `f` being
  a paid display tier, not a furnished flag.
- **Real per-response cap is 500 PROPERTY LISTINGS, not the 800 implied by
  `search.max`, and not denominated in the same unit as `total` (see below).**
  Confirmed 2026-08-10 across two independent captures: both returned exactly
  500 listings regardless of `total`/drawn area size, even though `search.max`
  reports `"800"` in the payload. Judge whether a drawn/zoomed area is small
  enough by checking the number of property rows returned against 500 (aim
  comfortably under, e.g. ~450, since 500-vs-500 at the boundary is ambiguous
  — can't tell if you got everything or got truncated right at the edge). Do
  NOT judge it by `total`, which is a suite-type count and runs roughly 2x the
  property count. Multiple zoomed-in quadrant/draw-tool captures per month are
  required to stay under the real cap.
- **`total` counts SUITE TYPES, not property listings. This is the single most
  important thing to know about the payload** (established 2026-08-14; it
  overturns roughly three weeks of wrong conclusions recorded below and above).
  `sum(units)` across the returned listings equals `total` exactly in 169 of
  173 captures in the 2026-08-11 pull -- and the 4 exceptions are precisely the
  captures that hit the 500-property cap, where the array (and so the sum) was
  cut short. Independently confirmed on a location-search capture: 457 property
  rows, `sum(units)` = 628, `total` = 628. The site's own "Results (N)" badge
  uses the same suite-type grain, which is why it reads far higher than the
  number of map pins.
  Practical rules:
    - one row in `listings` = one PROPERTY ad (one map pin), carrying a `units`
      count of how many distinct suite types it advertises
    - `total` = suite types in the current view
    - the 500 cap applies to PROPERTY rows returned, not to suite types
    - to test completeness: `sum(units) == total`. Never compare `len(listings)`
      against `total` -- that is a units mismatch (properties vs suite types)
      and it is what produced every bogus "SHORT" warning in this project.
    - to compare captured data against the site's Results counter, sum `units`
      (i.e. `n_suite_types` in the export), do NOT count rows.
- **`total2` is unreliable and should not be used for completeness.** It equals
  the property-row count on draw-tool captures (NW section: `total` 153 suite
  types, `total2` 80 properties, array 80) but simply repeats the suite-type
  `total` on plain zoom/pan captures (`total` 921, `total2` 921, 426 property
  rows covering exactly 921 suite types). Prior entries here theorized that
  `total` counted the drawn shape's bounding box while `total2` counted the
  polygon, citing a West Central capture whose returned listings stopped 3.8 km
  short of the bbox's west edge. That empty strip was real, but it was not the
  cause of the number gap: `sum(units)` for that same capture is 746, exactly
  matching its `total` of 746. The bbox-vs-polygon hypothesis is withdrawn.
- **The "sub-cap truncation pattern" recorded 2026-08-13 never existed.** Those
  captures (426 of a reported 921, 406 of 904, 374 of 836, all on
  `"e": "zoom_changed"` views) were complete: 426 property rows covering exactly
  921 suite types, 406 covering 904, 374 covering 836. They were flagged only
  because the QC script compared property rows against a suite-type total. The
  speculated mechanism (the map front end thinning results at certain zoom
  levels) is withdrawn -- there is no evidence for it. The only genuine
  truncation mode remains the 500-property hard cap, which affected 4 captures
  in the 2026-08-11 pull, all early wide zoomed-out views.
- **No top-level `cities` field on draw-tool captures.** Present on the earlier
  whole-city payloads (that's where `city_totals.csv` comes from), absent from
  the 2026-08-11 NW draw. Section-by-section captures therefore lose the free
  metro-wide coverage cross-check — take one unfiltered whole-city capture per
  month alongside the sections if that cross-check is wanted.
- Fields present in map.json but not yet used: `title` (listing headline, often
  carries the incentive in plain text e.g. "1 Month Free Rent" — a text-side
  cross-check on the `promotions` codes), `marker` (opaque hash), `thumb2`,
  `has_book_tour`, `request_tour_enabled`, `v`.
- **Map filter (property type checkboxes) can silently drift between draws
  within the same session** — observed 2026-08-10 across several captures in
  one sitting: some included Condo Unit (against the documented
  Apartment+Townhouse+Triplex+Fourplex/no-condo convention), one dropped
  Townhouse and Triplex entirely. This is a structural gap, not a coverage
  problem — a dropped property type is invisible for that whole quadrant, and
  the coverage percentage can't catch it since it's computed against whatever
  total the active (wrong) filter returns. Re-check the filter checkboxes
  before every single draw, not just at the start of a session.
- Some listings have empty `intro` and generic slug (`rentals-edmonton-NNNNN`):
  no address available, lat/long-only matching with lower confidence.

### Suite-type rent inference rule (refined 2026-08-04, supersedes 2026-07-30 version)

map.json gives ranges, not per-suite-type rents. Original rule was units-based;
refined to be driven by whether `beds_lo == beds_hi`, because a high `units`
count with a single bed count (e.g. three differently-sized 2BR floorplans)
is NOT actually ambiguous — the price range genuinely belongs to that one bed
count. Blending is only correct when the bed count itself spans a real range.
Four confidence tiers, most to least certain:
- `direct` — `units == 1`, single suite type, no ambiguity at all
- `certain` — `beds_lo == beds_hi` regardless of `units`; price range = that
  bed count's range (this is the tier that changed — previously these fell
  into `range_only` whenever `units >= 3`)
- `inferred` — `beds_lo != beds_hi` and `units <= 2`; clean two-type split
  (price→beds, price2→beds2)
- `blended` — `beds_lo != beds_hi` and `units >= 3`; genuinely can't resolve
  which price maps to which bed count (renamed from `range_only`)

Validated against a real payload (2026-08-04, 500 listings): reclassified 12
listings from the old `range_only` bucket into `certain` with no contradictions
found (zero cases of `units==1` reporting a differing `beds2`). Final tier
counts on that sample: direct 162, blended 155, inferred 147, certain 36.

Caveat (found validating against the same payload): `beds_hi` is null in some
`units >= 2` listings that DO have a `price2` — a real price spread with no
reported second bed count (33/338 listings with `units >= 2`). Sampled cases
were all consistent with "same bed count, different floorplan sizes" (supports
treating as `certain`, not `blended`), but map.json alone can't fully rule out
a genuinely differing bed count the site just didn't report — unconfirmed
until cross-checked against detail-page JSON.

Note (confirmed 2026-07-30): for `units >= 3`, `beds`/`beds2` still gives the
true suite-mix range (e.g. building has both 1-beds and 2-beds on offer), so
suite mix is known even when the per-type rent isn't. What's unrecoverable
from map.json alone is which specific price in the range applies to which
specific bed count when there are 3+ distinct suite types — that needs the
detail-page JSON (not yet captured/documented).

For buildings being actively underwritten, pull the listing detail-page JSON
(full floorplan breakdown) on demand — hybrid approach. Detail schema not yet
documented; capture an example when first needed.

## 5. Build order and status

| Step | Description | Status |
|---|---|---|
| 1 | Ingest: map.json → snapshots + listings_master | **DONE** — `rentfaster_ingest.py`, tested |
| 2 | Geocode Inventory addresses (one-time, ~5,226) | NEXT — decide geocoder (reuse Colab sales geocoder vs fresh City of Edmonton Open Data geocoder) |
| 3 | Matching engine: listing ↔ building, ~50m threshold, slug-address cross-check, manual review queue, permanent match table | pending |
| 4 | Rent table: per-building per-suite-type asking rents w/ confidence flags + submarket/vintage aggregates for unlisted buildings | pending |
| 5 | Signals: below-market flag (subject vs comp median), listing activity (duration, count, rent cuts), refi join from Sales | pending |
| 6 | Proforma engine (format TBD with Neil; standard PGI→NOI→value expected) | pending |

### Step 1 details (rentfaster_ingest.py)

- `ingest_snapshot(files, snapshot_date, data_dir)`: merges multiple payload files,
  dedupes on listing_id, appends to `snapshots` (idempotent per snapshot_date —
  re-runs replace), rebuilds `listings_master` with first_seen/last_seen/active
- `rent_changes(data_dir)`: rent cuts on same listing_id between snapshots
- Storage: parquet with CSV fallback (pyarrow optional); CSV mirror of master
  always written for Excel
- Parsing handled: "studio"→0 beds, "+den" flag, junk prices (<$100 → null),
  address from slug (city + id stripped), promo flags
- Coverage report vs site `total`; warns below 90%

### Capture QC (check_capture.py, added 2026-08-11)

Run on every section file as it comes off the browser, before ingesting:
`python src/check_capture.py data/raw/<snapshot_date>/*.json`

Checks per file: listings vs the real 500 cap (flags at/near cap), array length
vs `min(total, total2)`, active `search.type` filter vs the
Apartment+Townhouse+Triplex+Fourplex/no-condo convention, duplicate ids, and
city/type/bbox composition. Across a section set it also reports cross-section
overlap so double-drawn areas are visible before ingest dedupes them silently.

This exists because `ingest_snapshot`'s coverage number does not work for
section-by-section captures: it takes `max()` of the per-file `total` values and
compares that single number to the union of all files. With sections, each file
has its own total for its own drawn area, so that ratio is meaningless (it reads
as ~100%+ coverage no matter what was missed). Per-file truncation checking plus
a whole-city cross-check is the workable substitute. Fix the aggregate before
relying on it.

### Matching design (step 3, agreed)

- Match is a ONE-TIME event per listing_id, stored permanently
  (rentfaster_id ↔ building_id table). Not re-matched monthly.
- Primary: nearest inventory building within ~50m of listing lat/long
- Secondary: slug-parsed address string vs Inventory `Address (Normalized)`
- Ambiguity (two buildings in threshold, or slug disagrees with nearest pin)
  → manual review queue CSV
- Inventory geocoded fresh (step 2); do NOT inherit Sales sheet coordinates
  (loose Colab geocode, adjacent-property overlap acknowledged)

### Honest metric framing (agreed)

- Vacancy/absorption NOT observable (listing ≠ unit count). Frame as
  "listing activity": duration (first_seen→disappearance), listing count per
  building over time, rent changes. Rent cuts on lingering listings are the
  best softness signal.
- Mortgage maturity = registration + 5yr ESTIMATE, flagged as such.
- All inferred rents carry confidence flags through to the proforma.

### Promotion/incentive tracking (added 2026-07-30)

Neil's domain input: landlords commonly pull an incentive right before
changing rent (either direction) — incentive presence/removal is a leading
signal, not just coincident with a rent change, so it's tracked as its own
function (`promo_changes`) rather than folded into `rent_changes`.

- Every listing row carries `has_promo`, `n_promotions`, `promotions_raw`
  (comma-joined raw codes), plus named booleans for known codes
  (`promo_rent_special`, `promo_other`, `promo_move_in_gift`).
- `promo_changes(data_dir)`: same listing_id, `has_promo` flips between
  consecutive snapshots → flagged `added` or `dropped`.
- Bath range (`baths_lo`/`baths_hi`) is captured in the export even though the
  Inventory workbook has no baths field to join against — kept for QA/filtering
  now, can be dropped or matched later. General principle: capture as much
  per-listing field as map.json gives for free; downstream steps pick and
  choose what to keep.

### Poster identity / portfolio signal (added 2026-08-04)

`userId` is highly concentrated in every payload sampled so far (e.g. 99 distinct
posters across 500 listings, top poster with 70 listings) — these are property
management companies, not individual landlords, and that's leverage for both
matching (Step 3) and landlord-behavior signals (Step 5+).

- `user_listings_in_snapshot`: count of listings the same `user_id` has live in
  the current snapshot, computed per-snapshot so it's directly filterable/sortable
  in Excel without building a pivot table.
- Not yet built: cross-snapshot portfolio tracking, or using shared `user_id` to
  raise match confidence on ambiguous listings near a building the same poster
  is already confidently matched to. Deferred to Step 3.
- `city_totals.csv` also added: the payload's top-level `cities` field gives
  metro-wide listing counts per city, independent of this run's quadrant capture
  coverage — a second, cheap coverage cross-check plus a free city-level supply
  trend once multiple months accumulate.

## 6. Data store layout

```
rf_data/
  snapshots.parquet(.csv)       append-only, one row per listing per snapshot
  listings_master.parquet(.csv) current state + first_seen/last_seen/active
  ingest_log.csv                per-run coverage QC
  city_totals.csv               metro-wide listing counts per city per snapshot
                                 (from payload's top-level 'cities' field, independent
                                 of quadrant capture coverage)
  # future:
  inventory_geocoded.parquet    step 2 output
  match_table.parquet           step 3: rentfaster_id <-> building_id, permanent
  match_review_queue.csv        step 3: ambiguous cases for manual resolution
  rent_table.parquet            step 4
```

## 7. Conventions for future sessions

- Neil's preferences: concise/direct, no em dashes, Python/Colab + Excel workflow.
  Excel-consumable outputs (CSV mirrors) for anything he reviews manually.
- Document every inference rule and estimate flag in this file's Decision Log.
- Preserve alternative approaches rather than deleting them (established pattern:
  Approach A/B in assessment work).
- Manual console capture only for Rentfaster. Never automate fetching.
- Ask before restructuring the data store; downstream steps depend on it.

## 8. Decision log

| Date | Decision |
|---|---|
| 2026-07-30 | Refresh cadence: monthly manual map.json capture |
| 2026-07-30 | Suite-rent inference rule (direct/inferred/range_only) per units field |
| 2026-07-30 | `f` field = display tier (tentative), not furnished |
| 2026-07-30 | Geocode Inventory fresh; don't inherit Sales coords |
| 2026-07-30 | Match once per listing_id, store permanently, review queue for ambiguity |
| 2026-07-30 | Frame vacancy proxies as "listing activity", not vacancy/absorption |
| 2026-07-30 | Refi screen = join from Sales sheet (already built there) |
| 2026-07-30 | Hybrid rent depth: map.json market-wide, detail JSON on demand for underwriting |
| 2026-07-30 | Repo bootstrapped: `src/rentfaster_ingest.py` + `notebooks/rentfaster_ingest.ipynb` for Colab |
| 2026-07-30 | Added baths_lo/baths_hi, promotion fields + codes, `promo_changes()` signal (incentive add/drop is a leading indicator, tracked separately from rent_changes) |
| 2026-07-30 | Confirmed via sample payload: `promotions`/`active_and_upcoming_promotions` always identical; `f==2` is the only tier with a `mapRole`, supporting `f` = display tier |
| 2026-07-30 | Confirmed: units>=3 still gives true suite-mix range via beds/beds2, just not per-type rent mapping (unresolvable without detail-page JSON) |
| 2026-08-04 | Added user_listings_in_snapshot (portfolio-size signal from userId concentration) and city_totals.csv (metro-wide per-city counts from payload's 'cities' field) |
| 2026-08-04 | Decided: comparison/trend visualization stays a separate future tool, not baked into rentfaster_ingest.py -- ingest stays extraction/export only |
| 2026-08-04 | Refined suite-type inference to `direct`/`certain`/`inferred`/`blended`, driven by beds_lo==beds_hi rather than raw units count (see Section 4); `range_only` renamed `blended`. Validated against real payload, 12 listings reclassified from range_only to certain, no contradictions found |
| 2026-08-04 | Reviewed sample Inventory workbook (5,061 rows): confirmed Address (Normalized) is 99.5% complete vs 44% for raw Address (match on normalized field); confirmed City is 83.4% complete but Subdivision fills most gaps for Edmonton rows; found existing CoStar/FileMaker match uses a reusable multi-signal scoring pattern (units_exact/year_built_exact combinations) worth reusing for Step 3 |
| 2026-08-04 | Found directional-suffix mismatch risk for Step 3: ~84% of Inventory normalized addresses carry NW/NE/SW/SE, but only ~65% of Rentfaster listing intros do (same capture) -- exact-string address matching will produce false negatives; matching should strip/normalize direction before comparing, treating it as a confidence booster not a requirement |
| 2026-08-04 | Flagged Owner Company (Inventory) <-> userId (Rentfaster) cross-reference as a high-value future matching signal -- both sources show heavy portfolio concentration (e.g. Boardwalk Equities 68 buildings in Inventory; top Rentfaster userId had 70 concurrent listings). Owner Company names need normalization first (e.g. "Mainstreet Equity Corp" vs "Corp." vs "Inc" — same entity, 3 spellings, 109 buildings) |
| 2026-08-04 | Rent-table structure planned for Step 4 (not yet built): long/tidy fact table, one row per (Building ID, snapshot_date, suite_type), suite_type either a real bed count or "blended"; incentive fields (has_promo/promo_codes/n_listings_with_promo) live in the same table at the same grain, not a separate one, so incentive-before-rent-change timing stays queryable without a join. Two derived views planned on top: current 12-month wide sheet, and an annual average sheet carrying n_months_observed/n_unique_listings/dominant_rent_confidence so aggregates never lose their support/confidence. Averaging must dedupe by unique listing_id first, not by snapshot row, or a listing that sits unrented for months gets overweighted |
| 2026-08-10 | Corrected the documented listing cap: real per-response cap is 500, not the 800 implied by `search.max`. Confirmed across two independent captures (one via draw-tool custom area, total=2,251, returned exactly 500). Judge draw-tool/quadrant sizing against `total` vs 500, not 800 |
| 2026-08-10 | Found `total`/`total2` can diverge (previously always identical); `total2` matches the real `listings` array length when they differ, `total` doesn't. Also observed map filter (property type checkboxes) drifting between draws within one session -- re-check filter before every draw, not just once per session. `ingest_snapshot`'s coverage calc still only reads `total`, not yet updated to prefer `total2` |
| 2026-08-11 | Restarted the first monthly pull as section-by-section captures, snapshot_date 2026-08-11, staged in `data/raw/2026-08-11/`. Section 1 (NW Edmonton, 80 listings) is well under cap but was drawn with Apartment+Fourplex+Townhouse — Triplex missing again, so it needs a re-draw. Sections are drawn far smaller than they need to be: at 80/500 the NW draw could cover several times the area, so the city needs fewer, larger sections rather than many small ones |
| 2026-08-11 | Section 2 (West Central, 369 listings, headroom 131) captured. Same Triplex filter gap as section 1 — the checkbox is persistently unset across sessions, so both sections need re-drawing once it is fixed. 17 listings overlap section 1 (dedupes on ingest, harmless) |
| 2026-08-11 | Section 3 (West Central South, 312 listings, headroom 188) captured. Overlaps section 2 by 144 listings, all inside a ~1 km latitude band (53.5370..53.5456) covering the Strathcona/Garneau/downtown-south corridor — a reminder that section boundaries should be placed away from dense corridors, not through them. Overlap is safe (guarantees no gap, dedupes on ingest) but spends cap headroom twice. Running union after 3 sections: 600 unique listings. Triplex filter gap on all three |
| 2026-08-11 | Dropped Triplex from the filter convention (Neil: not really multifamily; may loop back). Convention is now Apartment + Townhouse + Fourplex, no condo. `check_capture.py`'s EXPECTED_TYPES updated to match; sections 1-3 (NW, West Central, West Central South), all flagged for a Triplex gap under the old convention, are retroactively clean and need no re-draw |
| 2026-08-11 | Added `src/check_capture.py`: per-section QC (cap headroom, `min(total,total2)` vs array length, filter-drift vs the documented type convention, dupes, cross-section overlap). Written because `ingest_snapshot`'s coverage metric is invalid for section captures — it compares `max()` of per-file `total` values against the union count, which cannot detect a missed section |
| 2026-08-11 | Noted: draw-tool payloads carry no top-level `cities` field, so section-only months lose the `city_totals.csv` metro cross-check. Take one unfiltered whole-city capture per month to preserve it |
| 2026-08-11 | Added `tools/rf_console_capture.js`: a browser console snippet that hooks fetch/XHR to auto-capture every map.json-shaped response while Neil pans the map manually, replacing the manual per-request "Save Response As" step. Does not automate the panning/fetching itself -- every request still originates from a manual map interaction in a real browser session, consistent with the no-automated-fetching rule. Exports one combined `{"captures": [...]}` file per session via `rfDownload()`. `check_capture.py` extended to QC either shape (single payload or combined file), reporting cross-capture overlap the same way it already does across separate files |
| 2026-08-11 | Extended `rf_console_capture.js` to skip storing a response if it contains zero listing ids not already captured this session -- a slow pan/zoom fires a new map.json on nearly every small viewport shift, mostly redundant with what was just captured, so this avoids bloating the downloaded file and `check_capture.py`'s per-capture printout with near-duplicates. Tracked via a running seen-id set and skip counter, both surfaced in `rfStatus()` and reset by `rfClear()`. Verified in a Node harness with mocked fetch/XHR: a fully-redundant response is dropped and counted as skipped, a partially-overlapping one is kept and its new-id count is correct |
| 2026-08-13 | Fixed a real bug in `rf_console_capture.js` found live during Neil's first capture session: the `fetch` hook called `res.clone()` on every fetch the page made (not just map.json), and a synchronous throw from `clone()` on some other response type propagated up through the `.then()` callback, rejecting the promise handed back to the page's own code -- surfaced as a flood of "Uncaught (in promise) undefined" console errors on every pan, unrelated to Rentfaster's own bot protection. Fixed by wrapping the clone/parse call in try/catch so nothing here can ever affect the pass-through response the page depends on. Verified with a Node harness simulating a `clone()` throw: the wrapped fetch now resolves normally instead of rejecting. Capture data itself was never affected -- listing counts and total/total2 stayed sane throughout, this only affected other unrelated requests on the page |
| 2026-08-13 | Fixed a second issue found in the same session: the script's original "already installed" guard meant re-pasting an updated version (e.g. the bugfix above) into the same tab silently did nothing, leaving the old hooks running -- the only way to actually pick up a fix was a full page reload (losing pan position and captured-but-undownloaded data). Reworked to save the true native fetch/XHR once, then have every paste restore-then-rewrap from those originals while preserving existing captures/seen-ids/skip count. Re-pasting this script is now always safe: it swaps in fresh hooks and keeps everything already captured, no reload needed. Verified with a Node harness: re-invoking the script preserves prior capture state, does not double-record a subsequent response, and calls the native fetch exactly once per request (no wrapper stacking) |
| 2026-08-13 | **[PARTLY SUPERSEDED 2026-08-14 -- only 4 of the 7 flagged captures were genuine truncations, and the residual gap is 0 listings, not 67.]** First real `rf_console_capture.js` session ingested: `data/raw/2026-08-11/console_session_1.json`, 60 sub-captures via one `rfDownload()`, 1,290 unique listings. Cities: Edmonton 6,905 rows, plus St. Albert, Sherwood Park, Spruce Grove, Leduc, Fort Saskatchewan, Beaumont, and Stony Plain (city_id 35, not previously seen -- added to `check_capture.py`'s CITY_NAMES and the map.json schema notes). 7 of 60 captures flagged: 4 hit the plain 500 hard cap (early wide zoomed-out views, e.g. 500 of a 1,756 total), 3 are the new sub-cap truncation pattern documented above. Checked whether the 53 clean captures alone cover what the 7 flagged ones saw: 67 of the 1,290 unique listings (~5%) appear ONLY in a flagged/truncated capture, unconfirmed by any clean one -- a real but small residual coverage gap in the areas those 7 captures covered, not a blocking problem given the project's ~80% accuracy target, but worth a supplemental zoomed-in pass over those 7 areas if Neil wants to close it this cycle |
| 2026-08-13 | **[SUPERSEDED 2026-08-14 -- the 47.8% figure is a units mismatch; real coverage is 97.2%. The Results-counter-is-ground-truth lesson still holds, but it counts suite types.]** Corrected the true-total estimate used for coverage math. Initial estimate (1,750-1,840 for Edmonton) came from the `total`/`total2`/`cities` fields inside individual captured payloads, which are all scoped to whatever partial viewport was visible at that pan/zoom moment -- not a real unbounded citywide ceiling, and we'd already found those fields behave inconsistently in a few other ways this session. Neil checked Rentfaster's own "Results (N)" counter on the dedicated Edmonton Rentals location-search page (not map-viewport-bound) with the exact same filter (Apartment+Townhouse+Fourplex, no condo) confirmed active: **2,526**, well above the payload-derived estimate. Revised coverage: 1,207 unique Edmonton listings captured / 2,526 confirmed true total = **47.8%**, not the ~65-69% first reported. Metro-wide (adding Neil's ~200 estimate for outlying towns not covered by the Edmonton-scoped counter): 1,316 / ~2,726 = ~48%. Lesson: the site's own "Results (N)" badge on a location-search results page is the trustworthy ground truth for coverage math going forward, not any per-capture total/total2/cities field, which are all viewport-dependent |
| 2026-08-13 | **[PARTLY SUPERSEDED 2026-08-14 -- the near-total overlap is real, but the '~52% gap' it tries to explain never existed.]** Second `rf_console_capture.js` session (`console_session_2.json`, 47 sub-captures, 1,016 unique listings) added almost nothing: 1,015 of 1,016 were already in session 1, net +1 listing to the whole pull (1,316 -> 1,317 metro-wide). Cause: the in-browser seen-id dedup only tracks one tab session, so a fresh paste/tab for session 2 had no memory of session 1's results and re-recorded the same central-Edmonton listings as if new. Session 2's own geographic spread (898 rows east of -113.490, 266 south of 53.478, 137 west of -113.611) shows session 1's "swept the whole map" pass already reached those zones, so the west/east/south/far-north "still open" list from before session 1 existed is now stale -- the remaining ~52% gap is not a simple unswept quadrant anymore. Working theory: at wide/medium zoom the map shows clustered pins (the "44", "36" etc. count badges), and map.json may only return a partial sample behind a cluster until zoomed in close enough for it to break apart -- meaning a citywide pan at one zoom level can visually cover every neighborhood while still under-fetching what's bundled in denser clusters. Untested; suggested next step is a targeted zoom-in on a few dense cluster areas to see if that adds meaningfully more unique ids |
| 2026-08-13 | **[SUPERSEDED 2026-08-14 -- the gap being theorized about was a measurement artifact.]** Tested the cluster theory above (`console_session_3.json`, zoom level 15, 5 sub-captures, 65 unique listings): result was 0 new listings, all 65 already captured, and every sub-capture at this zoom showed an exact match between array length and total2 (e.g. 40=40, 19=19) -- no truncation at this zoom either. Theory not supported by this test: this specific area (roughly Bonnie Doon/Idylwylde, east-central Edmonton) had already been completely captured by an earlier pass, cluster markers or not. Revised conclusion: the remaining ~52% coverage gap is more likely genuine unswept geography -- neighborhoods no capture has touched at all -- rather than a zoom-level artifact hiding listings in places already panned over. Re-zooming into already-covered areas is unlikely to help further; finding literally-unvisited areas (or a supplementary non-geographic pass, e.g. price bands, purely as a cross-check) is the more promising next step |
| 2026-08-13 | Built `coverage_map.html` (published artifact, not in repo): a 1.3km density grid over all listings captured so far, with cells flagged as candidate gaps when empty but surrounded by nonzero neighbors (vs. plain edge-of-extent emptiness). Top flagged gaps: Ermineskin (23.7 km2, ~10.5km S of downtown), Jasper Park (11.8 km2, SW), Callingwood (10.1 km2, SW), Windermere (8.5 km2, SW), Tamarack (6.8 km2, SE), Overlanders (6.8 km2, ENE), Belmead (3.4 km2, WSW), Sherbrooke (3.4 km2, NW) |
| 2026-08-14 | **[SUPERSEDED later on 2026-08-14 -- the 48.4% figure is a units mismatch; the +5-new result actually indicates near-complete coverage, not a plateau.]** Fourth `rf_console_capture.js` session (`console_session_4.json`, 58 sub-captures, 693 unique listings): Neil deliberately targeted the flagged gap regions above plus other high-density areas. Result: only +5 genuinely new listings. Cross-checked the file's bbox against the gap list -- it covered 6 of 8 flagged regions (Ermineskin, Jasper Park, Callingwood, Windermere, Sherbrooke, Belmead) and came back with almost nothing new, while missing Tamarack and Overlanders (both east of the swept bbox). Conclusion: the flagged "surrounded by data but empty" cells are most likely genuine non-residential land (river valley, university, industrial, single-detached zones) rather than a capture failure -- consistent with Neil's own read of the areas. Combined Edmonton total now 1,213 / 2,505 confirmed true total (live count drifted down slightly from 2,526) = 48.4%, essentially flat. Coverage looks like it's genuinely plateauing in the high-40s% for this method; Tamarack and Overlanders remain the only two flagged regions not yet re-checked |
| 2026-08-14 | **Found and fixed a real bug in `rentfaster_ingest.py`, never previously run this month**: `load_payload()` only understood a single raw map.json payload per file, so all 4 `console_session_*.json` files (the combined `{"captures": [...]}` shape from `rf_console_capture.js`) silently returned 0 listings -- no error. Had `ingest_snapshot` been run before this was caught, it would have kept only the 3 hand-drawn sections and dropped the majority of the month's data with no warning. Fixed to pool listings across all sub-captures in a combined file, taking the largest sub-capture `total` and the last non-empty `cities` list (consistent with the existing per-file merge semantics in `ingest_snapshot`'s outer loop). Verified against real data, then ran the pipeline for the first time this month across all 7 raw files: `raw_rows=12992 unique=1322 site_total=1840 coverage=0.718`. The printed `coverage=0.718` is the OLD invalid max-per-file-total metric (already documented as broken for section/pan captures) and should be ignored -- the real figure is ~48% from the site's own Results counter. `listings_master.csv`/`snapshots.csv`/`city_totals.csv`/`ingest_log.csv` now exist in `data/rf_data/` for real for the first time this month (gitignored, not committed). Spot-checked the output: 0 duplicate listing_ids, 7 null-price rows (all legitimate junk-price parses, e.g. empty raw price field), 129 rows with no community (matches the documented empty-intro quirk), and every lat/long extreme checked out as a real outlying town (Stony Plain, Leduc, Fort Saskatchewan) -- no bad geocodes found. Type breakdown of the full dataset: Apartment 851 (64.4%), Townhouse 444 (33.6%), Fourplex 27 (2.0%) -- Townhouse is a third of all captured data, so narrowing to Apartment-only (considered, not adopted) would be a real data loss, not just cleanup; whether Townhouse/Fourplex listings actually match anything in the 5+ unit Inventory is still an open question, unverifiable without the workbook. `rent_confidence` breakdown: direct 702 (53%), certain 76 (6%), inferred 248 (19%), blended 296 (22%) -- the blended fifth is a real completeness gap independent of geographic coverage, needs detail-page JSON to resolve (open question #5), and is probably the next-most-valuable improvement after this month's pull wraps up |
| 2026-08-14 | **Root cause of the "missing half the data" problem found: there was no missing half.** `total` in map.json counts SUITE TYPES, not property listings -- `sum(units)` equals `total` exactly in 169/173 captures (the 4 exceptions are the genuine 500-property-cap truncations), independently confirmed on a location-search capture (457 property rows, sum(units)=628=total). The site's "Results (N)" badge uses the same suite-type grain. Every coverage figure in this log before today compared property rows (map pins) against a suite-type counter, a units mismatch. Corrected coverage for the 2026-08-11 pull: 1,213 Edmonton property listings covering **2,436 suite types against the site's 2,505 = 97.2%**, not the 47.8%/48.4% previously recorded. The remaining ~2.8% is consistent with listing churn between capture time and reading the counter. Consequences: (a) the bbox-vs-polygon theory for total/total2 is withdrawn -- West Central's `sum(units)`=746 exactly matches its `total`=746, the empty 3.8 km strip was real but not the cause; (b) the "sub-cap truncation pattern" of 2026-08-13 never existed -- those captures (426 of 921, 406 of 904, 374 of 836) were complete, 426 property rows covering exactly 921 suite types; (c) the residual coverage gap from truncated captures is 0 listings, not the 67 previously estimated, since only 4 captures were genuinely truncated and every listing in them appears in a clean capture too; (d) the ~52% "unswept geography" conclusion and the cluster-thinning theory are both moot; (e) `check_capture.py` fixed to compare `sum(units)` against `total` and to apply the 500 cap to property rows -- flagged captures drop from 7 to 4, all genuine |
| 2026-08-18 | **Step 3 redesigned in the companion repo** (`property-merge-pipeline/docs/PLANNING_2026-08-18.md`). The original design here — nearest building within ~50m as primary, address as cross-check — is withdrawn. Measured on 9,323 real Edmonton multifamily parcels: median nearest neighbour is **10.0m** and **79.7% have another parcel inside 50m**, while RentFaster pin error is median **9.3m**. Measurement noise equals candidate separation, so position can confirm a match but can never decide one. Rule is now **address proposes, proximity confirms**, and proximity is not available yet anyway because Inventory carries no coordinates from any source |
| 2026-08-18 | Matching validated against Inventory for the first time: **785 of 1,322 listings match a building on civic address**. Broken out by type — Apartment 71.4%, Townhouse 23.9%, Fourplex 7.7%. The long-standing "55% match rate" is therefore mostly **composition, not failure**, and it closes the open question in Section 4 about whether Townhouse/Fourplex listings match a 5+ unit inventory: mostly they do not. But `type` must NOT be used to scope capture — the label is poster-chosen and wrong in both directions (a "Fourplex" resolved to an 8-unit building, "Townhouse" to a 311-unit complex). Keep the Apartment + Townhouse + Fourplex, no-condo convention as is |
| 2026-08-18 | Multi-address range notation (`14519/14525 92 Street`) costs real matches. Expanding to each endpoint lifts matches **740 → 785 (+6.1%)**. Implemented as `tools/address_variants.py` in the companion repo, deliberately not folded into its audited `normalize.py` |
| 2026-08-18 | `thumb2` is now load-bearing rather than "present but unused" (Section 4): **1,321 of 1,322 listings carry an image URL**, and with no coordinates on the Inventory side the listing photo is the *primary* verification channel for matching. Surfaced as a clickable link in the review workbook — the human clicks, the browser fetches, nothing here issues a request to Rentfaster |
| 2026-08-25 | map.json availability is now parsed on the snapshot too (`property-merge-pipeline/tools/snapshot_rents.py`): all 27 distinct values resolve, 1,019 of 1,290 available now. Grain still differs — map.json is one value per LISTING, detail is per suite type, so only detail captures support the all-future-dated inference |
| 2026-08-25 | Availability parsed into `available_date` + `available_immediate` per suite, rolled up to `vacancy_signal` per listing. Neil's rule: "Immediate" proves vacancy exists but not how much; all-future-dated with no immediate is evidence of no current vacancy. Counts are suite types not units, and only the detail capture has the grain for it -- map.json carries availability per listing |
| 2026-08-25 | A listing address disagreeing with Inventory by a few civic numbers is often the same building, not a missing one. Meadow Mews advertises at 12408 161 Avenue NW; Inventory holds it at 12404 as a 4-structure complex. Same-side (even) differences of 4 or less are candidates, opposite-side (odd) ones are across the street and are not. Review queue lives in the companion repo as `tools/civic_near_miss.py` |
| 2026-08-25 | A building can be in CoStar and still be absent from this project's CoStar pull. Meadow Mews carries CoStar ID 11289926 but appears in none of the three exports, which cover Secondary Type = Apartments only — it is a condo corporation. Inventory has it from FileMaker alone. Absence from the pull is not absence from CoStar |
| 2026-08-18 | Highest-value next action is **a second capture**, not code. Only one snapshot exists (2026-08-11), so `rent_changes()` and `promo_changes()` have never run against real history, and open question 7 (listing_id stability on dormancy/reactivation) stays unanswerable. That question determines whether the permanent-match design is even correct, so it gates the build |
| 2026-08-14 | **map.json price/beds fields are FILTER-DEPENDENT -- they describe only the suites matching the active query, not the whole property.** Found comparing the same listing across two captures: id 757246 returns `price2` 1292 / `beds2` 2 under a 1050-1350 price band, but `price2` 1477 / `beds2` 3 with no price filter; id 578904 returns `price2` null / `beds2` null banded vs 1899 / 3 unbanded. Two consequences. First, this retroactively justifies rejecting price-band capture (2026-08-11 entry): it would have silently recorded truncated rent ranges for every multi-suite property, corrupting exactly the buildings the rent table most needs. Second and still open: the same mechanism may apply to the property-type filter -- a building advertising both an excluded type (Condo Unit) and an included one could be reporting a range clipped to only the included suites. Not yet tested; worth one capture with all types enabled compared against the standard filter on the same area before Step 4 relies on these ranges |
| 2026-08-11 | Explored price-band filtering (`price_min`/`price_max`) as a possible replacement for geographic quadrants -- confirmed it's a real server-side filter, and a location-search (no draw tool) + single band returned 457 unique citywide listings in one shot, no drawing. Not adopted: a 1050-1350 test band returned zero listings whose price/price2 straddled the band edges, suggesting (unconfirmed) the filter may require a listing's *entire* range inside the band. Real listings in the already-captured sections have spreads up to $4,682 (e.g. id 531465: $1,818-$6,500, studio/1-bed to 3-bed) -- if the straddle theory holds, price banding would silently drop exactly the wide-spread multi-suite-type buildings the rent table most needs, with no band width that both contains the spread and stays under the 500 cap. Designed but did not run a targeted test (tight draw box around 3 known wide-spread listings + a 1050-3000 band) to confirm before committing. Decision: keep geographic sectioning -- it is already proven clean and safe; the price-band gap risk isn't worth resolving right now. Revisit if geographic sectioning becomes too slow |
| 2026-08-10 | Started first real monthly pull (in progress, not yet ingested): draw-tool quadrant captures for Edmonton, snapshot_date 2026-08-10. First quadrant (NW Edmonton) captured but flagged for re-draw -- filter was Apartment+Fourplex only, missing Townhouse/Triplex from the documented convention. Paused mid-pull to start a fresh session; next session should confirm filter is Apartment+Townhouse+Triplex+Fourplex (no condo) before continuing, then resume drawing remaining quadrants |

## 9. Open questions

1. Step 2 geocoder: reuse existing Colab sales geocoder or build fresh against
   City of Edmonton Open Data? (Fresh recommended for Edmonton civic formats.)
2. Metro addresses outside Edmonton proper in Inventory: handle now or defer?
3. Proforma template: Neil to provide format when step 6 starts.
4. `f` field interpretation: confirm against more payloads.
5. ~~Detail-page JSON schema~~ **ANSWERED 2026-08-18** — schema.org JSON-LD, documented in Section 4. Field population rates across a larger sample are still unknown.
6. Suite mix gaps (~3,100 buildings): manual fill prioritization TBD.
7. Listing ID stability on dormancy/reactivation: unknown whether a listing_id
   persists when a unit goes quiet and is re-listed later, or whether Rentfaster
   issues a new id for what's physically the same unit. Can't be tested from a
   single snapshot -- needs 2-3+ months of real captures to observe empirically
   (watch for a listing_id disappearing followed by a new id appearing at the
   same lat/long + address_slug + similar userId). Matters directly for Step 3:
   if ids get reissued, the "match once per listing_id, permanent" design needs
   a secondary key (address_slug + lat/long fingerprint) to survive it, and the
   annual rent rollup's n_unique_listings would overcount without a fix. Keeping
   full raw snapshot history (already the plan) is what makes this answerable
   later without having to re-capture anything.
8. apartments.com (and similar non-map-first sites) explored 2026-08-04 as a
   possible second bulk source: no single map.json-equivalent found via
   Fetch/XHR. Likely lives in paginated search results, inline page JSON
   (__NEXT_DATA__ / __INITIAL_STATE__-style embeds), or a GraphQL endpoint
   instead of one bulk payload -- would need per-page-source inspection, not
   just Network > Fetch/XHR, to confirm. Not yet pursued further; also carries
   a heavier ToS/anti-scraping risk profile than Rentfaster, worth checking
   before investing time even for manual capture.

## 10. Repo structure

```
RF-Scraping-Project/
  CLAUDE.md                  <- this file
  README.md
  src/
    rentfaster_ingest.py     <- exists (step 1)
    check_capture.py         <- capture QC: run per section file before ingest
    geocode_inventory.py     <- step 2
    match_engine.py          <- step 3
    rent_table.py            <- step 4
    signals.py               <- step 5
  tools/
    rf_console_capture.js    <- browser console helper: auto-saves map.json
                                 responses while Neil pans manually (2026-08-11)
  data/
    raw/                     <- monthly map.json captures (gitignored)
    rf_data/                 <- pipeline outputs (gitignored, logs kept via .gitkeep)
    inventory/               <- inventory xlsx (gitignored, sensitive)
  notebooks/
    rentfaster_ingest.ipynb  <- Colab notebook wrapping step 1
  .gitignore                 <- data files, credentials
```

Note: inventory xlsx contains owner names/contact-adjacent data. Keep the repo
private and gitignore raw data files.
