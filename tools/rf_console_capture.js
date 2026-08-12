/*
 * Rentfaster console capture helper
 * ==================================
 * Paste this into the browser DevTools console on the Rentfaster map page,
 * once per capture session. It does NOT fetch anything on its own -- it only
 * watches the requests your browser already makes as you pan/zoom the map
 * manually, and keeps a copy of every map.json-shaped response in memory.
 * Nothing is sent anywhere; everything stays local to the page until you
 * explicitly download it.
 *
 * Recognizes a response as a capture by its shape (a `listings` array plus a
 * `search` object), not by URL, so it works whether the page uses fetch or
 * XMLHttpRequest and doesn't need the exact endpoint hardcoded.
 *
 * Usage:
 *   1. Open the Rentfaster map, set your filters (Apartment + Townhouse +
 *      Fourplex, no condo), paste this whole file into the console, hit enter.
 *   2. Pan/zoom the map around the city. Every matching response gets logged
 *      and stored automatically -- no Network tab, no manual "Save Response As".
 *   3. Run `rfStatus()` any time to see what's been captured so far.
 *   4. Run `rfDownload()` when done. It downloads ONE file containing every
 *      capture from this session (`rf_captures_<timestamp>.json`). Send that
 *      one file over -- check_capture.py and the ingest pipeline understand
 *      the combined format and QC/dedupe each capture inside it individually.
 *   5. Run `rfClear()` to wipe memory and start a fresh session without
 *      reloading the page.
 *
 * Captures live only in this tab's memory -- a page reload wipes them, so
 * download before refreshing or closing the tab.
 */
(function () {
  if (window.__rfCaptureInstalled) {
    console.log('[rf-capture] already installed. Captures so far:', window.__rfCaptures.length);
    return;
  }
  window.__rfCaptures = [];
  window.__rfCaptureInstalled = true;

  function looksLikeMapPayload(obj) {
    return obj && typeof obj === 'object' &&
      Array.isArray(obj.listings) && obj.search && typeof obj.search === 'object';
  }

  function record(obj, source) {
    if (!looksLikeMapPayload(obj)) return;
    const seenIds = new Set(window.__rfCaptures.flatMap(c => c.data.listings.map(l => l.id)));
    const newIds = obj.listings.filter(l => !seenIds.has(l.id)).length;
    window.__rfCaptures.push({ ts: new Date().toISOString(), source, data: obj });
    console.log(
      `[rf-capture] #${window.__rfCaptures.length} captured: ${obj.listings.length} listings ` +
      `(total=${obj.total}, total2=${obj.total2}, ${newIds} new ids, types=${(obj.search.type || []).join('+')})`
    );
  }

  // fetch hook
  const origFetch = window.fetch;
  window.fetch = function (...args) {
    return origFetch.apply(this, args).then(res => {
      res.clone().json().then(obj => record(obj, 'fetch:' + (args[0] && args[0].url ? args[0].url : args[0]))).catch(() => {});
      return res;
    });
  };

  // XHR hook (in case the map uses XMLHttpRequest instead of fetch)
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__rfUrl = url;
    return origOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener('load', () => {
      try {
        record(JSON.parse(this.responseText), 'xhr:' + this.__rfUrl);
      } catch (e) { /* not JSON, ignore */ }
    });
    return origSend.apply(this, args);
  };

  window.rfStatus = function () {
    const allIds = new Set(window.__rfCaptures.flatMap(c => c.data.listings.map(l => l.id)));
    console.log(`[rf-capture] ${window.__rfCaptures.length} captures, ${allIds.size} unique listings total this session.`);
    window.__rfCaptures.forEach((c, i) => {
      console.log(
        `  #${i + 1} [${c.ts}] ${c.data.listings.length} listings, total=${c.data.total}, ` +
        `total2=${c.data.total2}, area=${c.data.search.area || '(none)'}`
      );
    });
    return { captures: window.__rfCaptures.length, uniqueListings: allIds.size };
  };

  window.rfDownload = function () {
    if (!window.__rfCaptures.length) {
      console.log('[rf-capture] nothing captured yet -- pan the map first.');
      return;
    }
    const payload = { captures: window.__rfCaptures.map(c => c.data) };
    const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    a.href = url;
    a.download = `rf_captures_${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    console.log(`[rf-capture] downloaded ${window.__rfCaptures.length} captures as one file.`);
  };

  window.rfClear = function () {
    window.__rfCaptures = [];
    console.log('[rf-capture] cleared. Session continues -- pan to capture more.');
  };

  console.log('[rf-capture] installed. Pan/zoom the map normally -- every map.json response is captured automatically.');
  console.log('  rfStatus()   -> see what has been captured so far');
  console.log('  rfDownload() -> download everything as one combined .json file');
  console.log('  rfClear()    -> wipe captures and start over (same page, no reload needed)');
})();
