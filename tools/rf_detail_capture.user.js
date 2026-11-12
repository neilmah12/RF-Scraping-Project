// ==UserScript==
// @name         Rentfaster detail capture
// @namespace    rf-scraping-project
// @version      1.5
// @description  Keeps the schema.org listing data your browser already loaded while you browse Rentfaster listings. Optional queue mode (off until you load a queue and press Start) opens the next listing in this tab on a paced timer.
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
 * Passive mode (default): it does NOT change what happens on the network. You
 * open every page yourself; this only reads the document your browser already
 * parsed and keeps a copy locally. Nothing is fetched or sent anywhere.
 *
 * Queue mode (v1.5, opt-in): after you load a queue CSV and press Start, it
 * opens the listed pages in this tab one at a time on a paced timer. See the
 * QUEUE MODE section below for pacing, caps and the conditions that stop it.
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

  var STORE = '__rfDetailCaptures';
  var MAX = 2000;

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || '[]'); }
    catch (e) { return []; }
  }
  // Captures run ~14KB each, so a 5-10MB localStorage quota fills somewhere
  // around 350-750 listings. Before v1.4 a quota failure was swallowed: the
  // write threw, capture() ignored the result, and the badge kept counting as
  // if nothing was wrong -- so a long browsing session would silently stop
  // persisting. Failure is now sticky and shown.
  function save(list) {
    try {
      localStorage.setItem(STORE, JSON.stringify(list));
      window.__rfdStorageFull = false;
      return true;
    } catch (e) {
      window.__rfdStorageFull = true;
      console.warn('[rf-detail] STORAGE FULL -- click the badge to download, then shift-click to clear. '
                   + 'Nothing new is being saved until you do.');
      return false;
    }
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
    if (!save(list)) return 0;   // quota hit: do not report a capture that was not stored
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
    if (!full && s.listings >= 250 && s.listings % 50 === 0) {
      el.textContent = '↓ ' + el.textContent + '  (worth downloading)';
    }
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

  function jget(key, dflt) {
    try { var v = JSON.parse(localStorage.getItem(key)); return v == null ? dflt : v; } catch (e) { return dflt; }
  }
  function jset(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); return true; } catch (e) { return false; } }
  function today() { var d = new Date(); return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2); }
  function rand(a, b) { return a + Math.random() * (b - a); }

  function qItems() { return jget(QKEY, []); }
  function qState() {
    var st = jget(QSTATE, null) || { running: false, pos: 0, ranToday: 0, day: today(), sessionLeft: 0, streak: 0, reason: '', nextAt: 0, lastId: '' };
    if (st.day !== today()) { st.day = today(); st.ranToday = 0; }
    return st;
  }
  function qSave(st) { jset(QSTATE, st); }
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
    return load().some(function (c) { return c.listing_id === id && c.promotions !== undefined; });
  }
  function nextIndex(from, q) {
    var i = from;
    while (i < q.length && holdsCurrent(q[i].id)) {
      qLog({ listing_id: q[i].id, status: 'skipped_held', priority: q[i].priority, url: q[i].url });
      i++;
    }
    return i;
  }

  function looksLikeChallenge() {
    var t = (document.title || '').toLowerCase();
    if (/just a moment|attention required|verify you are human|are you a robot|captcha|access denied|forbidden|too many requests|rate limit/.test(t)) return true;
    if (document.querySelector('iframe[src*="captcha"],iframe[src*="challenge"],.cf-turnstile,#challenge-form,#challenge-running,.g-recaptcha,.h-captcha')) return true;
    var b = ((document.body && document.body.innerText) || '').slice(0, 600).toLowerCase();
    return /verify you are human|unusual traffic|are you a robot|access denied|too many requests/.test(b);
  }
  function httpStatus() {
    try { var n = performance.getEntriesByType('navigation')[0]; return n && n.responseStatus ? n.responseStatus : 0; } catch (e) { return 0; }
  }
  function looksGone() {
    if (httpStatus() === 404) return true;
    var t = (document.title || '').toLowerCase();
    var h = document.querySelector('h1'); var ht = h ? (h.textContent || '').toLowerCase() : '';
    return /not found|no longer available|page unavailable|404/.test(t + ' ' + ht);
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
      location.href = target.url;
    }, Math.max(0, st.nextAt - Date.now()));
    renderPanel();
  }

  // Runs on every page load. Only acts when the page is the one the queue expects.
  function queueStep() {
    var st = qState(), q = qItems();
    if (!st.running || !q.length) { renderPanel(); return; }
    var id = listingId();
    var expected = q[st.pos];

    if (!expected) { qStop('queue finished', st); return; }
    if (looksLikeChallenge() || [403, 429].indexOf(httpStatus()) !== -1) {
      qLog({ listing_id: id, status: 'challenge', priority: expected.priority, url: location.href });
      qStop('challenge or block page detected - queue stopped, nothing was bypassed', st);
      return;
    }
    if (id !== expected.id) {
      if (id && id === st.lastId && st.nextAt) { armNavigation(st, q); return; }  // reloaded the page we just finished
      qStop('you left the queue (page ' + (id || location.pathname) + ' is not the expected listing) - paused', st);
      return;
    }

    var held = holdsCurrent(id);
    var status;
    if (held) { status = 'captured'; st.streak = 0; }
    else if (looksGone()) status = 'gone';
    else { status = 'no_data'; st.streak++; }
    qLog({ listing_id: id, status: status, priority: expected.priority, url: location.href });

    st.ranToday++; st.sessionLeft--; st.lastId = id;
    st.pos = nextIndex(st.pos + 1, q);
    qSave(st);

    if (window.__rfdStorageFull) { qStop('storage full - download captures, then shift-click the badge to clear', st); return; }
    if (st.streak >= CFG.maxNoDataStreak) { qStop(CFG.maxNoDataStreak + ' pages in a row returned no data - page layout changed or access limited', st); return; }
    if (st.ranToday >= CFG.dailyCap) { qStop('daily cap of ' + CFG.dailyCap + ' pages reached - resume tomorrow', st); return; }
    if (st.sessionLeft <= 0) { qStop('session page limit reached', st); return; }
    if (st.pos >= q.length) { qStop('queue finished', st); return; }
    scheduleNext(st, q, false);
  }

  function loadQueueFile() {
    var inp = document.createElement('input');
    inp.type = 'file'; inp.accept = '.csv,text/csv';
    inp.addEventListener('change', function () {
      var f = inp.files && inp.files[0]; if (!f) return;
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
        jset(QKEY, items);
        qSave({ running: false, pos: 0, ranToday: qState().ranToday, day: today(), sessionLeft: 0, streak: 0, reason: 'queue loaded (' + items.length + ' pages' + (bad ? ', ' + bad + ' skipped' : '') + ')', nextAt: 0, lastId: '' });
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
    var ans = prompt('How many pages this session? (daily cap remaining: ' + left + ', queue left: ' + Math.max(0, q.length - st.pos) + ')', String(Math.min(left, 60)));
    if (ans === null) return;
    var n = parseInt(ans, 10);
    if (!(n > 0)) return;
    st.sessionLeft = Math.min(n, left); st.streak = 0; st.running = true; st.reason = '';
    st.pos = nextIndex(st.pos, q);
    if (st.pos >= q.length) { qStop('queue finished', st); return; }
    qSave(st);
    if (listingId() === q[st.pos].id) { setTimeout(queueStep, 300); }
    else scheduleNext(st, q, true);
    renderPanel();
  }
  function pauseQueue() { qStop('paused by you', qState()); }
  function clearQueue() {
    if (!confirm('Remove the loaded queue and its progress? (Captured listings are kept.)')) return;
    if (navTimer) clearTimeout(navTimer);
    localStorage.removeItem(QKEY); localStorage.removeItem(QSTATE);
    renderPanel();
  }
  function downloadQueueLog() {
    var log = jget(QLOG, []);
    if (!log.length) { alert('Queue log is empty.'); return; }
    var csv = 'ts,listing_id,status,priority,url\n' + log.map(function (r) {
      return [r.ts, r.listing_id, r.status, r.priority, r.url].map(function (x) { return '"' + String(x == null ? '' : x).replace(/"/g, '""') + '"'; }).join(',');
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
    var status = st.running ? (mins ? 'next page in ' + mins + 's' : 'working…') : (st.reason || 'idle');
    el.style.background = (!st.running && /challenge|storage|no data/.test(st.reason || '')) ? '#8A2B20' : '#243b2f';
    el.innerHTML = '';
    var t = document.createElement('div'); t.textContent = line; el.appendChild(t);
    var s = document.createElement('div'); s.textContent = status; s.style.opacity = '.85'; el.appendChild(s);
    var bar = document.createElement('div'); bar.style.marginTop = '5px';
    function btn(label, fn) {
      var b = document.createElement('button'); b.textContent = label;
      b.style.cssText = 'margin-right:5px;font:11px system-ui;padding:2px 7px;cursor:pointer';
      b.addEventListener('click', fn); bar.appendChild(b);
    }
    btn('Load queue', loadQueueFile);
    if (q.length) btn(st.running ? 'Pause' : 'Start', st.running ? pauseQueue : startQueue);
    if (q.length) btn('Log', downloadQueueLog);
    if (q.length) btn('Clear', clearQueue);
    el.appendChild(bar);
    if (tickTimer) clearInterval(tickTimer);
    if (st.running && st.nextAt) tickTimer = setInterval(function () {
      var m = Math.max(0, Math.round((qState().nextAt - Date.now()) / 1000));
      var d = el.children[1]; if (d && qState().running) d.textContent = 'next page in ' + m + 's';
    }, 1000);
  }

  function queueInit() {
    renderPanel();
    // Wait past the late-JSON-LD retry below before judging the page.
    setTimeout(queueStep, CFG.settleMs);
  }

  var added = capture();
  badge();
  queueInit();
  if (added) console.log('[rf-detail] captured listing ' + listingId() + '. Total: ' + summary().listings);

  // Some pages inject the JSON-LD slightly late.
  if (!added) setTimeout(function () {
    if (capture()) {
      var el = document.getElementById('rf-detail-badge');
      if (el) { var s = summary(); el.textContent = s.listings + ' listings · ' + s.suites + ' suites · ' + s.withPromo + ' promos ⬇'; }
    }
  }, 1800);
})();
