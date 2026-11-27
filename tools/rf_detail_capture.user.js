// ==UserScript==
// @name         Rentfaster detail capture
// @namespace    rf-scraping-project
// @version      2.1
// @description  Keeps the schema.org listing data your browser already loaded while you browse Rentfaster listings. Optional queue mode (off until you load a queue and press Start) opens the next listing in this tab on a paced timer.
// @match        https://www.rentfaster.ca/properties/*
// @match        https://rentfaster.ca/properties/*
// @match        https://www.rentfaster.ca/*/*/rentals*
// @match        https://rentfaster.ca/*/*/rentals*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      rf-images-prod-bcdn.rentfaster.ca
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
 * Passive mode (default): it does NOT change what happens on the network. You
 * open every page yourself; this only reads the document your browser already
 * parsed and keeps a copy locally. Nothing is fetched or sent anywhere.
 *
 * Queue mode (v1.5, opt-in): after you load a queue CSV and press Start, it
 * opens the listed pages in this tab one at a time on a paced timer. See the
 * QUEUE MODE section below for pacing, caps and the conditions that stop it.
 *
 * v2.1 (2026-11-27): captures are stored in IndexedDB instead of localStorage. On 2026-11-25 the
 * cumulative store reached 246 captures (about 5.3M characters), past Chrome's roughly 5.2M-character
 * localStorage limit, so a page with good data (777245) could not be saved and the queue stopped on
 * "storage full". IndexedDB holds hundreds of MB, so that stop should not recur. Anything already in
 * localStorage moves across on the first page load and the old key is removed once the copy is
 * written. If IndexedDB cannot be opened (some private windows) the script falls back to localStorage
 * as before. Each page's write finishes before the queue opens the next page. The badge download reads
 * the whole store, so captures from other tabs are included. A challenge row in the queue log now
 * carries a note: which check fired, the page title and the HTTP status. After a challenge, Start asks
 * before resuming within 12 hours (recommended: stop for the day).
 *
 * v2.0 (2026-11-24): keeps a downscaled copy of each listing's HERO IMAGE, because some landlords
 * put their offer only in the photo (The Towns at Shawnee Park: "1 Month Free Rent!" exists in no
 * text on the page). The image is read from the page if the browser allows it; otherwise it is
 * requested once through Tampermonkey from rf-images-prod-bcdn.rentfaster.ca (the only host this
 * script may contact; usually served from the browser cache, since the page just showed it).
 * Downscaled to 720px wide JPEG (~35-60KB), stored in IndexedDB (not localStorage, which would
 * fill in ~100 listings), deduped on the image URL so an unchanged photo is stored once ever.
 * The badge download now also writes rf_images_<ts>_<n>.json files (150 images each) holding only
 * images not yet downloaded; after download the picture is dropped and only the URL is kept, so
 * the same photo is never stored or downloaded twice. Turn off with localStorage __rfdCfg
 * {"images": false}. Needs @grant, so the page's own globals are read via unsafeWindow.
 *
 * v1.9 (2026-11-24): each queue file remembers its position. Loading a different queue no longer
 * loses your place: the outgoing queue's position is saved under the queue's content (length,
 * first and last id, so a v1.8-loaded queue carries over), and loading that same file again resumes where it stopped. A "Go to #" button sets
 * the position by hand. Loading is refused while a queue is running (Pause first). The building
 * facts block now stops at "Explore Similar Rentals" / "Property Managed By" so parking lines no
 * longer swallow the neighbouring sections (seen live on 4 Calgary pages 2026-11-24).
 *
 * v1.8 (2026-11-24): every listing also keeps the building facts block (`building`: Listing ID,
 * Year Built, Pets, # of Units, parking lines, as raw lines plus a light parse) and the page's own
 * `window.units` suite array (`units_js`: per-suite external_id, sublisting, price, sq_feet, beds,
 * unit_notes such as "Jr 1 Bedroom") and `window.listing` when present (`listing_js`, discovery
 * mode, capped). Before v1.8 Year Built and # of Units survived only when they happened to fall
 * inside the 4,000-character promotions text, which was 5 of 168 captures. Revisiting a listing
 * whose stored copy has no `building` key now replaces it, so old captures upgrade in place.
 * Nothing new is fetched: all three are read from the page the browser already loaded.
 *
 * v1.7.1 (2026-11-23): only the tab that pressed Start acts on a running queue. Before, any other
 * Rentfaster tab (for example a map page opened by hand) read the shared queue state and, landing on a
 * results page, logged `unexpected_redirect` and stopped the queue. Other tabs now show
 * "running in another tab" and capture as normal. To move the queue, Pause then Start in the new tab.
 *
 * v1.7 (2026-11-23): listing pages also keep the Floor Plans block as read from the
 * page (`floorplans`: per-suite raw text lines plus a light parse of deposit,
 * furnished, lease term, availability, utilities, sqft). It is stored beside the
 * JSON-LD, and on its own (source `userscript:dom`) when a page has no usable
 * JSON-LD. The queue now retries a no-capture page once after 4 seconds and
 * logs WHY a page produced no data (`note` column), instead of just `no_data`.
 * Errors inside the promotions or floor-plan readers can no longer abort a capture.
 *
 * v1.6 (2026-11-20, from the discovery session): a removed listing does not 404.
 * It redirects, with HTTP 200, to the city results page with #listingNotFound on
 * the address, and a listing can redirect to a sibling listing at the same
 * address. The script now also loads on results pages (doing nothing there unless
 * a queue is running), logs the first case as `gone` and the second as
 * `redirected` (the sibling is kept under its own id with `redirected_from`).
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
 * 4. Queue mode: open any Rentfaster listing page, click "Load queue" in the green
 *    panel and pick detail_queue_<date>.csv, then "Start" and enter a page count.
 *    Keep the tab visible and the computer awake. Download the captures (badge)
 *    and the queue log (Log) when it stops.
 *
 * The console version stays useful for a quick one-off or for re-running
 * discovery if the page structure ever changes.
 */
(function () {
  'use strict';

  var STORE = '__rfDetailCaptures';   // localStorage key used before v2.1 (and the fallback)
  var MAX = 5000;

  // --- capture store (v2.1: IndexedDB) ----------------------------------------
  // CACHE is this tab's copy of the store, loaded once when the page opens, so the rest of the
  // script can stay synchronous. save() updates CACHE at once and queues the IndexedDB write;
  // PENDING resolves true/false when the writes so far have finished. Only records this tab
  // added, changed or removed are written, so a second tab's captures are never deleted here.
  var CACHE = [], KNOWN = {}, USE_LS = false, PENDING = Promise.resolve(true), capDbP = null;
  function capDb() {
    if (!capDbP) capDbP = new Promise(function (res, rej) {
      var r = indexedDB.open('rfd_captures', 1);
      r.onupgradeneeded = function () { r.result.createObjectStore('captures', { keyPath: '_k' }); };
      r.onsuccess = function () { res(r.result); };
      r.onerror = function () { rej(r.error); };
    });
    return capDbP;
  }
  function newKey(c) { return c.listing_id + '|' + c.ts + '|' + Math.random().toString(36).slice(2, 8); }
  function capTx(mode, fn) {
    return capDb().then(function (db) {
      return new Promise(function (res, rej) {
        var tx = db.transaction('captures', mode), st = tx.objectStore('captures'), out = fn(st);
        tx.oncomplete = function () { res(out && 'result' in out ? out.result : undefined); };
        tx.onerror = function () { rej(tx.error); };
        tx.onabort = function () { rej(tx.error || new Error('transaction aborted')); };
      });
    });
  }
  function lsLoad() {
    try { return JSON.parse(localStorage.getItem(STORE) || '[]'); }
    catch (e) { return []; }
  }
  function storeFailed(e) {
    window.__rfdStorageFull = true;
    console.warn('[rf-detail] STORAGE WRITE FAILED (' + (e && (e.name || e.message)) + ') -- click the badge to download, '
                 + 'then shift-click to clear. Nothing new is being saved until you do.');
    return false;
  }
  // Load the store, moving any pre-v2.1 localStorage captures into IndexedDB first.
  function storeInit() {
    return capDb().then(function () {
      var legacy = lsLoad();
      if (!legacy.length) return null;
      return capTx('readwrite', function (st) {
        legacy.forEach(function (c) { if (!c._k) c._k = newKey(c); st.put(c); });
      }).then(function () {
        localStorage.removeItem(STORE);
        console.log('[rf-detail] moved ' + legacy.length + ' captures from localStorage to IndexedDB');
      });
    }).then(function () {
      return capTx('readonly', function (st) { return st.getAll(); });
    }).then(function (all) {
      CACHE = (all || []).sort(function (a, b) { return a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0; });
      KNOWN = {}; CACHE.forEach(function (c) { KNOWN[c._k] = 1; });
    }).catch(function (e) {
      USE_LS = true; CACHE = lsLoad();
      console.warn('[rf-detail] IndexedDB unavailable (' + (e && e.message) + '): using localStorage, about 240 listings fit');
    });
  }
  function load() { return CACHE.slice(); }
  function save(list) {
    if (USE_LS) {
      try {
        localStorage.setItem(STORE, JSON.stringify(list));
        CACHE = list; window.__rfdStorageFull = false; return true;
      } catch (e) { return storeFailed(e); }
    }
    var keep = {};
    list.forEach(function (c) { if (!c._k) c._k = newKey(c); keep[c._k] = 1; });
    var del = CACHE.filter(function (c) { return !keep[c._k]; }).map(function (c) { return c._k; });
    var put = list.filter(function (c) { return !KNOWN[c._k] || c._dirty; });
    CACHE = list;
    if (!put.length && !del.length) return true;
    PENDING = PENDING.then(function () {
      return capTx('readwrite', function (st) {
        put.forEach(function (c) { var r = Object.assign({}, c); delete r._dirty; st.put(r); });
        del.forEach(function (k) { st.delete(k); });
      }).then(function () {
        put.forEach(function (c) { KNOWN[c._k] = 1; delete c._dirty; });
        del.forEach(function (k) { delete KNOWN[k]; });
        window.__rfdStorageFull = false;
        return true;
      }).catch(storeFailed);
    });
    return true;
  }
  // Everything in the store, from every tab (the download reads this, not CACHE).
  function allCaptures() {
    if (USE_LS) return Promise.resolve(lsLoad());
    return PENDING.then(function () { return capTx('readonly', function (st) { return st.getAll(); }); })
      .then(function (all) { return (all || []).sort(function (a, b) { return a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0; }); })
      .catch(function () { return CACHE.slice(); });
  }
  function clearCaptures() {
    localStorage.removeItem(STORE);
    CACHE = []; KNOWN = {};
    if (USE_LS) return Promise.resolve();
    return PENDING.then(function () { return capTx('readwrite', function (st) { st.clear(); }); });
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
      if (seen[line]) return; // the walk can surface a value twice
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


  // --- floor plans (v1.7) -----------------------------------------------------
  // The Floor Plans block is server-rendered. Every suite sits in an element with
  // data-unitname="2 beds, 1 bath, $1559" and its detail lines inside: rent, baths,
  // square feet, furnished, lease term, availability, utilities, deposit. The
  // JSON-LD carries only some of that (no deposit, furnished or lease term), and
  // on a page whose JSON-LD is unusable this is the only place the suites exist.
  // Raw lines are always kept so a wrong parse can be fixed offline.
  var AVAIL = /^(Immediate|No Vacancy|Call for Availability|Negotiable|[A-Za-z]{3,9}\.? \d{1,2}(, ?\d{4})?)$/;

  // Text of an element as an ordered list of text-node strings. Walking text nodes
  // (not leaf elements) keeps mixed content such as "825 ft<sup>2</sup>" intact.
  function leafLines(root, cap) {
    var lines = [];
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false), n;
    while ((n = w.nextNode()) && lines.length < cap) {
      var par = n.parentElement;
      if (par && /^(SCRIPT|STYLE|NOSCRIPT)$/.test(par.tagName)) continue;
      var s = (n.nodeValue || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
      if (s && s.length < 240) lines.push(s);
    }
    return lines;
  }

  function parseUnit(unitname, lines) {
    var out = { beds: '', baths: '', rent: null, sqft: null, furnished: '', lease_term: '',
                availability: '', utilities: '', deposit: '' };
    var m = String(unitname || '').match(/^(.*?),\s*([\d.]+)\s*baths?,\s*\$([\d,.]+)/i);
    if (m) { out.beds = m[1].trim(); out.baths = m[2]; out.rent = parseFloat(m[3].replace(/,/g, '')); }
    var i, util = [], inUtil = false;
    for (i = 0; i < lines.length; i++) {
      var l = lines[i];
      if (/^Utilities Included:?$/i.test(l)) { inUtil = true; continue; }
      if (/^Deposit:?$/i.test(l)) {
        inUtil = false;
        if (lines[i + 1] && /^\$?[\d,.]+/.test(lines[i + 1])) out.deposit = lines[i + 1];
        continue;
      }
      if (inUtil) { if (!/^(•|&bullet;|•)$/.test(l)) util.push(l); continue; }
      if (/^(un|partially )?furnished$/i.test(l)) out.furnished = l;
      else if (/^(long|short) term$|^month[- ]to[- ]month$|^\d+\s*months?$/i.test(l)) out.lease_term = l;
      else if (!out.availability && AVAIL.test(l)) out.availability = l;
      else if (out.sqft === null && /^\d[\d,.]*\s*ft/i.test(l)) out.sqft = parseFloat(l.replace(/,/g, ''));
      else if (out.sqft === null && /^\d[\d,.]*$/.test(l) && /^ft/i.test(lines[i + 1] || '')) out.sqft = parseFloat(l.replace(/,/g, ''));
    }
    out.utilities = util.join('|');
    return out;
  }

  function readFloorPlans() {
    var els = document.querySelectorAll('[data-unitname]');
    if (!els.length) return null;
    var units = [], sample = '';
    for (var k = 0; k < els.length && k < 60; k++) {
      var el = els[k];
      var lines = leafLines(el, 60);
      // the suite-group header ("2 Bedrooms", "Apartment", "1 unit") sits just outside the element
      var group = [], a = el.parentElement, up = 0;
      while (a && up++ < 5) {
        if (/\b\d+\s*units?\b/i.test((a.textContent || '').slice(0, 400))) { group = leafLines(a, 6); break; }
        a = a.parentElement;
      }
      units.push({
        key: el.getAttribute('data-sublisting') || '',
        unitname: el.getAttribute('data-unitname') || '',
        group: group.slice(0, 4),
        lines: lines,
        parsed: parseUnit(el.getAttribute('data-unitname'), lines)
      });
      if (!sample) { try { sample = (el.outerHTML || '').slice(0, 4000); } catch (e) {} }
    }
    return { units: units, sample_html: sample };
  }

  // --- building facts + page globals (v1.8) ----------------------------------
  // The page shows a labelled block with Listing ID, Year Built, Pets, # of Units and
  // parking. None of it is in the JSON-LD. Find the "Listing ID" label, climb to the
  // container that also holds "Year Built" or "# of Units", and keep its text lines.
  var BLD_LABELS = ['Listing ID', 'Year Built', 'Pets', '# of Units', 'Smoking', 'Monthly Fee',
                    'Parking Information', 'Street Parking', 'Parking Spots',
                    'Total Property Parking Spots', 'Visitor Parking', 'Furnishing', 'Lease Term'];
  function readBuilding() {
    var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false), n, label = null;
    while ((n = w.nextNode())) {
      if ((n.nodeValue || '').trim() === 'Listing ID') { label = n.parentElement; break; }
    }
    if (!label) return null;
    var box = label, up = 0;
    while (box.parentElement && up++ < 8) {
      box = box.parentElement;
      var t = box.textContent || '';
      if (/Year Built|# of Units/.test(t) && /Parking|Pets/.test(t)) break;
    }
    var lines = leafLines(box, 120);
    var fields = {}, i, cur = null;
    var STOP = /^(Explore Similar Rentals|Property Managed By|Similar Listings|Contact Landlord|Report|Share Listing)$/i;
    for (i = 0; i < lines.length; i++) {
      var l = lines[i];
      if (STOP.test(l)) { lines = lines.slice(0, i); break; }
      if (BLD_LABELS.indexOf(l) !== -1) { cur = l; if (!(cur in fields)) fields[cur] = []; continue; }
      if (cur && fields[cur].length < 6) fields[cur].push(l);
    }
    Object.keys(fields).forEach(function (k) { fields[k] = fields[k].join(' | '); });
    var num = function (v) { var m = String(v || '').replace(/,/g, '').match(/\d+/); return m ? parseInt(m[0], 10) : null; };
    return {
      lines: lines,
      fields: fields,
      year_built: num(fields['Year Built']),
      n_units: num(fields['# of Units']),
      pets: fields['Pets'] || '',
      parking: [fields['Parking Information'], fields['Parking Spots'], fields['Total Property Parking Spots'],
                fields['Visitor Parking'], fields['Street Parking']].filter(Boolean).join(' || ')
    };
  }

  // window.units / window.listing are set by the page's own inline script. Since v2.0 the script
  // runs with @grant (for the image request), which sandboxes `window`, so the page's own window
  // is reached through unsafeWindow.
  var PAGE = (typeof unsafeWindow !== 'undefined' && unsafeWindow) ? unsafeWindow : window;
  function readPageGlobals() {
    var out = {};
    try {
      if (Array.isArray(PAGE.units)) out.units_js = JSON.parse(JSON.stringify(PAGE.units.slice(0, 200)));
    } catch (e) {}
    try {
      if (PAGE.listing && typeof PAGE.listing === 'object') {
        var j = JSON.stringify(PAGE.listing);
        out.listing_js = j.length <= 8000 ? JSON.parse(j) : { _truncated: true, _keys: Object.keys(PAGE.listing) };
      }
    } catch (e) {}
    return out;
  }

  // --- hero image (v2.0) -----------------------------------------------------
  var IMG_HOST = 'https://rf-images-prod-bcdn.rentfaster.ca/';
  var IMG = { maxWidth: 720, quality: 0.72, perFile: 150 };
  function imagesOn() {
    try { return JSON.parse(localStorage.getItem('__rfdCfg') || '{}').images !== false; } catch (e) { return true; }
  }
  function idb() {
    return new Promise(function (res, rej) {
      var r = indexedDB.open('rfd_images', 1);
      r.onupgradeneeded = function () { r.result.createObjectStore('images', { keyPath: 'url' }); };
      r.onsuccess = function () { res(r.result); };
      r.onerror = function () { rej(r.error); };
    });
  }
  function idbReq(db, mode, fn) {
    return new Promise(function (res, rej) {
      var tx = db.transaction('images', mode), st = tx.objectStore('images'), out = fn(st);
      tx.oncomplete = function () { res(out && 'result' in out ? out.result : undefined); };
      tx.onerror = function () { rej(tx.error); };
    });
  }
  function heroUrl() {
    var u = null;
    readLinkedData().some(function (p) {
      var im = p.mainEntity && p.mainEntity.image;
      if (Array.isArray(im)) im = im[0];
      if (im && typeof im === 'object') im = im.url || im.contentUrl;
      if (typeof im === 'string' && im) { u = im; return true; }
      return false;
    });
    if (!u && PAGE.listing && typeof PAGE.listing.slide === 'string') u = PAGE.listing.slide;
    u = u ? u.split('?')[0] : null;
    return u && u.indexOf(IMG_HOST) === 0 ? u : null;
  }
  // First choice: the copy the page already shows. Reading it back fails if the image server sends
  // no CORS header (the canvas is "tainted"), in which case the Tampermonkey request is used.
  function blobFromPage(url) {
    var file = url.split('/').pop().replace(/^slide_/, '');
    var img = Array.prototype.filter.call(document.images, function (i) {
      return (i.currentSrc || i.src || '').indexOf(file) !== -1 && i.complete && i.naturalWidth;
    })[0];
    if (!img) return Promise.resolve(null);
    return new Promise(function (res) {
      try {
        var c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight;
        c.getContext('2d').drawImage(img, 0, 0);
        c.toBlob(function (b) { res(b); }, 'image/png');
      } catch (e) { res(null); }  // SecurityError: tainted canvas
    });
  }
  function blobViaGM(url) {
    return new Promise(function (res, rej) {
      if (typeof GM_xmlhttpRequest !== 'function') { rej(new Error('GM_xmlhttpRequest unavailable')); return; }
      GM_xmlhttpRequest({ method: 'GET', url: url, responseType: 'blob', timeout: 20000,
        onload: function (r) { if (r.status === 200 && r.response) res(r.response); else rej(new Error('HTTP ' + r.status)); },
        onerror: function () { rej(new Error('request failed')); },
        ontimeout: function () { rej(new Error('timeout')); } });
    });
  }
  function downscale(blob) {
    return createImageBitmap(blob).then(function (bmp) {
      var w = Math.min(IMG.maxWidth, bmp.width), h = Math.round(bmp.height * w / bmp.width);
      var c = document.createElement('canvas'); c.width = w; c.height = h;
      c.getContext('2d').drawImage(bmp, 0, 0, w, h);
      return new Promise(function (res) {
        c.toBlob(function (b) {
          var fr = new FileReader();
          fr.onload = function () { res({ b64: String(fr.result).split(',')[1], w: w, h: h, bytes: b.size }); };
          fr.readAsDataURL(b);
        }, 'image/jpeg', IMG.quality);
      });
    });
  }
  function saveHeroImage(id) {
    if (!imagesOn() || !id) return Promise.resolve('off');
    var url = heroUrl();
    if (!url) return Promise.resolve('no image');
    return idb().then(function (db) {
      return idbReq(db, 'readonly', function (st) { return st.get(url); }).then(function (have) {
        if (have) {  // same photo already held (or already downloaded): just note this listing uses it
          if ((have.listing_ids || []).indexOf(id) === -1) {
            have.listing_ids = (have.listing_ids || []).concat([id]);
            return idbReq(db, 'readwrite', function (st) { return st.put(have); }).then(function () { return 'held'; });
          }
          return 'held';
        }
        return blobFromPage(url).then(function (b) { return b || blobViaGM(url); }).then(downscale).then(function (img) {
          var rec = { url: url, listing_id: id, listing_ids: [id], ts: new Date().toISOString(),
                      w: img.w, h: img.h, bytes: img.bytes, jpeg_b64: img.b64, exported: false };
          return idbReq(db, 'readwrite', function (st) { return st.put(rec); }).then(function () { return 'saved'; });
        });
      });
    }).catch(function (e) {
      (window.__rfdErrors = window.__rfdErrors || []).push('image: ' + (e && e.message));
      return 'error';
    });
  }
  function pendingImages() {
    if (!imagesOn()) return Promise.resolve([]);
    return idb().then(function (db) {
      return idbReq(db, 'readonly', function (st) { return st.getAll(); });
    }).then(function (all) { return (all || []).filter(function (r) { return !r.exported && r.jpeg_b64; }); })
      .catch(function () { return []; });
  }
  function downloadImages() {
    return pendingImages().then(function (todo) {
      if (!todo.length) return 0;
      var stamp = new Date().toISOString().replace(/[:.]/g, '-');
      for (var k = 0; k * IMG.perFile < todo.length; k++) {
        var part = todo.slice(k * IMG.perFile, (k + 1) * IMG.perFile);
        saveFile(JSON.stringify({ kind: 'rentfaster_images', exported_at: new Date().toISOString(), images: part.map(function (r) {
          return { listing_id: r.listing_id, listing_ids: r.listing_ids, url: r.url, ts: r.ts, w: r.w, h: r.h, jpeg_b64: r.jpeg_b64 };
        }) }), 'rf_images_' + stamp + '_' + (k + 1) + '.json');
      }
      // Keep the URL so the same photo is never fetched or downloaded again; drop the picture.
      return idb().then(function (db) {
        return idbReq(db, 'readwrite', function (st) {
          todo.forEach(function (r) { delete r.jpeg_b64; r.exported = true; st.put(r); });
        });
      }).then(function () { return todo.length; });
    });
  }
  function saveFile(text, name) {
    var url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
    var a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  // Run a reader without letting an exception abort the whole capture.
  function safely(fn, label) {
    try { return fn(); }
    catch (e) {
      (window.__rfdErrors = window.__rfdErrors || []).push(label + ': ' + (e && e.message));
      return null;
    }
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
      if (existing.every(function (c) { return c.promotions !== undefined && c.building !== undefined; })) return 0;
      list = list.filter(function (c) { return c.listing_id !== id; });
    }

    var payloads = safely(readLinkedData, 'ld+json') || [];
    var floor = safely(readFloorPlans, 'floorplans');
    var hasFloor = !!(floor && floor.units && floor.units.length);
    if (!payloads.length && !hasFloor) return 0;

    var promotions = safely(readPromotions, 'promotions');
    var building = safely(readBuilding, 'building');
    var globals = safely(readPageGlobals, 'globals') || {};
    var errs = window.__rfdErrors ? window.__rfdErrors.slice(-3) : null;
    var now = new Date().toISOString();
    if (payloads.length) {
      payloads.forEach(function (p) {
        var rec = { ts: now, url: location.href, listing_id: id, source: 'userscript:ld+json',
                    promotions: promotions, data: p };
        if (hasFloor) rec.floorplans = floor;
        rec.building = building;
        if (globals.units_js) rec.units_js = globals.units_js;
        if (globals.listing_js) rec.listing_js = globals.listing_js;
        if (errs) rec.capture_errors = errs;
        list.push(rec);
      });
    } else {
      // No usable JSON-LD, but the page has its Floor Plans block: keep that.
      var rec2 = { ts: now, url: location.href, listing_id: id, source: 'userscript:dom',
                   promotions: promotions, data: null, floorplans: floor, building: building };
      if (globals.units_js) rec2.units_js = globals.units_js;
      if (globals.listing_js) rec2.listing_js = globals.listing_js;
      if (errs) rec2.capture_errors = errs;
      list.push(rec2);
    }
    if (!save(list)) return 0; // write refused: do not report a capture that was not stored
    return payloads.length || 1;
  }

  function summary() {
    var list = load();
    var suites = 0, withSqft = 0, withPromo = 0;
    list.forEach(function (c) {
      var places = (c.data && c.data.mainEntity && c.data.mainEntity.containsPlace) || [];
      if (!places.length && c.floorplans && c.floorplans.units) places = c.floorplans.units.map(function (u) {
        return { floorSize: u.parsed && u.parsed.sqft ? { value: u.parsed.sqft } : null };
      });
      suites += places.length;
      places.forEach(function (p) { if (p.floorSize && p.floorSize.value) withSqft++; });
      if (c.promotions && c.promotions.promos && c.promotions.promos.length) withPromo++;
    });
    return { listings: list.length, suites: suites, withSqft: withSqft, withPromo: withPromo };
  }

  function download() {
    allCaptures().then(function (all) {
      var list = all.map(function (c) { var r = Object.assign({}, c); delete r._k; delete r._dirty; return r; });
      if (list.length) {
        saveFile(JSON.stringify({ kind: 'rentfaster_detail', captured_at: new Date().toISOString(), captures: list }),
                 'rf_detail_' + new Date().toISOString().replace(/[:.]/g, '-') + '.json');
      }
      // Images go in their own files so the capture JSON stays small (v2.0).
      return downloadImages().then(function (n) {
        if (!list.length && !n) alert('Nothing captured yet.');
        refreshBadgeImages();
      });
    });
  }
  function refreshBadgeImages() {
    pendingImages().then(function (p) {
      var el = document.getElementById('rf-detail-badge');
      if (!el) return;
      el.textContent = el.textContent.replace(/ · \d+ img/, '').replace(/ ⬇$/, '') + (p.length ? ' · ' + p.length + ' img' : '') + ' ⬇';
    });
  }

  function badge() {
    if (document.getElementById('rf-detail-badge')) return;
    var s = summary();
    var full = window.__rfdStorageFull === true;
    var el = document.createElement('div');
    el.id = 'rf-detail-badge';
    el.style.cssText = [
      'position:fixed', 'bottom:14px', 'right:14px', 'z-index:2147483647',
      (full ? 'background:#8A5220' : 'background:#1F4E6B'), 'color:#fff', 'font:12px/1.4 system-ui,sans-serif',
      'padding:7px 11px', 'border-radius:5px', 'cursor:pointer',
      'box-shadow:0 2px 10px rgba(0,0,0,.28)', 'user-select:none', 'opacity:.93'
    ].join(';');
    el.title = full
      ? 'STORAGE FULL - nothing new is being saved. Download, then shift-click to clear.'
      : 'Click to download captures. Shift-click to clear.';
    el.textContent = full
      ? '⚠ STORAGE FULL - download now (' + s.listings + ' held) ⬇'
      : s.listings + ' listings · ' + s.suites + ' suites · ' + s.withPromo + ' promos ⬇';
    // Nudge to download well before the quota bites.
    if (!full && s.listings >= 150 && s.listings % 50 === 0) {
      el.textContent = '↓ ' + el.textContent + '  (worth downloading)';
    }
    el.addEventListener('click', function (e) {
      if (e.shiftKey) {
        if (confirm('Clear all captured listings?')) {
          clearCaptures().then(function () { el.textContent = 'cleared'; window.__rfdStorageFull = false; });
        }
        return;
      }
      download();
    });
    document.body.appendChild(el);
  }

  // ===========================================================================
  // QUEUE MODE (added v1.5, 2026-11-12) -- OPT-IN, does nothing until you use it
  // ===========================================================================
  // Walks a prioritised list of listing URLs (detail_queue_<date>.csv from
  // src/build_detail_queue.py) in THIS tab, one page at a time, saving each page
  // with the same capture() used when you browse by hand. This is the only part
  // of the script that opens pages itself; it exists so a monthly detail pass is
  // not hundreds of manual clicks.
  //
  // Pacing and safety, all adjustable in CFG below:
  //   * waits a random 30-90s between pages, with an occasional 2-4 minute rest
  //   * daily cap (default 150 pages) and a per-session page limit you set on Start
  //   * stops at once on anything that looks like a challenge / block page
  //     (CAPTCHA, "verify you are human", HTTP 403/429, "too many requests") and
  //     never tries to get around it
  //   * stops after 5 pages in a row that return no data (markup change, block)
  //   * stops if storage is full, if you navigate away from the queue, or on Pause
  //   * only ever navigates to https://www.rentfaster.ca/properties/... URLs
  //   * skips listings you already hold a current capture for
  // The tab must stay open and visible and the computer awake; Chrome slows
  // hidden tabs. Pacing can be tuned without editing the file: set localStorage
  // key __rfdCfg to JSON such as {"dailyCap":100} in the page's console.
  var QKEY = '__rfdQueue', QSTATE = '__rfdQueueState', QLOG = '__rfdQueueLog', QCFG = '__rfdCfg';
  var CFG = {
    minDelayMs: 30000, maxDelayMs: 90000,
    longPauseChance: 0.05, longPauseMinMs: 120000, longPauseMaxMs: 240000,
    dailyCap: 150, maxNoDataStreak: 5, settleMs: 2500, firstDelayMinMs: 5000, firstDelayMaxMs: 15000
  };
  try {
    var ov = JSON.parse(localStorage.getItem(QCFG) || '{}');
    Object.keys(ov).forEach(function (k) { if (k in CFG) CFG[k] = ov[k]; });
  } catch (e) { /* defaults */ }

  var SAFE_PREFIX = 'https://www.rentfaster.ca/properties/';
  var navTimer = null, tickTimer = null;

  // The queue state lives in localStorage, which every Rentfaster tab shares, and this script
  // runs in every Rentfaster tab. Without a rule, a second tab you open (a results page, a
  // listing) reads the shared state and acts on it: on 2026-11-23 a map page opened in another
  // tab logged an `unexpected_redirect` and stopped a running queue. So one tab owns the queue
  // (the one where Start was pressed, remembered in sessionStorage, which is per tab) and every
  // other tab leaves it alone.
  function tabId() {
    try {
      var id = sessionStorage.getItem('__rfdTab');
      if (!id) { id = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8); sessionStorage.setItem('__rfdTab', id); }
      return id;
    } catch (e) { return 'no-session-storage'; }
  }
  function ownsQueue(st) { return !st.ownerTab || st.ownerTab === tabId(); }

  function jget(key, dflt) {
    try { var v = JSON.parse(localStorage.getItem(key)); return v == null ? dflt : v; } catch (e) { return dflt; }
  }
  function jset(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); return true; } catch (e) { return false; } }
  function today() { var d = new Date(); return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2); }
  function rand(a, b) { return a + Math.random() * (b - a); }

  function qItems() { return jget(QKEY, []); }
  function qState() {
    var st = jget(QSTATE, null) || { running: false, pos: 0, ranToday: 0, day: today(), sessionLeft: 0, streak: 0, reason: '', nextAt: 0, lastId: '', navigatingTo: '', lastGone: false };
    if (st.day !== today()) { st.day = today(); st.ranToday = 0; }
    return st;
  }
  var QPOS = '__rfdQueuePositions';
  function qSave(st) {
    jset(QSTATE, st);
    if (st.qkey) {  // remember this queue's place so loading another queue never loses it
      var m = jget(QPOS, {});
      m[st.qkey] = { pos: st.pos, name: st.qname || '', n: qItems().length, savedAt: new Date().toISOString() };
      jset(QPOS, m);
    }
  }
  // Keyed on the queue's CONTENT (length, first and last id), not the file name, so a renamed copy
  // resumes too, and a queue loaded under v1.8 (which recorded no key) is parked correctly the
  // first time another queue is loaded.
  function queueKey(items) {
    return [items.length, items[0].id, items[items.length - 1].id].join('|');
  }
  function qLog(entry) {
    var log = jget(QLOG, []);
    entry.ts = new Date().toISOString();
    log.push(entry);
    jset(QLOG, log.slice(-5000));
  }

  // Minimal CSV parser (handles quoted fields with commas and doubled quotes).
  function parseCsv(text) {
    var rows = [], row = [], f = '', q = false, i = 0;
    for (; i < text.length; i++) {
      var c = text[i];
      if (q) {
        if (c === '"') { if (text[i + 1] === '"') { f += '"'; i++; } else q = false; }
        else f += c;
      } else if (c === '"') q = true;
      else if (c === ',') { row.push(f); f = ''; }
      else if (c === '\n' || c === '\r') {
        if (c === '\r' && text[i + 1] === '\n') i++;
        row.push(f); f = '';
        if (row.length > 1 || row[0] !== '') rows.push(row);
        row = [];
      } else f += c;
    }
    if (f !== '' || row.length) { row.push(f); rows.push(row); }
    return rows;
  }

  function holdsCurrent(id) {
    return load().some(function (c) { return c.listing_id === id && c.promotions !== undefined && c.building !== undefined; });
  }
  function nextIndex(from, q) {
    var i = from;
    while (i < q.length && holdsCurrent(q[i].id)) {
      qLog({ listing_id: q[i].id, status: 'skipped_held', priority: q[i].priority, url: q[i].url });
      i++;
    }
    return i;
  }

  // Returns WHICH check fired ('' when none), so a challenge row in the log explains itself.
  function challengeSignal() {
    var t = (document.title || '').toLowerCase(), m;
    m = t.match(/just a moment|attention required|verify you are human|are you a robot|captcha|access denied|forbidden|too many requests|rate limit/);
    if (m) return 'title "' + m[0] + '"';
    var el = document.querySelector('iframe[src*="captcha"],iframe[src*="challenge"],.cf-turnstile,#challenge-form,#challenge-running,.g-recaptcha,.h-captcha');
    if (el) return 'element ' + (el.id ? '#' + el.id : el.className ? '.' + String(el.className).split(' ')[0] : el.tagName.toLowerCase() + '[src~=' + ((el.getAttribute('src') || '').match(/captcha|challenge/) || [''])[0] + ']');
    var b = ((document.body && document.body.innerText) || '').slice(0, 600).toLowerCase();
    m = b.match(/verify you are human|unusual traffic|are you a robot|access denied|too many requests/);
    if (m) return 'page text "' + m[0] + '"';
    var code = httpStatus();
    if (code === 403 || code === 429) return 'HTTP ' + code;
    return '';
  }
  function looksLikeChallenge() { return !!challengeSignal(); }
  function httpStatus() {
    try { var n = performance.getEntriesByType('navigation')[0]; return n && n.responseStatus ? n.responseStatus : 0; } catch (e) { return 0; }
  }
  function looksGone() {
    if (httpStatus() === 404) return true;
    var t = (document.title || '').toLowerCase();
    var h = document.querySelector('h1'); var ht = h ? (h.textContent || '').toLowerCase() : '';
    return /not found|no longer available|page unavailable|404/.test(t + ' ' + ht);
  }


  // Why did a page that is not a removed listing give nothing? Recorded in the queue
  // log so the next failure explains itself.
  function diagnoseNoData() {
    var bits = [];
    try {
      var tags = document.querySelectorAll('script[type="application/ld+json"]'), ok = 0, main = 0, bad = 0, empty = 0;
      for (var i = 0; i < tags.length; i++) {
        var s = (tags[i].textContent || '').trim();
        if (!s) { empty++; continue; }
        try { var o = JSON.parse(s); ok++; if (o && o.mainEntity) main++; } catch (e) { bad++; }
      }
      bits.push('ld+json tags=' + tags.length + ' empty=' + empty + ' parsed=' + ok + ' withMainEntity=' + main + ' unparsable=' + bad);
      bits.push('floorplan units=' + document.querySelectorAll('[data-unitname]').length);
      bits.push('readyState=' + document.readyState);
      bits.push('bodyChars=' + ((document.body && document.body.innerText) || '').length);
      bits.push('title=' + (document.title || '').slice(0, 60));
      if (window.__rfdErrors && window.__rfdErrors.length) bits.push('errors=' + window.__rfdErrors.slice(-2).join(' / '));
    } catch (e) { bits.push('diagnose failed: ' + (e && e.message)); }
    return bits.join('; ');
  }

  function qStop(reason, st) {
    st = st || qState();
    st.running = false; st.reason = reason; st.nextAt = 0;
    qSave(st);
    if (navTimer) { clearTimeout(navTimer); navTimer = null; }
    console.warn('[rf-detail] queue stopped: ' + reason);
    renderPanel();
  }

  function scheduleNext(st, q, first) {
    var delay = first ? rand(CFG.firstDelayMinMs, CFG.firstDelayMaxMs)
      : (Math.random() < CFG.longPauseChance ? rand(CFG.longPauseMinMs, CFG.longPauseMaxMs) : rand(CFG.minDelayMs, CFG.maxDelayMs));
    st.nextAt = Date.now() + delay;
    qSave(st);
    armNavigation(st, q);
  }
  function armNavigation(st, q) {
    if (navTimer) clearTimeout(navTimer);
    var target = q[st.pos];
    if (!target) return;
    navTimer = setTimeout(function () {
      var cur = qState();
      if (!cur.running) return;
      if (typeof target.url !== 'string' || target.url.indexOf(SAFE_PREFIX) !== 0) {
        qLog({ listing_id: target.id, status: 'bad_url', priority: target.priority, url: target.url });
        cur.pos++; qSave(cur); armNavigation(cur, q); return;
      }
      // Do not leave the page until this page's capture is written.
      PENDING.then(function (ok) {
        var now = qState();
        if (!now.running) return;
        if (ok === false || window.__rfdStorageFull) { qStop('storage write failed - download captures, then shift-click the badge to clear', now); return; }
        // Remember what we are navigating to, so the next page load can tell a
        // redirect (gone / sibling listing) from you wandering off the queue.
        now.navigatingTo = target.id;
        qSave(now);
        location.href = target.url;
      });
    }, Math.max(0, st.nextAt - Date.now()));
    renderPanel();
  }

  // A redirected capture is stored under the id of the page we actually landed on;
  // record which queued listing sent us there.
  function annotateRedirect(newId, fromId) {
    var list = load(), changed = false;
    list.forEach(function (c) {
      if (c.listing_id === newId && !c.redirected_from) { c.redirected_from = fromId; c._dirty = true; changed = true; }
    });
    if (changed) save(list);
  }

  // Record the outcome for the queued item, apply the stop rules, schedule the next page.
  function finishItem(st, q, expected, status, landedId, note) {
    if (status === 'captured' || status === 'captured_dom' || status === 'redirected') st.streak = 0;
    else if (status === 'no_data' || status === 'redirected_no_data') st.streak++;
    qLog({ listing_id: expected.id, landed_id: landedId || '', status: status, priority: expected.priority, url: location.href, note: note || '' });

    st.ranToday++; st.sessionLeft--;
    st.lastId = landedId || ''; st.navigatingTo = ''; st.lastGone = (status === 'gone');
    st.pos = nextIndex(st.pos + 1, q);
    qSave(st);

    if (window.__rfdStorageFull) { qStop('storage full - download captures, then shift-click the badge to clear', st); return; }
    if (st.streak >= CFG.maxNoDataStreak) { qStop(CFG.maxNoDataStreak + ' pages in a row returned no data - page layout changed or access limited', st); return; }
    if (st.ranToday >= CFG.dailyCap) { qStop('daily cap of ' + CFG.dailyCap + ' pages reached - resume tomorrow', st); return; }
    if (st.sessionLeft <= 0) { qStop('session page limit reached', st); return; }
    if (st.pos >= q.length) { qStop('queue finished', st); return; }
    scheduleNext(st, q, false);
  }

  // Runs on every page load the script matches. Only acts when the page is one
  // the queue expects, or the redirect target of the page it just navigated to.
  function queueStep() {
    var st = qState(), q = qItems();
    if (!st.running || !q.length) { renderPanel(); return; }
    if (!ownsQueue(st)) { renderPanel(); return; }   // the queue runs in another tab
    var id = listingId();
    var onListing = /\/properties\//.test(location.pathname);
    var expected = q[st.pos];

    if (!expected) { qStop('queue finished', st); return; }
    var sig = challengeSignal();
    if (sig) {
      qLog({ listing_id: id || expected.id, landed_id: id, status: 'challenge', priority: expected.priority, url: location.href,
             note: 'signal=' + sig + '; title=' + (document.title || '').slice(0, 60) + '; http=' + (httpStatus() || '?')
                   + '; bodyChars=' + ((document.body && document.body.innerText) || '').length });
      st.lastChallengeAt = Date.now();
      qStop('challenge or block page detected - queue stopped, nothing was bypassed', st);
      return;
    }

    // Results page. Rentfaster sends a removed listing here, with HTTP 200 and
    // #listingNotFound on the address, so it cannot be caught as a 404.
    if (!onListing) {
      if (st.navigatingTo === expected.id) {
        // Also read the URL the navigation actually loaded, in case the page rewrites its own address.
        var navUrl = '';
        try { navUrl = (performance.getEntriesByType('navigation')[0] || {}).name || ''; } catch (e) {}
        if (/listingNotFound/i.test(location.hash + location.search + navUrl)) { finishItem(st, q, expected, 'gone', ''); return; }
        qLog({ listing_id: expected.id, landed_id: '', status: 'unexpected_redirect', priority: expected.priority, url: location.href, note: 'landed on ' + location.pathname.slice(0, 60) });
        qStop('redirected to an unexpected page - queue stopped', st);
        return;
      }
      if (st.lastGone && st.nextAt) { armNavigation(st, q); return; } // reloaded the results page after a gone listing
      qStop('you left the queue (now on a non-listing page) - paused', st);
      return;
    }

    if (id !== expected.id) {
      // Redirected to a different listing (for example a sibling at the same
      // address). Keep that page's capture under its own id and move on.
      if (st.navigatingTo === expected.id && id) {
        var got = holdsCurrent(id);
        if (got) annotateRedirect(id, expected.id);
        finishItem(st, q, expected, got ? 'redirected' : 'redirected_no_data', id);
        return;
      }
      if (id && id === st.lastId && st.nextAt) { armNavigation(st, q); return; } // reloaded the page we just finished
      qStop('you left the queue (page ' + (id || location.pathname) + ' is not the expected listing) - paused', st);
      return;
    }

    var status, note = '';
    if (!holdsCurrent(id) && !looksGone() && !window.__rfdRetried) {
      // Nothing stored yet and the page is not a removed-listing page: look once more
      // after a pause before calling it no_data (slow render, late JSON-LD).
      window.__rfdRetried = true;
      capture();
      setTimeout(queueStep, 4000);
      return;
    }
    if (!holdsCurrent(id) && window.__rfdRetried) capture();   // second look, after the retry pause
    if (holdsCurrent(id)) {
      var rec = load().filter(function (c) { return c.listing_id === id; }).pop();
      status = (rec && rec.source === 'userscript:dom') ? 'captured_dom' : 'captured';
      if (status === 'captured_dom') note = 'no usable JSON-LD; Floor Plans block kept';
      else if (window.__rfdRetried) note = 'captured on retry';
    } else if (looksGone()) status = 'gone';
    else { status = 'no_data'; note = diagnoseNoData(); }
    PENDING.then(function (ok) {
      if (ok === false && (status === 'captured' || status === 'captured_dom')) { status = 'store_failed'; note = 'capture read but the write to storage failed'; }
      finishItem(qState(), q, expected, status, id, note);
    });
  }

  function loadQueueFile() {
    var inp = document.createElement('input');
    inp.type = 'file'; inp.accept = '.csv,text/csv';
    inp.addEventListener('change', function () {
      var f = inp.files && inp.files[0]; if (!f) return;
      if (qState().running) { alert('Pause the running queue before loading another one. Its position is saved and comes back when you load that file again.'); return; }
      var rd = new FileReader();
      rd.onload = function () {
        var rows = parseCsv(String(rd.result || ''));
        if (rows.length < 2) { alert('Queue file looks empty.'); return; }
        var h = rows[0].map(function (x) { return x.trim().toLowerCase(); });
        var ci = h.indexOf('listing_id'), cu = h.indexOf('url'), cp = h.indexOf('priority');
        if (ci < 0 || cu < 0) { alert('Queue CSV needs listing_id and url columns.'); return; }
        var items = [], bad = 0;
        rows.slice(1).forEach(function (r) {
          var url = (r[cu] || '').trim().split('?')[0], id = (r[ci] || '').trim();
          if (!id || url.indexOf(SAFE_PREFIX) !== 0) { bad++; return; }
          items.push({ id: id, url: url, priority: cp >= 0 ? r[cp] : '' });
        });
        if (!items.length) { alert('No valid https://www.rentfaster.ca/properties/ URLs in that file.'); return; }
        var prev = qState(), old = qItems();
        if (!prev.qkey && old.length) { prev.qkey = queueKey(old); prev.qname = prev.qname || 'previous queue'; }
        if (prev.qkey) qSave(prev);  // park the outgoing queue's position
        var key = queueKey(items), saved = jget(QPOS, {})[key];
        var pos = saved && saved.pos > 0 && saved.pos < items.length ? saved.pos : 0;
        jset(QKEY, items);
        qSave({ running: false, pos: pos, ranToday: prev.ranToday, day: today(), sessionLeft: 0, streak: 0,
                reason: (pos ? 'resumed at ' + (pos + 1) + ' of ' + items.length : 'queue loaded (' + items.length + ' pages') + (bad ? ', ' + bad + ' skipped' : '') + (pos ? '' : ')'),
                nextAt: 0, lastId: '', navigatingTo: '', lastGone: false, qkey: key, qname: f.name });
        renderPanel();
      };
      rd.readAsText(f);
    });
    inp.click();
  }

  function startQueue() {
    var q = qItems();
    if (!q.length) { alert('Load a queue CSV first.'); return; }
    var st = qState();
    var left = Math.max(0, CFG.dailyCap - st.ranToday);
    if (!left) { alert('Daily cap of ' + CFG.dailyCap + ' pages already used today.'); return; }
    if (st.lastChallengeAt && Date.now() - st.lastChallengeAt < 12 * 3600 * 1000) {
      var ago = Math.round((Date.now() - st.lastChallengeAt) / 60000);
      if (!confirm('The queue stopped on a challenge ' + ago + ' minutes ago. Recommended: stop for the day. Start anyway?')) return;
    }
    var ans = prompt('How many pages this session? (daily cap remaining: ' + left + ', queue left: ' + Math.max(0, q.length - st.pos) + ')', String(Math.min(left, 60)));
    if (ans === null) return;
    var n = parseInt(ans, 10);
    if (!(n > 0)) return;
    st.sessionLeft = Math.min(n, left); st.streak = 0; st.running = true; st.reason = ''; st.ownerTab = tabId();
    st.pos = nextIndex(st.pos, q);
    if (st.pos >= q.length) { qStop('queue finished', st); return; }
    qSave(st);
    if (listingId() === q[st.pos].id) { setTimeout(queueStep, 300); }
    else scheduleNext(st, q, true);
    renderPanel();
  }
  function pauseQueue() { qStop('paused by you', qState()); }
  function gotoPosition() {
    var q = qItems(), st = qState();
    if (st.running) { alert('Pause first.'); return; }
    var ans = prompt('Start from which position? (1 to ' + q.length + ', now at ' + (st.pos + 1) + ')', String(st.pos + 1));
    if (ans === null) return;
    var n = parseInt(ans, 10);
    if (!(n >= 1 && n <= q.length)) { alert('Enter a number from 1 to ' + q.length + '.'); return; }
    st.pos = n - 1; st.reason = 'set to position ' + n; qSave(st); renderPanel();
  }
  function clearQueue() {
    if (!confirm('Remove the loaded queue and its progress? (Captured listings are kept.)')) return;
    if (navTimer) clearTimeout(navTimer);
    localStorage.removeItem(QKEY); localStorage.removeItem(QSTATE);
    renderPanel();
  }
  function downloadQueueLog() {
    var log = jget(QLOG, []);
    if (!log.length) { alert('Queue log is empty.'); return; }
    var csv = 'ts,listing_id,landed_id,status,priority,url,note\n' + log.map(function (r) {
      return [r.ts, r.listing_id, r.landed_id, r.status, r.priority, r.url, r.note].map(function (x) { return '"' + String(x == null ? '' : x).replace(/"/g, '""') + '"'; }).join(',');
    }).join('\n');
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = 'rf_queue_log_' + new Date().toISOString().replace(/[:.]/g, '-') + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
  }

  function renderPanel() {
    var el = document.getElementById('rf-queue-panel');
    if (!el) {
      el = document.createElement('div');
      el.id = 'rf-queue-panel';
      el.style.cssText = 'position:fixed;bottom:54px;right:14px;z-index:2147483647;background:#243b2f;color:#fff;font:12px/1.5 system-ui,sans-serif;padding:8px 11px;border-radius:5px;box-shadow:0 2px 10px rgba(0,0,0,.28);opacity:.95;max-width:300px';
      document.body.appendChild(el);
    }
    var q = qItems(), st = qState();
    var mins = st.nextAt ? Math.max(0, Math.round((st.nextAt - Date.now()) / 1000)) : 0;
    var line = q.length
      ? 'Queue ' + Math.min(st.pos + (st.running ? 1 : 0), q.length) + '/' + q.length + ' · today ' + st.ranToday + '/' + CFG.dailyCap
      : 'No queue loaded';
    var status = st.running ? (!ownsQueue(st) ? 'running in another tab (Pause here to take over)' : (mins ? 'next page in ' + mins + 's' : 'working…')) : (st.reason || 'idle');
    el.style.background = (!st.running && /challenge|storage|no data/.test(st.reason || '')) ? '#8A2B20' : '#243b2f';
    el.innerHTML = '';
    var t = document.createElement('div'); t.textContent = line; el.appendChild(t);
    if (q.length && st.qname) { var nm = document.createElement('div'); nm.textContent = st.qname; nm.style.cssText = 'opacity:.7;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'; el.appendChild(nm); }
    var s = document.createElement('div'); s.id = 'rf-q-status'; s.textContent = status; s.style.opacity = '.85'; el.appendChild(s);
    var bar = document.createElement('div'); bar.style.marginTop = '5px';
    function btn(label, fn) {
      var b = document.createElement('button'); b.textContent = label;
      b.style.cssText = 'margin-right:5px;font:11px system-ui;padding:2px 7px;cursor:pointer';
      b.addEventListener('click', fn); bar.appendChild(b);
    }
    btn('Load queue', loadQueueFile);
    if (q.length) btn(st.running ? 'Pause' : 'Start', st.running ? pauseQueue : startQueue);
    if (q.length && !st.running) btn('Go to #', gotoPosition);
    if (q.length) btn('Log', downloadQueueLog);
    if (q.length) btn('Clear', clearQueue);
    el.appendChild(bar);
    if (tickTimer) clearInterval(tickTimer);
    if (st.running && st.nextAt && ownsQueue(st)) tickTimer = setInterval(function () {
      var m = Math.max(0, Math.round((qState().nextAt - Date.now()) / 1000));
      var d = document.getElementById('rf-q-status'); if (d && qState().running) d.textContent = 'next page in ' + m + 's';
    }, 1000);
  }

  function queueInit() {
    renderPanel();
    // Wait past the late-JSON-LD retry below before judging the page.
    setTimeout(queueStep, CFG.settleMs);
  }

  var onListingPage = /\/properties\//.test(location.pathname);
  storeInit().then(function () {
  if (onListingPage) {
    var added = capture();
    badge();
    saveHeroImage(listingId()).then(refreshBadgeImages);
    queueInit();
    if (added) console.log('[rf-detail] captured listing ' + listingId() + '. Total: ' + summary().listings);

    // Some pages inject the JSON-LD slightly late.
    if (!added) setTimeout(function () {
      if (capture()) {
        var el = document.getElementById('rf-detail-badge');
        if (el) { var s = summary(); el.textContent = s.listings + ' listings · ' + s.suites + ' suites · ' + s.withPromo + ' promos ⬇'; }
      }
    }, 1800);
  } else if (qState().running) {
    // Results page reached during a queue run (removed listing): no capture or
    // badge here, only the queue's own handling.
    queueInit();
  }
  });
})();
