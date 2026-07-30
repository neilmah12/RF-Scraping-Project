# Rentfaster Market Intelligence + Proforma Engine

Edmonton multifamily market rent tracking, listing-to-building matching, and
signals pipeline built on top of Rentfaster manual map.json captures.

See `CLAUDE.md` for full project context, decision log, and build status.

## Running in Colab

Open `notebooks/rentfaster_ingest.ipynb` in Google Colab and follow the cells
in order. It clones this repo (or lets you upload `rentfaster_ingest.py`
directly), lets you upload your monthly `map.json` capture(s), and runs the
ingest into `rf_data/`.

## Running locally

```bash
pip install pandas pyarrow
python src/rentfaster_ingest.py map_nw.json map_ne.json map_sw.json
```

## Repo structure

```
CLAUDE.md                  project context, decision log
src/
  rentfaster_ingest.py      step 1: map.json -> snapshots + listings_master
notebooks/
  rentfaster_ingest.ipynb   Colab wrapper for step 1
data/                       gitignored (raw captures + pipeline outputs)
```
