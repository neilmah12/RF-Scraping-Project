# Rentfaster Market Intelligence + Proforma Engine

Project reference file. Written 2026-07-30. This document is the persistent context
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

File pattern: `inventory_merge_*.xlsx`. Sheets: Inventory (5,252 rows x 78 cols),
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
multifamily; may revisit), opens DevTools Network tab,
and saves map.json responses. Pipeline ingests whatever files he saves. Keep it
this way — do not add automated fetching.

### map.json schema (documented 2026-07-30)

Per listing: `id`/`ref_id` (stable listing ID), `userId`, `latitude`, `longitude`,
`city`, `city_id` (2=Edmonton, 43=St. Albert, 33=Sherwood Park, 34=Spruce Grove,
39=Leduc, 31=Fort Sask, 36=Beaumont), `community`, `type`, `intro` (display
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
- **Real per-response cap is 500 listings, not the 800 implied by `search.max`.**
  Confirmed 2026-08-10 across two independent captures: both returned exactly
  500 listings regardless of `total`/drawn area size, even though `search.max`
  reports `"800"` in the payload. Judge whether a drawn/zoomed area is small
  enough by checking its `total` field against 500 (aim comfortably under, e.g.
  ~450, since 500-vs-500 at the boundary is ambiguous — can't tell if you got
  everything or got truncated right at the edge). `total` field gives the true
  count for the current filtered view (e.g. 2,496 citywide, 2,251 for a large
  draw). Multiple zoomed-in quadrant/draw-tool captures per month are required
  to stay under the real cap. Ingest script reports coverage and warns <90%.
- **`total` and `total2` can diverge; when they do, `total2` matches the actual
  `listings` array length, not `total`.** Confirmed 2026-08-10 on a small
  draw-tool capture (`total: 154`, `total2: 89`, `listings` length 89). In
  every earlier capture the two fields were identical, so this wasn't visible
  until a smaller/narrower-filtered draw exposed it. Use `total2` (or
  `min(total, total2)`) as the authoritative "true count for this view" when
  checking coverage, not `total` alone. Not yet reflected in `ingest_snapshot`'s
  coverage calculation, which currently only reads `total` — worth fixing
  before relying on the coverage warning for captures where the two might
  differ. Second occurrence 2026-08-11 (NW section: `total: 153`, `total2: 80`,
  array length 80) — again on a draw-tool capture, again with `total2` matching.
  Working hypothesis: `total` counts the rectangular bounding box of the drawn
  shape (the `search.area` field is always a bbox, even when `draw: "1"`) while
  `total2` counts the polygon actually drawn. Strengthened 2026-08-11 by the
  West Central section (`total: 746`, `total2: 369`, array 369): its returned
  listings stop 3.8 km short of the bbox's west edge and 0.8 km short of the
  north edge, leaving dense west-end area inside the bbox with zero listings.
  Listing extent is 57% of bbox area against a 49% count ratio — consistent.
  Still not proof (a concave polygon's extent overstates its area, which is why
  NW shows 82% extent against a 52% ratio). Definitive test: one rectangular
  non-draw-tool capture, where the two fields should agree exactly.
  Practical consequence: do NOT read `total2 < total` as lost coverage. The
  gap is area outside the drawn shape, and it gets picked up by the adjacent
  section.
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
| 2026-08-10 | Started first real monthly pull (in progress, not yet ingested): draw-tool quadrant captures for Edmonton, snapshot_date 2026-08-10. First quadrant (NW Edmonton) captured but flagged for re-draw -- filter was Apartment+Fourplex only, missing Townhouse/Triplex from the documented convention. Paused mid-pull to start a fresh session; next session should confirm filter is Apartment+Townhouse+Triplex+Fourplex (no condo) before continuing, then resume drawing remaining quadrants |

## 9. Open questions

1. Step 2 geocoder: reuse existing Colab sales geocoder or build fresh against
   City of Edmonton Open Data? (Fresh recommended for Edmonton civic formats.)
2. Metro addresses outside Edmonton proper in Inventory: handle now or defer?
3. Proforma template: Neil to provide format when step 6 starts.
4. `f` field interpretation: confirm against more payloads.
5. Detail-page JSON schema: document when first captured.
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
