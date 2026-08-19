// ==UserScript==
// @name         Rentfaster detail capture
// @namespace    rf-scraping-project
// @version      1.3
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
    // Find the "Promotions" heading.
    var heading = null;
    var nodes = document.querySelectorAll('h1,h2,h3,h4,h5,div,section,span,p');
    for (var i = 0; i < nodes.length; i++) {
      var t = (nodes[i].textContent || '').trim();
      if (t.toLowerCase() === 'promotions' && t.length < 20) { heading = nodes[i]; break; }
    }
    if (!heading) return null;

    // Walk up only until the container also holds a promo TYPE label. v1.1
    // walked up on text length alone and swallowed the whole page, so Floor
    // Plans fields (Deposit, Unit Number) were mis-read as promo fields.
    var TYPES = /(Rent Special|Promo Available|Move[- ]?in Gift|Other Promotion)/i;
    var box = heading.parentElement, guard = 0;
    while (box && guard++ < 8) {
      if (TYPES.test(box.textContent || '')) break;
      box = box.parentElement;
    }
    if (!box) return null;

    // Read with textContent, NOT innerText. innerText skips CSS-hidden
    // elements, and these accordions are collapsed by default -- which is why
    // v1.1 captured only the type labels and none of the discount, lease
    // length or validity fields. textContent includes hidden nodes.
    var parts = [];
    (function walk(node, depth) {
      if (!node || depth > 14 || parts.length > 400) return;
      var kids = node.children || [];
      if (!kids.length) {
        var leaf = (node.textContent || '').replace(/\s+/g, ' ').trim();
        if (leaf) parts.push(leaf);
        return;
      }
      for (var i = 0; i < kids.length; i++) walk(kids[i], depth + 1);
    })(box, 0);

    var seen = {}, lines = [];
    parts.forEach(function (line) {
      if (line.length > 400) line = line.slice(0, 400);
      if (seen[line]) return;          // the walk can surface a value twice
      seen[line] = 1;
      lines.push(line);
    });
    var raw = lines.join('\n');
    if (raw.length > 4000) raw = raw.slice(0, 4000);

    // Bound the parse to the promotions block itself: from the "Promotions"
    // heading to the landlord disclaimer or the next section. The container
    // walk lands on an ancestor that still spans the page header and Floor
    // Plans, so scoping by container alone is not enough.
    var start = lines.indexOf('Promotions');
    var slice = start === -1 ? lines : lines.slice(start + 1);
    var stop = slice.length;
    for (var s2 = 0; s2 < slice.length; s2++) {
      if (/^\*/.test(slice[s2]) || /^(Floor Plans|Similar Listings|Description)$/i.test(slice[s2])) {
        stop = s2;
        break;
      }
    }
    slice = slice.slice(0, stop);

    var promos = [], current = null, pendingLabel = null;
    slice.forEach(function (line) {
      var m = line.match(TYPES);
      if (m && line.length < 40) {
        current = { type: m[1], headline: '', body: [], fields: {} };
        promos.push(current);
        pendingLabel = null;
        return;
      }
      if (!current) return;
      // Skip bare URLs before anything else -- otherwise one gets swallowed
      // as a continuation of the preceding label, e.g. Valid becoming
      // "Jun 03, 2026 - Sep 01, 2026 - https://...".
      if (/^https?:\/\//i.test(line) || /^www\./i.test(line)) { pendingLabel = null; return; }

      // A label can sit on its own line with its value on the next -- e.g.
      // "Valid :" then "Aug 18, 2026" then "Sep 01, 2026". Collect those.
      if (pendingLabel) {
        if (/^[A-Za-z ]{3,20}\s*:?$/.test(line) && /:$/.test(line)) {
          pendingLabel = line.replace(/\s*:\s*$/, '').trim();
          return;
        }
        // Values are short (dates, amounts, terms). Anything long is prose,
        // so stop collecting rather than gluing a paragraph onto the label.
        if (line.length > 60) { pendingLabel = null; }
        else {
          var have = current.fields[pendingLabel];
          current.fields[pendingLabel] = have ? have + ' - ' + line : line;
          return;
        }
      }
      if (/^[A-Za-z ]{3,20}\s*:\s*$/.test(line)) {
        pendingLabel = line.replace(/\s*:\s*$/, '').trim();
        return;
      }

      // Inline "Label: value", but not a URL -- "https://..." was being
      // stored as a field named "https".
      var kv = line.match(/^([A-Za-z][A-Za-z ]{2,19})\s*:\s*(.+)$/);
      if (kv && !/^https?$/i.test(kv[1].trim())) {
        current.fields[kv[1].trim()] = kv[2].trim();
        return;
      }
      if (!current.headline) current.headline = line;
      else current.body.push(line);
    });
    promos.forEach(function (p) { p.body = p.body.join(' '); });

    // Bounded markup snapshot, so a wrong parse can be diagnosed offline
    // instead of costing another round of captures. Two guesses at this
    // structure have been wrong already.
    var html = '';
    try { html = (box.outerHTML || '').slice(0, 6000); } catch (e) {}

    return { raw: raw, promos: promos, html: html };
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
