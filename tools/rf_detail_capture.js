/*
 * Rentfaster listing-detail capture helper  (companion to rf_console_capture.js)
 * =============================================================================
 * Paste into the DevTools console while browsing Rentfaster listing pages.
 * Like the map capture, it NEVER fetches anything itself -- it only watches
 * what your browser already loaded as you click through listings normally,
 * and keeps a copy locally until you download it.
 *
 * WHY THIS IS SEPARATE FROM rf_console_capture.js
 * -----------------------------------------------
 * Two differences make the map hook unsuitable here:
 *
 *   1. The map hook only recognizes a `listings` array + `search` object.
 *      The detail-page schema is UNDOCUMENTED (open question 5 in CLAUDE.md),
 *      so this script cannot match a known shape. It runs in DISCOVERY mode:
 *      it keeps anything plausibly listing-related and reports the shape back
 *      so the schema can be written down before an extractor is built.
 *
 *   2. The detail data may never travel over fetch/XHR at all. If the page is
 *      server-rendered, the payload arrives inside the HTML document as an
 *      inline <script> JSON blob, which a network hook cannot see. This script
 *      scans the already-loaded DOM for those too. Reading the DOM your browser
 *      has already parsed is not a request.
 *
 * Also: clicking into a listing is a navigation, and a full page load wipes
 * everything in memory. Captures are mirrored to localStorage so they survive
 * moving between listings. Re-paste on a new page only if the console says the
 * hooks are not installed.
 *
 * USAGE
 * -----
 *   1. Open any Rentfaster listing page. Paste this whole file, hit enter.
 *   2. Browse listings normally -- ideally the ones in your match review queue.
 *      Each page you open is captured automatically.
 *   3. rfdStatus()      what has been captured so far
 *      rfdInspect(1)    print the SHAPE of capture #1 (keys, not values) --
 *                       this is what documents the schema
 *      rfdDownload()    download everything as one file
 *      rfdClear()       wipe and start over
 *
 * WHAT IT KEEPS
 * -------------
 * During discovery it is deliberately permissive: any same-origin JSON
 * response over a small size threshold, plus any inline JSON blob in the page.
 * That will include some irrelevant payloads. Better to over-collect once and
 * narrow the filter after the schema is known than to miss the real thing
 * because the guess was wrong.
 *
 * Nothing leaves your browser. localStorage is local to your machine.
 */
(function () {
  var STORE = '__rfDetailCaptures';
  var MAX = 400;               // stop growing without bound
  var MIN_BYTES = 200;         // ignore trivial responses (pings, tokens)

  if (!window.__rfdNativeFetch) window.__rfdNativeFetch = window.fetch;
  if (!window.__rfdNativeXHROpen) window.__rfdNativeXHROpen = XMLHttpRequest.prototype.open;
  if (!window.__rfdNativeXHRSend) window.__rfdNativeXHRSend = XMLHttpRequest.prototype.send;

  // Restore first so a re-paste swaps hooks cleanly instead of double-wrapping.
  window.fetch = window.__rfdNativeFetch;
  XMLHttpRequest.prototype.open = window.__rfdNativeXHROpen;
  XMLHttpRequest.prototype.send = window.__rfdNativeXHRSend;

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || '[]'); }
    catch (e) { return []; }
  }
  function save(list) {
    try { localStorage.setItem(STORE, JSON.stringify(list)); }
    catch (e) { console.warn('[rf-detail] localStorage full -- captures kept in memory only.'); }
  }

  var captures = load();
  var wasInstalled = window.__rfdInstalled === true;
  window.__rfdInstalled = true;

  function listingIdFromUrl() {
    var m = location.pathname.match(/-(\d+)\/?$/);   // /properties/<slug>-<id>
    return m ? m[1] : null;
  }

  // Shape summary rather than a schema assumption: record which keys exist so
  // the payload can be identified after the fact.
  function describe(obj, depth) {
    depth = depth || 0;
    if (obj === null || obj === undefined) return 'null';
    if (Array.isArray(obj)) {
      return obj.length === 0 ? '[]'
        : '[' + obj.length + ' x ' + (depth < 2 ? describe(obj[0], depth + 1) : typeof obj[0]) + ']';
    }
    if (typeof obj === 'object') {
      if (depth >= 2) return '{' + Object.keys(obj).length + ' keys}';
      var out = {};
      Object.keys(obj).slice(0, 60).forEach(function (k) { out[k] = describe(obj[k], depth + 1); });
      return out;
    }
    return typeof obj;
  }

  function interesting(obj) {
    if (!obj || typeof obj !== 'object') return false;
    var flat = JSON.stringify(obj);
    if (flat.length < MIN_BYTES) return false;
    var id = listingIdFromUrl();
    if (id && flat.indexOf(id) !== -1) return true;
    // listing-ish vocabulary, without committing to a schema
    return /"(ref_id|listing_id|floorplan|floorPlans|suites?|units?|beds|baths|price|availability|amenities|building)"/i.test(flat);
  }

  function record(obj, source) {
    if (!interesting(obj)) return;
    var flat = JSON.stringify(obj);
    if (captures.some(function (c) { return c.bytes === flat.length && c.source === source; })) return;
    if (captures.length >= MAX) { console.warn('[rf-detail] capture cap reached -- download and clear.'); return; }
    captures.push({
      ts: new Date().toISOString(),
      url: location.href,
      listing_id: listingIdFromUrl(),
      source: source,
      bytes: flat.length,
      data: obj
    });
    save(captures);
    console.log('[rf-detail] #' + captures.length + ' kept from ' + source +
                ' (' + flat.length + ' bytes, listing ' + (listingIdFromUrl() || '?') + ')');
  }

  // --- network hooks -------------------------------------------------------
  var origFetch = window.__rfdNativeFetch;
  window.fetch = function () {
    var args = arguments;
    return origFetch.apply(this, args).then(function (res) {
      // Never let anything here reject the promise the page depends on.
      try {
        res.clone().json()
          .then(function (obj) { record(obj, 'fetch:' + (args[0] && args[0].url ? args[0].url : args[0])); })
          .catch(function () {});
      } catch (e) { /* not clonable or not JSON */ }
      return res;
    });
  };

  var origOpen = window.__rfdNativeXHROpen;
  var origSend = window.__rfdNativeXHRSend;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__rfdUrl = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    var self = this;
    this.addEventListener('load', function () {
      try { record(JSON.parse(self.responseText), 'xhr:' + self.__rfdUrl); }
      catch (e) { /* not JSON */ }
    });
    return origSend.apply(this, arguments);
  };

  // --- inline JSON in the already-loaded document --------------------------
  // Covers the server-rendered case, where the payload never crosses fetch/XHR.
  // This reads the DOM the browser already parsed; it issues no request.
  function scanInline() {
    var found = 0;
    document.querySelectorAll('script').forEach(function (tag) {
      var text = (tag.textContent || '').trim();
      if (text.length < MIN_BYTES) return;
      var type = (tag.getAttribute('type') || '').toLowerCase();
      var candidates = [];
      if (type.indexOf('json') !== -1) {
        candidates.push(text);
      } else {
        // window.__X = {...};  /  var X = {...};
        var m = text.match(/=\s*(\{[\s\S]*\})\s*;?\s*$/);
        if (m) candidates.push(m[1]);
      }
      candidates.forEach(function (raw) {
        try {
          var obj = JSON.parse(raw);
          var before = captures.length;
          record(obj, 'inline:' + (tag.id || type || 'script'));
          if (captures.length > before) found++;
        } catch (e) { /* not JSON */ }
      });
    });
    // Some apps stash state on window directly.
    ['__NEXT_DATA__', '__INITIAL_STATE__', '__NUXT__', '__APOLLO_STATE__'].forEach(function (key) {
      if (window[key]) {
        var before = captures.length;
        record(window[key], 'window.' + key);
        if (captures.length > before) found++;
      }
    });
    return found;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scanInline);
  } else {
    scanInline();
  }
  // Re-scan shortly after, for state written by scripts that run late.
  setTimeout(scanInline, 1500);

  // --- console API ---------------------------------------------------------
  window.rfdStatus = function () {
    console.log('[rf-detail] ' + captures.length + ' captures across ' +
      new Set(captures.map(function (c) { return c.listing_id; })).size + ' listing ids.');
    captures.forEach(function (c, i) {
      console.log('  #' + (i + 1) + ' [' + c.ts + '] listing=' + (c.listing_id || '?') +
                  ' ' + c.bytes + ' bytes  ' + c.source.slice(0, 70));
    });
    return { captures: captures.length };
  };

  window.rfdInspect = function (n) {
    var c = captures[(n || 1) - 1];
    if (!c) { console.log('[rf-detail] no capture #' + n); return; }
    console.log('[rf-detail] shape of capture #' + (n || 1) + ' (' + c.source + '):');
    console.log(JSON.stringify(describe(c.data), null, 2));
    return c.data;
  };

  window.rfdDownload = function () {
    if (!captures.length) { console.log('[rf-detail] nothing captured yet -- open a listing page.'); return; }
    var payload = { kind: 'rentfaster_detail_discovery', captured_at: new Date().toISOString(), captures: captures };
    var blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'rf_detail_' + new Date().toISOString().replace(/[:.]/g, '-') + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    console.log('[rf-detail] downloaded ' + captures.length + ' captures.');
  };

  window.rfdClear = function () {
    captures = [];
    save(captures);
    console.log('[rf-detail] cleared.');
  };

  console.log(wasInstalled
    ? '[rf-detail] hooks refreshed. Kept ' + captures.length + ' captures.'
    : '[rf-detail] installed (discovery mode). Captures survive navigation via localStorage.');
  console.log('  Browse listings normally. Then:');
  console.log('  rfdStatus()    -> what has been captured');
  console.log('  rfdInspect(1)  -> print the SHAPE of capture #1 (this documents the schema)');
  console.log('  rfdDownload()  -> download everything as one file');
  console.log('  rfdClear()     -> wipe and start over');
  if (!captures.length) console.log('  (nothing captured yet on this page -- try opening a listing)');
})();
