# Brief: apartments.com discovery session (Claude in Chrome)

Written 2026-11-24 for a Claude in Chrome session in Neil's own browser. Paste this whole file
as the first message. Read-only observation: this session builds nothing and changes nothing.

## 1. Who and why

Neil is a Multifamily Associate in Edmonton, Alberta (commercial real estate, multifamily). He
builds rental market surveys: for a subject building, a set of comparable buildings with rent per
suite type, square feet, rent per square foot, incentives (months free, $ off), net effective rent,
availability status, parking and utilities. Preferences: concise and direct, no em dashes, Excel
as the output.

Today the live rent data comes from **RentFaster.ca**. We want to know whether **apartments.com**
(Canadian listings, Edmonton and Calgary) can be a second source: to cross-check RentFaster, to
fill buildings that do not list on RentFaster, and to catch incentives and floor plans RentFaster
misses.

## 2. What we already have on RentFaster (the benchmark to compare against)

| Need | RentFaster source | How we get it |
|---|---|---|
| Every listing in the metro, monthly | `/api/map.json`: one JSON response per map view, up to 500 listings, with id, lat/long, address, type, beds range, price range, promo flags, poster id | Neil pans the map by hand; a console helper saves each response the browser already received |
| Per-suite detail | Listing page HTML carries **schema.org JSON-LD** (`ApartmentComplex` with `containsPlace[]`: beds, baths, rent, square feet, availability, utilities) plus inline `window.units` (floor plan names, external ids) and `window.listing` | A Tampermonkey userscript reads what the page already loaded; a paced queue (30-90 s gaps, daily cap, stops on any challenge) opens listings one at a time |
| Incentives | Promo box, Floor Plans block, description text, and sometimes only the hero image | Text parsed; hero image saved and read |
| Building facts | Rarely: Year Built shows on about 1 in 68 pages | Comes from Neil's internal inventory instead |

Access rules we apply (and apply here too): every request comes from Neil's own browser; nothing
server-side, headless, parallel, or that works around a challenge.

## 3. Ground rules for this session

- Neil's own browser, normal session. Never a headless or cloud browser.
- About 30 page loads in total, 30-60 s apart, browsing as a person would: search pages, a map view,
  and listing pages.
- **Stop at once** on any challenge, CAPTCHA, "verify you are human", "access denied", 403 or 429,
  or a page that looks unlike the normal site, and report it. Do not retry or work around it.
- Observation only: read the Network panel, page source and console. Do not replay, edit or script
  any request. Do not fill in forms or contact any landlord.
- Do not record phone numbers, emails, landlord or tenant names, or cookie VALUES (cookie names and
  lifetimes are fine).
- apartments.com is reported to run heavier bot protection than RentFaster. Treat any sign of it as
  a finding, not an obstacle.

## 4. Where to look

Use Edmonton first, then one Calgary check:
- A search results page for Edmonton apartments, and the same area in map view if the site has one.
- Zoom and pan the map a few times to see what fires.
- 6-10 listing pages, mixed: a large professionally managed building, a small walk-up, a new-build
  with named floor plans, and a townhouse complex.
- If possible, include buildings we already hold on RentFaster so the fields can be compared:
  Capital View Tower (Edmonton), The Towns at Shawnee Park (Calgary), Junction 21 (Calgary),
  Essential Seton Townhomes (Calgary).

## 5. Questions to answer

**A. Bulk / search data (our map.json equivalent)**
1. When the search or map loads or moves, which requests return listing data? Endpoint paths, method
   (GET/POST), and whether the response is JSON, HTML fragments, or GraphQL.
2. Per listing in that payload: which fields (id, name, address, lat/long, beds range, price range,
   units available, incentive or special flag, property type, last updated)?
3. Is there a per-response cap or paging? How many listings does one map view return versus the
   count the site shows?
4. Are the request parameters readable (bounding box, filters, page number) or opaque/signed tokens?
5. Is the search data instead embedded in the page HTML (for example `__NEXT_DATA__`,
   `__INITIAL_STATE__`, `window.*` objects, or JSON-LD on the results page)?

**B. Listing detail (our JSON-LD + window.units equivalent)**
6. Does the listing page carry schema.org JSON-LD? Which `@type`, and does it list units or floor
   plans with rent, beds, baths and square feet?
7. Any other structured data inline (script tags with JSON, `window.*` objects, hydration state)?
   Name the keys, sample one structure (shape and field names, not personal data).
8. Floor plans: are named plans shown with rent ranges, square feet and availability counts or dates?
   Per-unit rows (unit number, rent, available date)?
9. Incentives: where do specials appear (a labelled specials section, a badge, description text, a
   photo)? Are the terms structured (months free, $ off, lease length) or free text?
10. Building facts: year built, number of units, number of storeys, management company, parking and
    utilities, pet policy. Which are present and where?
11. Any "last updated" or "price changed" timestamp, rent history, or days-on-market indicator?

**C. Access and protection**
12. Bot protection seen: vendor scripts or cookies (names only), challenge pages, rate signals.
13. Response headers on the listing data requests and on a listing page: caching
    (`Cache-Control`, `Age`), any `RateLimit-*` / `Retry-After` / vendor headers.
14. How many data requests one normal page view or one map pan fires, and how close together.
15. Anything that would make a userscript like ours (read-only on pages Neil opens) fail or stand out.

**D. Coverage cross-check**
16. For the RentFaster buildings in section 4: present on apartments.com? Same rents and suites, or
    different? Any building or incentive that appears only on one site?
17. Rough sense of Edmonton coverage: how many listings the site reports for Edmonton apartments,
    and whether it skews to large managed buildings.

## 6. Report format

Return one Markdown report with:
1. A table per section A-D with the observed values (endpoint, field list, yes/no, counts).
2. One sample of each structured payload found (shape and field names only, values redacted).
3. A short assessment:
   - Is there a usable bulk source, and could the same manual-capture approach work (console helper
     saving responses the browser already received while Neil browses)?
   - Is there a usable detail source a read-only userscript could keep?
   - What apartments.com gives that RentFaster does not (and the reverse).
   - Risks and limits seen, and the lowest-risk way forward if Neil goes ahead.
4. Anything surprising, and what was NOT checked.

Neil decides from the report. Nothing is built until he does.
