# Brief: Rentfaster network risk assessment (Claude in Chrome)

Written 2026-11-24. For a Claude in Chrome session in Neil's own browser. Purpose: decide
whether tier 3 access (CLAUDE.md Section 4) is safe to build: calling `/api/map.json` from
Neil's logged-in tab over a grid of map areas, and a faster or larger detail queue.
Read-only. This session observes; it builds nothing.

## Ground rules

- Neil's browser, normal logged-in session. Never from a cloud container or headless browser.
- At most ~30 page loads in total, 30-60 s apart. Pan and zoom the map by hand-like steps only.
- Stop at once on any Cloudflare challenge, CAPTCHA, "verify you are human", 403 or 429, and report it.
- Do not record phone numbers, landlord names or cookie values. Report cookie NAMES and lifetimes only.
- Do not replay or modify any request in this session. Observation only.

## Already known (2026-11-20 discovery, do not repeat)

Endpoints: `/api/map.json` (79-82 KB, ~0.5 s), `/api/views/<id>.json`, `/api/SimilarListings.json`,
`/api/moreListingsFromCompany.json`, `/api/accounts/info.json`, `/api/boundaries/city.json`.
Cloudflare `/cdn-cgi/challenge-platform` and `/cdn-cgi/rum` on every page. No challenge in 12 loads.
That session could NOT see methods, query strings, request or response headers, or cookies.

## Questions to answer

1. **map.json request shape.** Method (GET/POST), full parameter list (bbox / `l` / `area`, type
   filter, zoom, `max`, draw polygon), and whether the parameters are plain and readable.
2. **What authorises it.** Is any token, nonce, signature or rotating parameter sent (header,
   query or body)? Does it depend on a Cloudflare cookie (`cf_clearance`, `__cf_bm`)? Cookie names and expiry only.
3. **Rate signals.** Any `RateLimit-*`, `Retry-After`, `X-RateLimit-*` or `cf-*` response headers on
   map.json and on a listing page. `Cache-Control` / `Age` values (is map.json cached at the edge?).
4. **Request cadence of the site itself.** How many map.json calls one manual pan or zoom fires, and
   how close together. This is the baseline any in-page fetch must stay under.
5. **`/api/views/<id>.json`.** When it fires, method, and what the response holds (a view counter
   would be a demand signal).
6. **Listing pages.** Response headers on a `/properties/...` document: caching, `cf-cache-status`,
   any bot-score or challenge header.

## Report

One table per question with the observed values, then a short assessment: for tier 3 map
fetch and for a faster queue, what would make it look different from Neil browsing, and the
lowest-risk design if it goes ahead (cadence, cap, stop conditions). Neil decides from that.
