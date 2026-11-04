"""
Month-over-month comparison + trend chart for Rentfaster snapshots.

Separate from rentfaster_ingest.py on purpose (Decision Log 2026-08-04): ingest
extracts and exports, this reads what ingest wrote.

Usage (Colab or local):
  from snapshot_compare import compare, supply_series, trend_chart

  # Compare two snapshots. Each source is a CSV path or DataFrame containing a
  # snapshot_date column: snapshots.csv (full history, preferred) or a
  # listings_master.csv (rows whose snapshot_date matches are that listing's
  # LAST appearance, which equals the full snapshot only for the newest one).
  res = compare("rf_data/snapshots.csv", "rf_data/snapshots.csv",
                old_date="2026-10-02", new_date="2026-11-04", out_dir="rf_data/compare")

  # Exact supply/promo/rent history from the full snapshots store.
  trend_chart("rf_data/snapshots.parquet", "rf_data/charts/trends.png")

Notes:
  * Rent changes use a CLEAN COHORT: listing present in both snapshots, rent
    confidence direct/certain in both, same beds_lo. Blended/inferred ranges
    move for reasons unrelated to rent (a suite type listing or delisting).
  * Asking rents, not achieved rents. price_lo is the lower end of the range.
  * Use snapshots.parquet/.csv, not listings_master: master keeps only each
    listing's latest row, so older months' rents/promos are not recoverable from it.
"""
from pathlib import Path

import numpy as np
import pandas as pd

CLEAN_TIERS = ["direct", "certain"]
PROMO_CODES = ["promo_rent_special", "promo_other", "promo_move_in_gift"]


def _df(source):
    if isinstance(source, pd.DataFrame):
        return source.copy()
    if str(source).endswith(".parquet"):
        return pd.read_parquet(source)
    return pd.read_csv(source, dtype={"snapshot_date": str})


def load_snapshot(source, date=None):
    df = _df(source)
    df["snapshot_date"] = df["snapshot_date"].astype(str)
    date = date or df["snapshot_date"].max()
    out = df[df["snapshot_date"] == date].drop_duplicates("listing_id").copy()
    out["mid"] = out[["price_lo", "price_hi"]].mean(axis=1)
    return out


def _tally(g, col="dlo"):
    return pd.Series({
        "n": len(g), "up": int((g[col] > 0).sum()), "down": int((g[col] < 0).sum()),
        "mean_pct": round(g["pct"].mean(), 4)})


def compare(old_source, new_source, old_date=None, new_date=None, out_dir=None,
            min_group=8, verbose=True):
    o = load_snapshot(old_source, old_date)
    n = load_snapshot(new_source, new_date)
    io, i_n = set(o.listing_id), set(n.listing_id)
    p = o.merge(n, on="listing_id", suffixes=("_o", "_n"))
    p["dlo"] = p.price_lo_n - p.price_lo_o
    for c in ["has_promo"] + PROMO_CODES:
        p[c + "_o"] = p[c + "_o"].fillna(False).astype(bool)
        p[c + "_n"] = p[c + "_n"].fillna(False).astype(bool)

    clean = p[p.rent_confidence_o.isin(CLEAN_TIERS) & p.rent_confidence_n.isin(CLEAN_TIERS)
              & (p.beds_lo_o == p.beds_lo_n) & p.dlo.notna()].copy()
    clean["pct"] = clean.dlo / clean.price_lo_o
    clean["promo_state"] = np.select(
        [clean.has_promo_o & clean.has_promo_n, clean.has_promo_o & ~clean.has_promo_n,
         ~clean.has_promo_o & clean.has_promo_n], ["kept", "dropped", "added"], "none")

    def med(df):
        d = df[df.rent_confidence.isin(CLEAN_TIERS) & df.price_lo.notna()]
        return d.groupby("beds_lo").price_lo.median()

    res = {
        "dates": (o.snapshot_date.iloc[0], n.snapshot_date.iloc[0]),
        "supply": {"old": len(o), "new": len(n), "carried": len(io & i_n),
                   "gone": len(io - i_n), "arrived": len(i_n - io)},
        "clean_cohort": _tally(clean).to_dict(),
        "median_rent": pd.concat([med(o).rename("old"), med(n).rename("new")], axis=1),
        "by_beds": clean.groupby("beds_lo_o").apply(_tally, include_groups=False),
        "by_city": clean.groupby("city_n").apply(_tally, include_groups=False),
        "by_community": clean.groupby("community_n").apply(_tally, include_groups=False),
        "by_poster": clean.groupby("user_id_n").apply(_tally, include_groups=False),
        "promo_share": pd.DataFrame({
            "old": o[["has_promo"] + PROMO_CODES].fillna(False).astype(bool).mean(),
            "new": n[["has_promo"] + PROMO_CODES].fillna(False).astype(bool).mean()}),
        "promo_flips": pd.DataFrame({
            c: {"kept": int((p[c + "_o"] & p[c + "_n"]).sum()),
                "dropped": int((p[c + "_o"] & ~p[c + "_n"]).sum()),
                "added": int((~p[c + "_o"] & p[c + "_n"]).sum())}
            for c in ["has_promo"] + PROMO_CODES}).T,
        "promo_vs_rent": clean.groupby("promo_state").apply(_tally, include_groups=False),
        "cohort": clean,
    }
    for k in ("by_city", "by_community", "by_poster"):
        t = res[k]; res[k] = t[t.n >= min_group].sort_values("mean_pct")
    # listings that left vs arrived: asking rent and promo composition
    comp = {}
    for name, d in [("gone", o[~o.listing_id.isin(i_n)]), ("arrived", n[~n.listing_id.isin(io)]),
                    ("carried", n[n.listing_id.isin(io)])]:
        d = d[d.rent_confidence.isin(CLEAN_TIERS)]
        comp[name] = {"n": len(d), "promo_share": round(d.has_promo.fillna(False).astype(bool).mean(), 3),
                      **{f"med_{b}br": d[d.beds_lo == b].price_lo.median() for b in (1, 2, 3)}}
    res["composition"] = pd.DataFrame(comp).T

    if verbose:
        pd.set_option("display.width", 200)
        print(f"Compare {res['dates'][0]} -> {res['dates'][1]}")
        print("supply", res["supply"]); print("clean cohort", res["clean_cohort"])
        for k in ("median_rent", "by_beds", "promo_share", "promo_flips", "promo_vs_rent",
                  "composition"):
            print(f"\n{k}\n{res[k].round(3)}")
        for k in ("by_city", "by_community", "by_poster"):
            print(f"\n{k} (n>={min_group}, sorted by mean % change)\n{pd.concat([res[k].head(6), res[k].tail(4)]).drop_duplicates()}")
    if out_dir:
        out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
        tag = f"{res['dates'][0]}_to_{res['dates'][1]}"
        clean.to_csv(out / f"rent_changes_clean_cohort_{tag}.csv", index=False)
        for k in ("by_beds", "by_city", "by_community", "by_poster", "promo_flips",
                  "promo_vs_rent", "composition", "median_rent", "promo_share"):
            res[k].to_csv(out / f"{k}_{tag}.csv")
    return res


def supply_series(snapshots_source):
    """Exact per-snapshot supply from the full snapshots store: active listings
    (property rows), suite types, new (id not in the prior snapshot, so returning
    ids count as new), dropped (prior ids absent now) and churn %."""
    df = _df(snapshots_source)
    df["snapshot_date"] = df["snapshot_date"].astype(str)
    rows, prev_ids = [], None
    for d, g in sorted(df.groupby("snapshot_date")):
        ids = set(g.listing_id)
        r = {"snapshot_date": d, "active": len(ids),
             "suite_types": int(g.n_suite_types.fillna(1).sum()),
             "new": len(ids - prev_ids) if prev_ids is not None else np.nan,
             "dropped_since_prev": len(prev_ids - ids) if prev_ids is not None else np.nan}
        r["churn_pct"] = round(r["dropped_since_prev"] / len(prev_ids), 3) if prev_ids else np.nan
        rows.append(r); prev_ids = ids
    return pd.DataFrame(rows)


def snapshot_stats(source):
    """Per-snapshot promo share and median asking rent by beds. Needs FULL snapshots
    (snapshots.csv); on a master file only the newest date is meaningful."""
    df = _df(source)
    rows = []
    for d, g in df.groupby("snapshot_date"):
        r = {"snapshot_date": d, "n": len(g)}
        for c in ["has_promo"] + PROMO_CODES:
            r[c] = g[c].fillna(False).astype(bool).mean()
        gc = g[g.rent_confidence.isin(CLEAN_TIERS) & g.price_lo.notna()]
        for b in (1, 2, 3):
            r[f"med_{b}br"] = gc[gc.beds_lo == b].price_lo.median()
        rows.append(r)
    return pd.DataFrame(rows)


def trend_chart(snapshots_source, out_png):
    """4-panel small-multiples PNG, one y-axis per panel (no dual axes).

    snapshots_source: snapshots.parquet/.csv path or DataFrame (full history).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"   # validated slots 1-3
    INK, MUTED, GRID, SURF = "#0b0b0b", "#52514e", "#e3e2de", "#fcfcfb"
    sup = supply_series(snapshots_source).dropna(subset=['new'])
    sup0 = supply_series(snapshots_source)
    st = snapshot_stats(snapshots_source)
    xs = lambda ds: [pd.to_datetime(d) for d in ds]

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), facecolor=SURF)
    for ax in axes.flat:
        ax.set_facecolor(SURF)
        for s in ("top", "right"): ax.spines[s].set_visible(False)
        for s in ("left", "bottom"): ax.spines[s].set_color(GRID)
        ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
        ax.tick_params(colors=MUTED, labelsize=9, length=0)
        ax.margins(x=0.12)

    def title(ax, t, sub):
        ax.set_title(t, loc="left", fontsize=11, color=INK, fontweight="bold", pad=22)
        ax.text(0, 1.03, sub, transform=ax.transAxes, fontsize=8.5, color=MUTED)

    # A: active listings
    ax = axes[0, 0]; sup = sup0; x = xs(sup.snapshot_date)
    ax.plot(x, sup.active, color=BLUE, lw=2, marker="o", ms=7, mec=SURF, mew=2)
    for xi, v in zip(x, sup.active): ax.annotate(f"{v:,}", (xi, v), (0, 9), textcoords="offset points", ha="center", fontsize=9, color=INK)
    ax.set_ylim(sup.active.min() * 0.9, sup.active.max() * 1.08)
    title(ax, "Active listings (properties)", "Rows per full snapshot")

    # B: new vs dropped
    ax = axes[0, 1]; s2 = sup0.dropna(subset=["dropped_since_prev"]); x2 = xs(s2.snapshot_date)
    ax.plot(x2, s2.new, color=AQUA, lw=2, marker="o", ms=7, mec=SURF, mew=2)
    ax.plot(x2, s2.dropped_since_prev, color=ORANGE, lw=2, marker="o", ms=7, mec=SURF, mew=2)
    for xi, a, b in zip(x2, s2.new, s2.dropped_since_prev):
        up_a = a >= b  # label the higher series above, the lower one below
        ax.annotate(f"{int(a)}", (xi, a), (0, 9 if up_a else -15), textcoords="offset points", ha="center", fontsize=9, color=INK)
        ax.annotate(f"{int(b)}", (xi, b), (0, -15 if up_a else 9), textcoords="offset points", ha="center", fontsize=9, color=INK)
    ax.text(x2[-1], s2.new.iloc[-1], "  new", color=INK, fontsize=9, va="center")
    ax.text(x2[-1], s2.dropped_since_prev.iloc[-1], "  dropped", color=INK, fontsize=9, va="center")
    ax.set_ylim(0, max(s2.new.max(), s2.dropped_since_prev.max()) * 1.2)
    title(ax, "New vs dropped listings per month", "New = id not in prior snapshot (includes returning ids)")

    # C: promo share
    ax = axes[1, 0]; x3 = xs(st.snapshot_date)
    for col, colr, lab in [("has_promo", BLUE, "any promo"), ("promo_rent_special", ORANGE, "rent special"),
                           ("promo_other", AQUA, "other promo")]:
        ax.plot(x3, st[col] * 100, color=colr, lw=2, marker="o", ms=7, mec=SURF, mew=2)
        ax.text(x3[-1], st[col].iloc[-1] * 100, f"  {lab} {st[col].iloc[-1]*100:.1f}%", color=INK, fontsize=9, va="center")
        ax.text(x3[0], st[col].iloc[0] * 100, f"{st[col].iloc[0]*100:.1f}%  ", color=INK, fontsize=9, va="center", ha="right")
    ax.set_ylim(0, 32); ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.margins(x=0.35)
    title(ax, "Share of listings with an incentive", "Full snapshots only")

    # D: median asking rent
    ax = axes[1, 1]
    for col, colr, lab in [("med_1br", BLUE, "1 bed"), ("med_2br", ORANGE, "2 bed"), ("med_3br", AQUA, "3 bed")]:
        ax.plot(x3, st[col], color=colr, lw=2, marker="o", ms=7, mec=SURF, mew=2)
        ax.text(x3[-1], st[col].iloc[-1], f"  {lab} ${st[col].iloc[-1]:,.0f}", color=INK, fontsize=9, va="center")
        ax.text(x3[0], st[col].iloc[0], f"${st[col].iloc[0]:,.0f}  ", color=INK, fontsize=9, va="center", ha="right")
    ax.margins(x=0.35); ax.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    title(ax, "Median asking rent (direct/certain listings)", "Lower end of asking range, single-suite-type listings")

    for ax in axes[1]: ax.set_xticks(x3); ax.set_xticklabels([d.strftime("%b %d") for d in x3])
    for ax in axes[0]: ax.set_xticks(xs(sup0.snapshot_date)); ax.set_xticklabels([d.strftime("%b %d") for d in xs(sup0.snapshot_date)])
    fig.suptitle("Rentfaster market trends: Edmonton metro", x=0.06, ha="left", fontsize=14,
                 fontweight="bold", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160, facecolor=SURF); plt.close(fig)
    return sup, st
