// ==UserScript==
// @name         Rentfaster detail capture
// @namespace    rf-scraping-project
// @version      1.0
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

  function capture() {
    var id = listingId();
    if (!id) return 0;
    var list = load();
    if (list.length >= MAX) return 0;
    if (list.some(function (c) { return c.listing_id === id; })) return 0;  // already have it

    var payloads = readLinkedData();
    if (!payloads.length) return 0;

    payloads.forEach(function (p) {
      list.push({
        ts: new Date().toISOString(),
        url: location.href,
        listing_id: id,
        source: 'userscript:ld+json',
        data: p
      });
    });
    save(list);
    return payloads.length;
  }

  function summary() {
    var list = load();
    var suites = 0, withSqft = 0;
    list.forEach(function (c) {
      var places = (c.data.mainEntity && c.data.mainEntity.containsPlace) || [];
      suites += places.length;
      places.forEach(function (p) { if (p.floorSize && p.floorSize.value) withSqft++; });
    });
    return { listings: list.length, suites: suites, withSqft: withSqft };
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
    el.textContent = s.listings + ' listings · ' + s.suites + ' suites (' + s.withSqft + ' with sq ft) ⬇';
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
      if (el) { var s = summary(); el.textContent = s.listings + ' listings · ' + s.suites + ' suites (' + s.withSqft + ' with sq ft) ⬇'; }
    }
  }, 1800);
})();
