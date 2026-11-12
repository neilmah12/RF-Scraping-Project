# How to capture listing details

Plain-English reminder. Nothing here needs the console.

---

## One-time setup (you only ever do this once)

1. Install the **Tampermonkey** extension in Chrome.
2. Click the Tampermonkey icon → **Dashboard**.
3. Click the **+** tab (Create a new script).
4. Delete whatever is in the box.
5. Paste in the whole of `tools/rf_detail_capture.user.js`.
   It must start with the line `// ==UserScript==` — if it doesn't, you have
   the wrong file.
6. **File → Save** (or Ctrl+S).

That's it. Setup is done forever.

---

## Every time you work (the actual routine)

**You do NOT turn Tampermonkey on. It runs by itself.**

1. Open the match review workbook in Excel.
2. Click a **Listing** link in a row. The RentFaster page opens in Chrome.
3. A small blue box appears in the **bottom-right corner**:

       3 listings · 5 suites (4 with sq ft) ⬇

   That means it worked. The page is captured. You don't click anything.

4. Look at the page and the photo. Decide if it's the same building as the
   Inventory row.
5. Go back to Excel. Put **Y**, **N** or **?** in the Verdict column.
6. Next row. Repeat.

The number in the blue box goes up as you go. That is the only feedback you
need.

---

## At the end of your session

1. **Click the blue box.** A `.json` file downloads.
2. Send that file over.

Do this before you close the browser for the day. It is quick and there is no
harm in doing it several times — each download contains everything so far, so
downloading twice just gives you two copies of the same thing.

---

## Things worth knowing

**Opening a listing twice does nothing.** Already-captured listings are
skipped, so click around freely.

**The count is remembered between days.** It lives in the browser, so it
survives closing Chrome. It does not survive clearing browsing data — so
download before you ever clear cookies or site data.

**Shift-clicking the blue box wipes everything.** It asks first. Don't do it
by accident.

**No blue box?** In order of likelihood:
- You are not on a listing page. It only appears on `rentfaster.ca/properties/...`
- Tampermonkey is switched off for the site — click its icon and check.
- The script didn't save. Reopen the Dashboard and confirm it is listed and
  enabled.

**It never fetches anything.** It only reads pages you opened yourself. There
is no background activity, nothing runs when you aren't clicking, and no page
is loaded that you didn't ask for.

---

## What this is collecting, and why

Each listing page carries structured data the map capture doesn't have:

- **Rent per suite type** — "1 bed $1,199", "2 bed $1,379" — instead of just a
  range for the whole building
- **Square footage per suite type** — the biggest missing piece for proformas
- Whether **utilities are included** (so you know if a rent is net or gross)
- The **property manager** and the **building name**
- Postal code and structured amenities

Aim for 15–20 listings before sending the first batch. That's enough to know
how often each field is actually filled in.

---

## The other script

There is also `tools/rf_detail_capture.js`. That one is for the **console**,
and needs re-pasting on every page. Ignore it for normal work — it exists for
one-offs and for re-running discovery if RentFaster changes its page layout.
