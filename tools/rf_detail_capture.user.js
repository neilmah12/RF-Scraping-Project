// ==UserScript==
// @name         Rentfaster detail capture
// @namespace    rf-scraping-project
// @version      1.1
// @description  Keeps the schema.org listing data your browser already loaded, while you browse Rentfaster listings by hand. Issues no requests of its own.
// @match        https://www.rentfaster.ca/properties/*
// @match        https://rentfaster.ca/properties/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

/*
 * Volume companion to tools/rf_detail_capture.js (the console version).
 *
 * WHY THIS EXISTS
 * ---------------
 * The console version has to be re-pasted on every page, because opening a
 * listing is a navigation and page memory is destroyed. That is fine for a few
 * listings and unworkable for the ~800 rows in a match review queue. Installed
 * as a userscript this runs itself on each listing page you open, so clicking
 * links out of the review workbook just works.
 *
 * It does NOT change what happens on the network. You still open every page
 * yourself; this only reads the document your browser already parsed and keeps
 * a copy locally. Nothing is fetched, nothing is sent anywhere, and no page is
 * opened without you clicking it.
 *
 * WHAT IT KEEPS  (schema documented 2026-08-18 from 4 real captures)
 * -----------------------------------------------------------------
 * Rentfaster embeds schema.org JSON-LD in the page:
 *   <script type="application/ld+json"> { "@type": "ItemPage",
 *       "mainEntity": { "@type": ["LocalBusiness","ApartmentComplex"], ... } }
 *
 * mainEntity carries:
 *   name                    building name, or the street address when unnamed
 *   slogan                  often the building name, sometimes a promo line
 *   parentOrganization      the property MANAGER (not necessarily the owner --
 *                           Zen Residential manages a building owned by an
 *                           individual), with telephone and url
 *   address                 streetAddress, locality, region, postalCode
 *   geo                     latitude, longitude
 *   priceRange, telephone, image, petsAllowed, description
 *   amenityFeature[]        13-18 structured amenities per listing
 *   containsPlace[]         ONE ENTRY PER ADVERTISED SUITE TYPE:
 *                             numberOfBedrooms, numberOfFullBathrooms
 *                             potentialAction.price      <- rent for THIS type
 *                             floorSize.value            <- SQUARE FEET
 *                             additionalProperty         <- Availability Date,
 *                                                          Utilities Included
 *
 * containsPlace is what is currently ADVERTISED, not the building's full suite
 * mix -- one capture listed a single 1-bed while its own description mentioned
 * bachelor, 1 and 2 bedroom options. Treat it as a rent observation, not a
 * rent roll.
 *
 * PROMOTIONS (added v1.1)
 * ----------------------
 * The JSON-LD folds promo copy into `description` and loses the structured
 * parts -- the type label, the discount amount, lease length and validity
 * dates all exist on the page and none reach the JSON-LD. G17 Apartments
 * advertises "Discount: $750.00 off / Lease length: 12 months / Valid from:
 * Mar 01, 2026" and the JSON-LD keeps only a run-on sentence. So promotions
 * are read from the rendered document instead, and stored under a
 * `promotions` key beside `data`.
 *
 * INSTALL
 * -------
 * 1. Install Tampermonkey (or Violentmonkey) in your browser.
 * 2. Dashboard -> Create a new script -> paste this file -> save.
 * 3. Open listing pages normally. A small badge appears bottom-right showing
 *    how many are captured; click it to download everything as one file.
 *
 * The console version stays useful for a quick one-off or for re-running
 * discovery if the page structure ever changes.
 */
(function () {
  'use strict';

  var STORE = '__rfDetailCaptures';
  var MAX = 2000;

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || '[]'); }
    catch (e) { return []; }
  }
  function save(list) {
    try { localStorage.setItem(STORE, JSON.stringify(list)); return true; }
    catch (e) { console.warn('[rf-detail] localStorage full -- download and clear.'); return false; }
  }

  function listingId() {
    var m = location.pathname.match(/-(\d+)\/?$/);
    return m ? m[1] : null;
  }

  // Read the JSON-LD the browser already parsed. No request is made.
  function readLinkedData() {
    var found = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (tag) {
      try {
        var obj = JSON.parse(tag.textContent);
        if (obj && obj.mainEntity) found.push(obj);
      } catch (e) { /* not parseable */ }
    });
    return found;
  }


  // --- promotions -----------------------------------------------------------
  // The JSON-LD concatenates promo copy into `description` and drops the
  // structured parts. On the page they are separate labelled fields inside a
  // "Promotions" accordion:
  //
  //     Rent Special
  //       Get up to a $750 MOVE-IN CREDIT!
  //       Discount: $750.00 off
  //       Lease length: 12 months
  //       Valid from : Mar 01, 2026
  //
  // Those labels are what a proforma needs and none of them survive into the
  // JSON-LD, so this reads them from the rendered document. Collapsed
  // accordions still hold their content in the DOM, so nothing needs clicking.
  //
  // The raw section text is always stored alongside the parsed fields. Parsing
  // labelled prose is brittle and the layout may change; keeping the raw means
  // a bad parse can be corrected later without recapturing.
  function readPromotions() {
    var heading = null;
    var all = document.querySelectorAll('h1,h2,h3,h4,h5,div,section,span');
    for (var i = 0; i < all.length; i++) {
      var t = (all[i].textContent || '').trim();
      if (t.toLowerCase() === 'promotions' && t.length < 20) { heading = all[i]; break; }
    }
    if (!heading) return null;

    // Walk up until we find a container that holds more than just the heading.
    var box = heading.parentElement, guard = 0;
    while (box && box.textContent.trim().length < 60 && guard++ < 6) box = box.parentElement;
    if (!box) return null;

    var raw = box.innerText || box.textContent || '';
    raw = raw.replace(/\u00a0/g, ' ').split('\n').map(function (l) { return l.trim(); })
             .filter(Boolean).join('\n');
    if (raw.length > 4000) raw = raw.slice(0, 4000);

    // Split into promo blocks on the known type labels.
    var TYPES = /(Rent Special|Promo Available|Move[- ]?in Gift|Other Promotion)/i;
    var lines = raw.split('\n');
    var promos = [], current = null;
    lines.forEach(function (line) {
      var m = line.match(TYPES);
      if (m && line.length < 40) {
        current = { type: m[1], headline: '', body: [], fields: {} };
        promos.push(current);
        return;
      }
      if (!current) return;
      var kv = line.match(/^([A-Za-z ]{3,20})\s*:\s*(.+)$/);
      if (kv) { current.fields[kv[1].trim()] = kv[2].trim(); return; }
      if (!current.headline) current.headline = line;
      else current.body.push(line);
    });
    promos.forEach(function (p) { p.body = p.body.join(' '); });

    return { raw: raw, promos: promos };
  }

  function capture() {
    var id = listingId();
    if (!id) return 0;
    var list = load();
    if (list.length >= MAX) return 0;

    // Skip a listing already captured -- unless the stored copy predates the
    // promotions support added in v1.1, in which case re-visiting UPGRADES it
    // in place. Without this, plain dedupe would make old captures impossible
    // to enrich without wiping everything and starting again.
    var existing = list.filter(function (c) { return c.listing_id === id; });
    if (existing.length) {
      if (existing.every(function (c) { return c.promotions !== undefined; })) return 0;
      list = list.filter(function (c) { return c.listing_id !== id; });
    }

    var payloads = readLinkedData();
    if (!payloads.length) return 0;

    var promotions = readPromotions();
    payloads.forEach(function (p) {
      list.push({
        ts: new Date().toISOString(),
        url: location.href,
        listing_id: id,
        source: 'userscript:ld+json',
        promotions: promotions,
        data: p
      });
    });
    save(list);
    return payloads.length;
  }

  function summary() {
    var list = load();
    var suites = 0, withSqft = 0, withPromo = 0;
    list.forEach(function (c) {
      var places = (c.data.mainEntity && c.data.mainEntity.containsPlace) || [];
      suites += places.length;
      places.forEach(function (p) { if (p.floorSize && p.floorSize.value) withSqft++; });
      if (c.promotions && c.promotions.promos && c.promotions.promos.length) withPromo++;
    });
    return { listings: list.length, suites: suites, withSqft: withSqft, withPromo: withPromo };
  }

  function download() {
    var list = load();
    if (!list.length) { alert('Nothing captured yet.'); return; }
    var blob = new Blob([JSON.stringify({
      kind: 'rentfaster_detail', captured_at: new Date().toISOString(), captures: list
    })], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'rf_detail_' + new Date().toISOString().replace(/[:.]/g, '-') + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function badge() {
    if (document.getElementById('rf-detail-badge')) return;
    var s = summary();
    var el = document.createElement('div');
    el.id = 'rf-detail-badge';
    el.style.cssText = [
      'position:fixed', 'bottom:14px', 'right:14px', 'z-index:2147483647',
      'background:#1F4E6B', 'color:#fff', 'font:12px/1.4 system-ui,sans-serif',
      'padding:7px 11px', 'border-radius:5px', 'cursor:pointer',
      'box-shadow:0 2px 10px rgba(0,0,0,.28)', 'user-select:none', 'opacity:.93'
    ].join(';');
    el.title = 'Click to download captures. Shift-click to clear.';
    el.textContent = s.listings + ' listings · ' + s.suites + ' suites · ' + s.withPromo + ' promos ⬇';
    el.addEventListener('click', function (e) {
      if (e.shiftKey) {
        if (confirm('Clear all captured listings?')) {
          localStorage.removeItem(STORE);
          el.textContent = 'cleared';
        }
        return;
      }
      download();
    });
    document.body.appendChild(el);
  }

  var added = capture();
  badge();
  if (added) console.log('[rf-detail] captured listing ' + listingId() + '. Total: ' + summary().listings);

  // Some pages inject the JSON-LD slightly late.
  if (!added) setTimeout(function () {
    if (capture()) {
      var el = document.getElementById('rf-detail-badge');
      if (el) { var s = summary(); el.textContent = s.listings + ' listings · ' + s.suites + ' suites · ' + s.withPromo + ' promos ⬇'; }
    }
  }, 1800);
})();
