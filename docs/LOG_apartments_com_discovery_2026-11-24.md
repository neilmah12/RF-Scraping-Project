# Log: apartments.com discovery session (2026-11-24)

Session run by Claude in Chrome in Neil's own browser (Windows, normal session), following
`BRIEF_apartments_com_discovery.md`. Purpose: decide whether apartments.com can be a second rent
source alongside RentFaster (cross-check, fill gaps, catch incentives). Edmonton only; Calgary not
checked. Nothing was built. This log is the hand-off for planning in the repo.

---

## 1. Headline findings

1. **There is a usable bulk source.** `POST /api/listing-search` returns every listing in the map view as
   JSON pins. It is the equivalent of RentFaster `map.json`, but much thinner per listing (location, rent
   range, ad tier, special flag only).
2. **The zoomed-out city view hides about 31% of listings.** The map thins pins when zoomed out. Zooming
   one level into each of the 4 map quadrants returns a complete set (verified, see section 4).
3. **A complete Edmonton pull** (filter: apartments + townhomes, entire place) = **812 pins at 754 distinct
   locations, plus 132 extra listings stacked on existing pin locations = 944 listings**. The site reports
   3,271 available units for the same search.
4. **Overlap with RentFaster (Nov 4 capture) is partial.** About 40% of RentFaster listings have an
   apartments.com pin within 60 m; about 60% of apartments.com locations have a RentFaster listing within
   60 m. Dedupe adds **about 299 apartments.com-only locations, +27% coverage** over RentFaster alone.
5. **Rents agree when it is the same building.** Within 60 m, 25 to 40% match exactly and 46 to 60% are
   within 5%. Beyond 60 m, exact matches drop to about 6% (different, often adjacent, buildings).
   **60 m is the matching cutoff.**
6. **Listing pages are rich.** Rents per floor plan, square feet, deposit, unit rows with availability,
   year built, unit count and storeys. Building facts are better than RentFaster (Year Built shows on
   about 1 in 68 RentFaster pages).
7. **Specials and utilities can disagree between sites** (Capital View Tower example, section 6). Keep a
   duplicate cross-check, not just gap filling.
8. **Bot protection: Akamai Bot Manager is present** but nothing triggered across 1 to 2 page loads per
   phase and about 45 map moves. No challenge, CAPTCHA, 403 or 429 at any point.
9. **Open governance question:** apartments.com is owned by CoStar. Terms likely prohibit systematic
   collection, and visits from the office network are identified as Avison Young (Demandbase on the
   page). Check with compliance or the CoStar rep whether this data is available through the existing
   CoStar subscription before making this routine. Not legal advice.

---

## 2. Bulk / search data (map.json equivalent)

### Endpoint

| Item | Value |
|---|---|
| Path | `/api/listing-search?seoZone=...` (single query param) |
| Method | POST, JSON body, JSON response |
| Fires | once per pan or zoom, after the map settles |
| Response size | about 410 KB, mostly HTML for the 40 result cards |
| Request params | readable, no signed tokens |
| Response headers | `cache-control: no-store, no-cache`; no `RateLimit-*` or `Retry-After`; Akamai headers present (`akamai-grn`, `server-timing`); also `x-envoy-upstream-service-time`, `x-request-id` |

### Request body shape (values redacted)

```json
{
  "IsBoundedSearch": true,
  "Geography": {"GeographyType": 0, "v": 0, "VectorId": 0, "ID": "<7 chars>",
                "Address": {"City": "", "State": "", "County": "", "CountryCode": ""}},
  "Map": {"BoundingBox": {"UpperLeft": {"Latitude": 0, "Longitude": 0},
                          "LowerRight": {"Latitude": 0, "Longitude": 0}}},
  "Listing": {"Style": 17}
}
```

`Listing.Style` is a bitmask filter: `1` = apartments, `17` = apartments + townhomes. The "entire place"
filter is reflected in the URL path (`/apartments-townhomes/edmonton-ab/entire-place/`). After some pans
the site switches to a plain "nearby" bounding-box search; pins are unaffected.

### Response shape

```json
{
  "results": {"placardsHtml": "<html for 40 cards>", "hasResults": true, "displayState": 0,
              "availabilityCount": 0, "resultCountText": ""},
  "paging": {"page": 1, "totalPages": 18, "nextUrl": ""},
  "map": {
    "pins": [{"listingKey": "<7 chars>", "latitude": 0, "longitude": 0, "listingTypeId": 0,
              "flags": 0, "minRent": 0, "maxRent": 0, "monetaryUnit": 0}],
    "listingCount": 700, "newListingCount": 0, "totalUnitCount": 0,
    "geography": {"geographyType": 0, "geographyVectorId": 0, "geographyId": "",
                  "geographyDisplay": "", "geographyBounds": {"south": 0, "west": 0, "north": 0, "east": 0}}
  },
  "filters": {"facets": {}, "priceMinimums": [], "priceMaximums": [], "hasTotalMonthlyPrice": false},
  "criteria": {}
}
```

### Pin fields

| Field | Meaning |
|---|---|
| `listingKey` | apartments.com listing ID, 7 chars. Same ID appears in the listing URL: `/capital-view-tower-edmonton-ab/e1y75vp/` |
| `latitude`, `longitude` | location |
| `listingTypeId` | **most likely advertising tier, not property type.** Page 1 of default sort is all type 5 (towers, townhomes, affordable mixed). Counts form a pyramid: 5: 58, 4: 33, 3: 28, 2: about 110, 1: about 580 |
| `flags` | bitmask. 128 always set. **32 = has special** (corroborated on 58 to 70 matched buildings). 16 and 8 unknown (guess: video/virtual tour, verified) |
| `minRent`, `maxRent` | rent range; 0 when "Call for Rent" |
| `monetaryUnit` | currency code |

### Page-level counts

- `listingCount` is a constant 700 in every view (zoomed, filtered). It is a ceiling, not a count. Do not use it.
- `totalUnitCount` is reliable: 3,639 unfiltered, 3,271 with apartments + townhomes + entire place.
- The four quadrant views' unit counts sum to 3,303 against 3,271 city-wide (edge overlap), confirming the
  quadrants cover the whole search.

### Embedded data on the first page load (no API call needed)

- `window.aptsState.as.p`: pins with short keys `k, lat, lng, t, f, nr, xr`, plus `sl` = list of other
  listing keys stacked at the same point (includes the parent key itself).
- First load holds exactly 700 listing keys counting stacked ones. 132 of those are stacked-only and never
  come back as their own pin from the API.
- `window.aptsState.as` also has `ac` (unit count), `lc` (700), `pg` (paging), `lsc` (search criteria with
  bounding box), `f` (facets).
- JSON-LD `SearchResultsPage` / `CollectionPage` → `ItemList` of the 40 cards on the page: name, url,
  address, geo, `offers.lowPrice/highPrice`, `amenityFeature`.

### Result cards (`placardsHtml` and the page's card DOM)

Each card: `data-listingid`, property title, address, rent by bed type (`.bedRentBox`, e.g. "2 Beds $1,890"),
specials text ("1 Month Free", "1.5 Months Free", "$500 Off", "Special Offers"), management logo, amenities,
phone (do not store). 40 cards per page, about 24 pages for 944 listings. This is the "middle layer":
roughly RentFaster map.json level plus specials text.

---

## 3. Comparison with RentFaster map.json

| Field | apartments.com pin | RentFaster map.json |
|---|---|---|
| ID | yes (`listingKey`) | yes |
| Lat/long | yes | yes |
| Rent range | min/max only | price/price2 |
| Special | flag only (bit 32), no terms | promo flags |
| Property type | no (type code is ad tier) | Apartment / Townhouse |
| Name, address | no | yes |
| Beds/baths range | no | yes |
| Unit count | no | yes |
| Poster / landlord ID | no | yes (`userId`) |
| Listed date, availability | no | yes |
| Link | no, but URL contains the key | yes |
| Per-response cap | none on pins, but zoom thinning | 500 listings |

---

## 4. Completeness method and evidence

1. Load the filtered search: `https://www.apartments.com/apartments-townhomes/edmonton-ab/entire-place/`.
2. Install the capture helper (section 9) and seed the union from `window.aptsState` including stacked keys.
3. Zoom one level into each map corner (scroll 3 ticks at the corner pixel), wait, zoom back out (scroll 3
   ticks down at the same pixel). Zooming at a corner covers that quadrant exactly, and zooming back out
   restores the previous view exactly.
4. Check: one level deeper on sub-quadrants.

Results:
- City view (lat span 0.32): 564 pins, missing 248 of 812 known (31%).
- One-level quadrant views (lat span 0.16): each missed 0 to 2 known listings (likely same-coordinate
  collisions).
- Deeper checks (lat span 0.08 and 0.04), 7 sub-quadrants including downtown and Oliver: added 0 new listings.
- An earlier, differently tiled pass (9 views + deep downtown zooms) gave the same total (812 Edmonton
  listings, 750 locations), so the result is reproducible.

**Rule:** 4 quadrant views at one zoom level, plus the first page load for stacked listings, is a complete pull.
About 10 map moves, about 3 minutes.

Earlier wrong turn (keep in mind): the first estimate said "no cap, city view returns everything". Wrong.
The city view thins pins. Also an early provider read ("Boardwalk absent") was an artifact of thinning;
the complete pull shows Boardwalk at 43%.

---

## 5. Overlap analysis vs RentFaster (complete pull)

Inputs: apartments.com complete pull (812 pins, Edmonton) vs RentFaster Nov 4 captures
(`rf_captures_2026-11-04T16-24-58-485Z.json` + `rf_captures_2026-11-04T16-34-40-036Z.json`, deduped by id:
1,307 listings, 1,187 in Edmonton, 769 apartments + 418 townhouses, 1,094 building locations after 35 m
clustering). Matching is **location only** (no names in pins). Snapshots are 20 days apart.

| Metric | 30 m | 60 m |
|---|---|---|
| apartments.com locations with RentFaster nearby | 323 / 754 | 455 / 754 (60%) |
| RentFaster listings with apartments.com nearby | 341 / 1,187 | 476 / 1,187 (40%) |
| RentFaster listings with 3+ units | 106 / 301 | 148 / 301 (49%) |

Rent agreement (apartments.com min rent vs nearest RentFaster min rent):

| Distance | n | Exact | Within 5% | Median gap |
|---|---|---|---|---|
| 0 to 15 m | 238 | 38% | 60% | 1.9% |
| 15 to 30 m | 109 | 40% | 56% | 2.5% |
| 30 to 60 m | 138 | 25% | 46% | 6.6% |
| 60 to 100 m | 109 | 6% | 31% | 14.5% |
| 100 to 250 m | 137 | 6% | 24% | 17.7% |

Even at 0 to 15 m, about 40% disagree by more than 5%: different suites in the same building, stale
listings, or two buildings on one lot. Not resolved.

Dedupe at 60 m: 299 apartments.com-only locations (320 pins), 57 with the special flag.
Combined 1,393 locations vs RentFaster 1,094 = **+27%**.

Provider coverage (share of a poster's RentFaster listings with apartments.com within 60 m):

| RentFaster poster | Listings | Share |
|---|---|---|
| Wellington Park Townhomes | 82 | 96% |
| NW apartments poster ("Modern pet-friendly...") | 69 | 81% |
| Boardwalk Living | 67 | 43% |
| Ascot Gardens townhomes | 41 | 68% |
| Rohit (2 months free townhomes) | 29 | 72% |
| Inglewood 8 Plex | 25 | 28% |
| Astoria Greens | 24 | 25% |
| Stirlingwood Townhomes | 23 | 0% |

---

## 6. Listing detail page (tested on Capital View Tower)

URL pattern: `/<slug>/<listingKey>/` e.g. `/capital-view-tower-edmonton-ab/e1y75vp/`.

Structured data:
- JSON-LD `["Product","RealEstateListing"]` → `mainEntity`: name, address, geo, images, `petsAllowed`,
  `openingHoursSpecification`, **`lastReviewed`** (timestamp), `amenityFeature`,
  `containsPlace[]` = floor plans with `accommodationFloorPlan.numberOfBedrooms`,
  `numberOfBathroomsTotal`, `floorSize.value` (sq ft). **No rent per plan in JSON-LD.**
  `offers`: `lowPrice`, `highPrice`, `availability`.
- Also JSON-LD `BreadcrumbList`, `WebPage`, `FAQPage`.
- `window.aptsState` on a listing page only holds site config (`rs, el, c`). No hydration object like
  RentFaster `window.units`.

Rendered HTML sections (ids): `availabilitySection` (pricing grid: each floor plan with rent, deposit, and
unit rows: unit label, price, sq ft, availability), `feesSection` (fees and policies, plus
"Built in 1964, 115 units/11 stories"), `descriptionSection`, `amenitiesSection`, `officeHoursSection`,
`reviewsSection`, `schoolsSection`, `transportationSection`, `walkScoreSection`, `frequentlyAskedQuestionSection`.
A userscript would parse the DOM for rents and building facts.

Capital View Tower, apartments.com vs RentFaster (both 2026-11-24):

| | apartments.com | RentFaster |
|---|---|---|
| Studio | $1,099, 406 sq ft, now | $1,099, 406 sq ft, immediate |
| 1 bed 1 bath | $1,325, 607 sq ft, $799 deposit | $1,325, 607 sq ft |
| 2 bed 2 bath | $1,499, 845 sq ft | $1,499, 845 sq ft |
| Incentive | none shown | $250 off first month if applying within 24 h |
| Utilities | "not included in rent" | heat and water included |
| Year built / units / storeys | 1964 / 115 / 11 | not given |
| Last updated | `lastReviewed` 2026-10-15 | listing date |

Identical rents and sq ft suggest a shared syndication feed (RentFaster listing carries `s: "RSYNC"`).

---

## 7. Access and protection

- **Akamai Bot Manager:** sensor script at a randomized first-party path (`/Juh4Ty/ANG8_/...`), one POST
  per page load returning 202. Akamai mPulse (`s.go-mpulse.net`). Akamai edge headers on API responses.
- **Demandbase** tags visits with company identity from IP (Avison Young via Zscaler).
- Lots of ad/analytics vendors (Google, Meta, TikTok, Reddit, Pinterest, Snap, LinkedIn, Bing, Criteo,
  Taboola, Teads, Clarity).
- **Cookie names and lifetimes: not checked.** The extension blocks returning cookie data and the Akamai
  cookies are likely HttpOnly. Needs DevTools Application tab (names and expiry only).
- Fetch/XHR wrapper (read-only capture) ran all session without any challenge. Akamai could in principle
  fingerprint a patched `fetch`; low risk at this volume, unknown long-term.
- Total footprint: about 3 page loads (initial search, reload for the clean pull, Capital View Tower by Neil)
  and about 45 map moves across the session, paced 10 to 35 s.

---

## 8. Proposed monthly workflow (to plan in the repo)

1. **Same week, capture both sources:** RentFaster map.json (existing routine) and the apartments.com
   complete pull (section 4, about 10 map moves). Optionally page through the 24 result pages for card
   data (names, bed-type rents, specials text).
2. **Match into three buckets:** location within 60 m **plus a name check** from cards, to avoid false gaps
   (large complexes geocoded at different corners, different landlord names).
   - Both sites: no page visits. Auto-compare min rent and specials. Flag rent gaps over 10% and special
     mismatches for review.
   - apartments.com only: candidates to add.
   - RentFaster only: nothing to do.
3. **Filter apartments.com-only to professionally managed multi-unit buildings** (card has several bed
   types, a management name, ad tier 3 to 5). Visit those listing pages with a paced userscript (same
   rules as RentFaster: 30 to 90 s gaps, daily cap, stop on any challenge). Tag rows as source = apartments.com.
4. **Keep an ID crosswalk:** apartments.com `listingKey` ↔ RentFaster id ↔ internal building id. After month
   one, only new keys need matching (site showed about 75 new listings in one view).
5. Expected volume: month one about 299 gap locations, probably 60 to 150 worth visiting (estimate until
   cards confirm). After that, new keys only.

---

## 9. Code used this session (reference)

Capture helper, pasted in the console on the search page (read-only: wraps `fetch`, copies responses the
page itself receives; does not send, change or repeat requests):

```js
window.__pull = [];
const of = window.fetch;
window.fetch = function (input, init) {
  const url = typeof input === 'string' ? input : input.url;
  return of.apply(this, arguments).then(r => {
    try {
      if (new URL(url, location.href).pathname === '/api/listing-search') {
        r.clone().json().then(j => {
          let q = {}; try { q = JSON.parse(init.body); } catch (e) {}
          window.__pull.push({
            t: Date.now(), bb: q.Map && q.Map.BoundingBox, style: (q.Listing || {}).Style,
            units: j.map.totalUnitCount,
            pins: j.map.pins.map(p => [p.listingKey, p.latitude, p.longitude, p.listingTypeId,
                                       p.flags, p.minRent, p.maxRent])
          });
        });
      }
    } catch (e) {}
    return r;
  });
};
```

Export (download a JSON with the first-load state and every captured response):

```js
(() => {
  const a = window.aptsState.as;
  const out = {
    kind: 'apartments_com_map_pull', captured_at: new Date().toISOString(),
    initial_state: { units: a.ac, pins: a.p.map(o => ({ k: o.k, lat: o.lat, lng: o.lng, t: o.t, f: o.f,
      nr: o.nr, xr: o.xr, stacked: (o.sl || []).map(s => s.k) })) },
    responses: window.__pull.map(r => ({ t: r.t, bb: r.bb, style: r.style, units: r.units,
      pins: r.pins.map(p => ({ k: p[0], lat: p[1], lng: p[2], t: p[3], f: p[4], nr: p[5], xr: p[6] })) }))
  };
  const b = new Blob([JSON.stringify(out)], { type: 'application/json' });
  const el = document.createElement('a');
  el.href = URL.createObjectURL(b); el.download = 'apartments_com_pull.json'; el.click();
})();
```

Note: the helper does not yet store `placardsHtml`. Add it if card data (names, bed-type rents, specials
text) is wanted from the map pull.

Matching logic used (Python, summary):
- Distance: equirectangular, `hypot(dlat*111000, dlng*111000*cos(53.5°))`.
- RentFaster building locations: greedy clustering of listings within 35 m.
- apartments.com locations: clustering of pins within 25 m.
- Match = nearest RentFaster listing within 60 m (also reported at 30 m).
- Rent comparison: apartments.com `minRent` vs RentFaster `price` of the nearest listing; skip rent <= 300.
- Review flags: |gap| > 10%, or apartments.com special bit != RentFaster `promotions` non-empty.
  This specials check is too loose (flags 226 of 812). Tighten using card specials text.

---

## 10. Files produced

| File | Where | Contents |
|---|---|---|
| `apartments_com_edmonton_pull_2026-11-24.json` (476 KB) | Neil's Downloads | raw complete pull: first-load state with stacked keys + 24 API responses, with listing keys |
| `apartments_com_vs_rentfaster_2026-11-24.csv` | sent in chat | 812 pins with nearest RentFaster listing, distance, bucket, rent gap, review flag. No listing keys (join to the JSON on lat/lng if needed) |
| this log | sent in chat | |

---

## 11. Open questions / not checked

- CoStar terms and whether the existing CoStar subscription covers apartments.com asking rents and specials.
- Pin `listingTypeId` meaning (ad tier hypothesis) and `flags` bits 16 and 8.
- Cookie names and lifetimes (needs DevTools Application tab).
- Calgary (brief asked for one check), and the Calgary comparison buildings (Junction 21, Towns at Shawnee
  Park, Essential Seton).
- Only one listing page examined. Specials coverage on apartments.com vs RentFaster needs 2 or 3 more.
- Whether a listing URL works with the key alone (no slug).
- Name-level matching (needs card data).
- Result-card pagination as a source (24 pages) not exercised.
- Why about 40% of same-building pairs disagree on rent by more than 5%.
