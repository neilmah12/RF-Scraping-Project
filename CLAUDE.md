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
monthly console capture. Neil filters the map (Apartment + Townhouse + Triplex +
Fourplex; condo units excluded as non-market-rent), opens DevTools Network tab,
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

Known quirks:
- **`f` is a display/promotion tier, NOT furnished** (tentative interpretation;
  same listing appears under multiple f values → dedupe on `id` mandatory)
- **~800 listing cap per response** (`search.max`); `total` field gives true count
  (e.g. 2,496). Multiple zoomed-in quadrant captures per month are required.
  Ingest script reports coverage and warns <90%.
- Some listings have empty `intro` and generic slug (`rentals-edmonton-NNNNN`):
  no address available, lat/long-only matching with lower confidence.

### Suite-type rent inference rule (core decision)

map.json gives ranges, not per-suite-type rents. Inference:
- `units == 1` → price maps to beds directly → confidence `direct`
- `units == 2` → price→beds, price2→beds2 → confidence `inferred`
- `units >= 3` → range only, no per-type mapping → confidence `range_only`

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

## 6. Data store layout

```
rf_data/
  snapshots.parquet(.csv)       append-only, one row per listing per snapshot
  listings_master.parquet(.csv) current state + first_seen/last_seen/active
  ingest_log.csv                per-run coverage QC
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

## 9. Open questions

1. Step 2 geocoder: reuse existing Colab sales geocoder or build fresh against
   City of Edmonton Open Data? (Fresh recommended for Edmonton civic formats.)
2. Metro addresses outside Edmonton proper in Inventory: handle now or defer?
3. Proforma template: Neil to provide format when step 6 starts.
4. `f` field interpretation: confirm against more payloads.
5. Detail-page JSON schema: document when first captured.
6. Suite mix gaps (~3,100 buildings): manual fill prioritization TBD.

## 10. Repo structure

```
RF-Scraping-Project/
  CLAUDE.md                  <- this file
  README.md
  src/
    rentfaster_ingest.py     <- exists (step 1)
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
