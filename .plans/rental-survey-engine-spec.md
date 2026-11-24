# Rental Survey Engine: Spec

Date: 2026-11-24. Status: planning, approved for nothing yet. Source: handoff from claude.ai chat, repo audit, and interview with Neil.

## 1. Goal

Cut the time from "new subject property" to "survey workbook uploaded to Survey_Map". The engine finds comparable buildings (listed and unlisted), refreshes live RentFaster rents for the comps Neil confirms, flags gaps that need a phone call, and exports a client-ready survey plus internal intel.

Volume: 1 to 3 surveys a month, 8 to 15 comps each. Runs in Claude Code sessions (Neil asks, Claude runs the code, hands back the workbook).

## 2. Repos and roles

| Repo | Role |
|---|---|
| **RF-Scraping-Project** (branch `claude/sleepy-goldberg-qhpc6n`) | **Primary.** Survey engine lives here, next to the userscript, queue builder, detail ingest and `survey_catalogue.py`. |
| **Survey_Map** | Downstream consumer. The exporter targets its `parse.ts` header aliases. One change in scope: read and display `Status`. |
| **property-merge-pipeline** | Read-only reference. Supplies a stripped Inventory extract (building facts + native keys, no owners). Join on CoStar ID / FileMaker Excel row / Gettel ID, never Building ID. |

Proforma engine: on hold. The canonical suite table (section 6) is designed so a future proforma can consume it unchanged.

## 3. Data storage

- **Google Drive, private folder**, is the raw archive. Append-only, dated names per city:
  `raw/edmonton/map/`, `raw/edmonton/detail/`, `raw/calgary/map/`, `raw/calgary/detail/`, `inventory/inventory_extract_<date>.csv`.
- **`data/manifest.csv` in git**: one row per raw file: path, city, kind (map/detail/queue_log), capture date, map filter used, n captures, coverage, sha256. This is where filter drift and catchment changes are recorded.
- Raw captures contain phone numbers and landlord names: never committed to git.
- Inventory extract columns: native keys, name, address (normalized), city, year built, units, storeys, building type, CoStar Style, Condo, lat/long. No owner or contact fields.
- Open item: confirm the Drive connector can read the folder from a Claude Code session (first check returned nothing; folder does not exist yet).

## 4. Workflow

1. **Subject in.** Neil gives the subject address plus product, vintage and lifecycle (proposed vs stabilized).
2. **Candidates.** Engine builds a tiered candidate list from Inventory + RentFaster, merged (section 5).
3. **Visual confirmation.** Neil reviews in two places side by side: the Candidates tab (Excel, links, Include Y/N + Reject Reason dropdown) and a candidate sheet uploaded to Survey_Map. He still uses RentFaster's own map as he does today.
4. **Live refresh.** Engine writes `detail_queue_survey_<slug>.csv` for the confirmed listed comps. Neil runs it in Tampermonkey (about 20 min), drops the detail file in Drive.
5. **Build.** Engine ingests, diffs against history, applies flags, exports the workbook.
6. **Calls.** Neil phones or secret-shops the Call List, types results straight into the survey xlsx. Engine re-reads the workbook and stores those rents with source and date.
7. **Upload** to Survey_Map.
8. **Optional retro** (section 9).

## 5. Candidate selection

**Pool:** Inventory buildings with coordinates plus RentFaster listings (map and detail captures). Matched where possible; unmatched listings still appear, flagged "not matched to Inventory". Unlisted Inventory buildings are first-class: a comparable building that is fully leased is a direct comp and a call target.

**Automatic filters (applied):** distance radius, vintage band, product type. **Condo = Yes is always excluded** (Condo blank = unknown, kept, flagged).

**Vintage bands (Neil, 2026-11-24):** two bands only. **New** = built 2010 or later; **Existing** = 2009 or earlier. A candidate must be in the subject's band. Unknown year built goes to Tier B, flagged yellow.

**Expanding radius (Neil, 2026-11-24):** start at 2 km and expand in 1 km rings until at least **10 Tier A candidates** are found. Candidates from the next ring are shown as Tier B, so if Neil rejects some he can pull replacements without rerunning. A maximum radius (proposed 15 km) stops tertiary markets expanding forever; hitting it is reported. Rule to be revisited after the first few surveys.

**Flags (never exclude):**
- Units: flag if under 1/4 or over 4x the subject's units.
- Material: inferred only, "over 6 storeys = concrete (inferred)". Not a screening filter; tower surveys are specified by Neil up front.

**Tiering, to avoid noise:**
- Tier A: passes all filters. Shown first, about 10 to 15.
- Tier B: one filter missed (e.g. 4 years outside the vintage band). Collapsed.
- Rest: hidden, counted only ("62 others in radius excluded: 48 vintage, 14 product").

**Comp memory:** buildings previously comped with the subject, or with buildings near it, get a badge ("comped with X in Oct 2026 Fort Sask survey") and move up one tier.

Both rules can be overridden per survey.

## 6. Data model

- **Canonical suite table** (long format): one row per building x suite type x observation. Fields: building key, RentFaster listing ID, suite key (`listing_id + sublisting`, `external_id` where present, it is null on some suites), beds, baths, sqft, rent, utilities, parking text and $/mo, availability flag, incentive (parsed: kind, months free, $ off, term), source (RentFaster / phone / secret shopper / rent roll / proposed), observed date.
- **Comp graph** (extends `survey_catalogue.py`): survey, subject, candidate, tier, included Y/N, reject reason, date.
- **Survey record**: subject, lifecycle, filters used, net rent settings, template profile.

## 7. Rules

- **Room shares** excluded.
- **Net rent:** base rent less the incentive amortized over **12 months** by default (a stated term overrides). Incentive is computed as a number, never a text search (Fort Sask's `SEARCH("1 month free")` silently misses "2 months free" and "$500 off").
- **Parking and utilities in Net Rent:** a per-survey toggle.
- **Net-effective rule:** "amortized over X months" does not mean the incentive is already in the rent; "reflects net effective pricing / already includes the incentive" does.
- **Staleness:** a RentFaster rent older than **14 days** at export is flagged. Off Market rows keep their last rent, labelled with its date.
- **Status:** Available / Waitlist / Off Market, from availability (Immediate or dated = Available; No Vacancy = Waitlist or Off Market; not listed = Off Market).
- **Subject row:** stabilized or existing assets use RentFaster face rents (face vs face); proposed assets use rent roll or proposed rents, entered by Neil.
- Existing rent hygiene flags (`rent_flag`, `asking_ok`) apply unchanged.

## 8. Flags and Call List

Missing suite type signals: map `beds` range vs detail suite types; map `price2` above highest captured rent; detail suite count below map `units` (20% of listings today); suite seen in earlier captures but gone now; description mentions bed types not captured; No Vacancy / Call for Availability suites; image-only incentives noted by hand.

Call List row: building, phone (from RentFaster if ever listed, else blank for Neil to look up), last known rents + date, and the specific question ("confirm 2BR rent and any incentive; 3BR listed in description but not advertised"). No owner grouping.

## 9. Learning over time

- **Retro (optional, after each survey):** a short session recording what went wrong (missed comp, wrong suite type, bad rent, comp later rejected by client). Stored with the survey and used to tune defaults and rules.
- **Comp recommendation:** v1 is rules plus co-comp counts (badge + boost). An actual learned model (e.g. logistic regression on accept/reject vs distance, vintage gap, product, size ratio, prior co-comp) only once there are about 300 labelled candidate decisions, likely 6+ months at current volume. Reject reasons from the dropdown are the training labels.

## 10. Output workbook

| Tab | Audience | Contents |
|---|---|---|
| Survey | Client | New combined layout (below). Yellow = missing, pink = Off Market, live formulas, links rightmost. Uploads to Survey_Map. |
| Flags + Call List | Internal | Gaps, stale rents, missing suite types, call questions |
| Intel | Internal | Change log per comp (rent and incentive changes with dates), market summary stats by unit type. Drafted for Neil; copied to the client only when useful |
| Candidates | Internal | Tiered list, links, Include Y/N, Reject Reason dropdown, comp-memory badge |

**Combined survey layout:** Building Name, Address, Product, Year Built, Unit Type, SF, Base Rent, PSF, Incentive, Net Rent, Net PSF, Status, Parking, Parking ($/mo), Utilities, Link. Building fields on the first row of each building; helper column carries the name down. Headers must match Survey_Map aliases.

## 11. Survey_Map change (in scope)

Add `status` to the parser; show Off Market pink/greyed on the map; label or exclude Off Market and Waitlist rows in summary stats (user toggle).

## 12. Geocoding

Coordinates only need to be close, not matched 1:1 to a parcel (Neil, 2026-11-24).
- **RentFaster listings** already carry lat/long. Phase 1 needs nothing more.
- **Unlisted Inventory buildings** (Phase 2 only): an address lookup against a public geocoding source, accepting near hits, with misses counted and listed. This is a lookup, not building matching. Full 1:1 assessment matching is **deferred**; RentFaster to Inventory matching and Neil's other data work come first.
- **Confirmed comps:** pinned exactly in Survey_Map (paste lat/long from Google Maps, or drag), as today.

## 13. Capture changes

- **Userscript:** save the full building block (Listing ID, Year Built, Pets, # of Units, Parking) and the `window.units` array on every capture. Today raw text is saved only with a promotion and cut at 4,000 chars, which is why only 5 of 168 captures carry Year Built.
- **Calgary:** start the monthly map pull from the next cycle. Stored as its own city partition. Calgary building facts for unlisted comps: decide later (CoStar year built is loose).
- **Queue pace/cap:** unchanged until the risk assessment (section 14).

## 14. Fetch rule and risk assessment

Rewrite CLAUDE.md's "never automate fetching" as tiers:

Outcome 2026-11-24: the risk session found no viable automated method; tier 3 is not pursued.

| Tier | Example | Status |
|---|---|---|
| Passive | Userscript records pages Neil opens | Allowed |
| Paced queue | Tampermonkey queue in Neil's browser, stops on any challenge | Allowed |
| In-page fetch | `map.json` called from the logged-in tab over a bbox grid | Pending risk assessment |
| Server-side | Headless/Python requests, parallel, challenge bypass | Not allowed |

Risk assessment first (Claude in Chrome, read-only): request parameters of `map.json`, rate-limit headers, Cloudflare behaviour, `/api/views/<id>.json`; plus a read of the current ToS text (3.16, 7.2). Then decide on automated map pulls and a faster queue.

## 15. Phases

0. **Base and hygiene:** Drive folder + manifest; upload all historical pulls; branch decision (build on `sleepy-goldberg`); CLAUDE.md rule rewrite; userscript building-block fix; Inventory extract with coordinates (assessment join).
1. **Survey from confirmed IDs** (BUILT 2026-11-24, `src/survey_engine.py`; Hopewell is the first real test): refresh queue, ingest, diff, flags, Call List, combined layout export, re-read of phone rents. Survey_Map `Status` support.
2. **Candidates:** tiered list from Inventory + RentFaster, Candidates tab, Survey_Map candidate sheet, comp memory.
3. **Learning:** retro sessions, reject-reason labels, model once data allows.
4. **Risk assessment, then time savers:** automated map pulls, faster queue, Calgary detail scale-up.

Phase 4's risk assessment can run any time in parallel; it needs only Neil's browser.

## 16. Decided 2026-11-24

- Reject Reason values: too far, wrong product, wrong vintage, condition/renovation, size mismatch, condo, other.
- Radius and vintage rules: section 5.

## 17. Open items for Neil

- Branch: proposal is to create `main` in RF-Scraping-Project from the `sleepy-goldberg` tip and make it the canonical branch (section 0 of CLAUDE.md updated to match).
- Storage choice: Drive folder vs private GitHub data repo (section 3).
