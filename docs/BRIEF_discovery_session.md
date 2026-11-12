# Brief: Rentfaster data discovery session (Claude in Chrome)

Written 2026-11-12. Paste this into a new Claude conversation that has the Chrome
extension, together with the files listed under "Attach".

## Who and why
Neil is a multifamily associate in Edmonton building a rent-tracking and proforma
platform on top of Rentfaster asking rents. Today the pipeline uses two things:
1. `map.json` responses (listing-level price and bed RANGES, no per-suite rents).
2. The schema.org JSON-LD on each listing page (per-suite rent, beds, sqft,
   availability, utilities, promo terms), captured by a Tampermonkey userscript.

Gaps we are trying to close: per-suite rent for listings the map blends together,
the full suite mix of a building (an unlisted 3-bed is invisible), listing age and
price history, and anything else the site loads that we are not keeping. Neil is
not a developer, so explain what you find in plain language and say what is
usable and what is not.

## Your job (discovery only, one-time)
1. Read the Network and Console tabs thoroughly on the Rentfaster map page and on
   a few listing pages. Catalog every distinct request the pages make, not only
   `map.json`: URL pattern, method, status, content type, size, and the top-level
   JSON keys of any JSON response. Note anything that looks like price history,
   days on market, last-updated time, floorplan or unit lists, similar listings,
   landlord or poster pages, search pagination, or analytics that leaks useful fields.
2. Compare what the page loads against what we already keep (see the attached
   schema notes). The valuable output is the DIFFERENCE: fields or endpoints we
   do not use today.
3. Visit a small, varied sample of listings (see "Scope") and for each one record
   what the page shows versus what the stored JSON-LD holds.
4. Say whether a monthly pass over a queue of listing pages is feasible, how long
   each page takes to load, and whether you hit any friction (challenge page,
   rate limiting, login prompts).

## Scope and limits (set by Neil for this session only)
Neil has chosen to relax the project's usual "manual only" rule for this discovery
step so you can navigate yourself. The limits below still apply:
- Read-only. Do not log in, submit forms, send messages, click Contact, Apply,
  Book a tour, Save, or anything that creates data on the site or contacts a landlord.
- Total of about 25 page loads: the map page (a few pans or zooms), and 8 to 12
  listing pages chosen to cover: a single-suite listing, a multi-suite blended
  listing, a townhouse, a brand-new building, one with a promotion, one with
  "No Vacancy" suites, and two in St. Albert or Sherwood Park.
- Wait a random 20 to 60 seconds between navigations. Do not open pages in
  parallel and do not loop.
- Do not call endpoints you discover; list them. Do not run `fetch` or `XHR` from
  the console. Observing what the page already requests is fine.
- If you hit a CAPTCHA, a "verify you are human" page, a block, or an unusual
  slowdown, STOP immediately and report. Do not try to get around it.
- Do not download images, and do not save personal contact details (phone numbers
  and landlord names can appear in the page, leave them out of your report).

## Attach
- `CLAUDE.md` (Section 0 handoff map, Section 4 data source, the 2026-11-04 and
  2026-11-12 decision log rows on the detail schema and Altairus).
- `tools/rf_detail_capture.user.js` (what the userscript keeps today).
- One sample `map.json` payload and one sample `rf_detail_*.json` file (Neil will
  attach from his files).
- `detail_queue_<date>.csv` (the monthly queue; use it only to pick the sample).

## Known facts, so you do not rediscover them
- Real per-response cap is 500 property rows. `total` counts suite types (sum of
  `units`), not properties. `f` is a display tier. `date` is posted or renewed
  (28% of repeat listings had it move forward, so it reflects renewals).
- `recent` in map.json equals `date` as a unix timestamp, nothing new.
- Detail pages are server-rendered with JSON-LD, so the detail data does not cross
  the network as a separate JSON request.
- `price` / `beds` in map.json are filter-dependent (they describe only suites
  that match the active filter). Property-type filter in use: Apartment + Townhouse.
- Map price ranges for `blended` listings (3 or more suite types) cannot be mapped
  to bed counts without the detail page.

## Output format
Return a short report with these sections:
1. **Endpoint inventory**: table of every distinct request (pattern, what it returns, keys).
2. **New fields or endpoints**: what we do not capture today, with a one-line
   judgment of how useful it is for rent tracking and why (usable now, usable with
   effort, not useful).
3. **Per-listing sample table**: the listings you visited, what the page showed,
   and whether the stored JSON-LD would have captured it.
4. **Feasibility of a monthly queue pass**: load time per page, friction seen,
   and your recommendation on pacing and daily page cap.
5. **Open questions for Neil.**

Keep raw JSON out of the report except short key lists and a few illustrative values.
