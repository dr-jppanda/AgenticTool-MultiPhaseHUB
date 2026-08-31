# MHT-DataHub

An automatically-labeled, searchable catalog of **multiphase heat transfer** datasets.
Each entry is grounded in its source paper and carries a normalized numeric operating
envelope plus multi-tier faceted tags derived from it — with every value traceable to
a verbatim quote, its page number(s), and its section(s).

---

## Quick start

```bash
cd mht-datahub
pip install -e .

python -m mhtdb.pipeline backends                 # what's available here
python -m mhtdb.pipeline run papers/*.pdf --build # extract + rebuild dashboard
# then open app/dist/index.html
```

Run everything from the repo root. `pip install -e .` also creates an `mhtdb`
console script, but on Windows it lands in
`%APPDATA%\Python\Python312\Scripts`, which is often not on PATH — so
`python -m mhtdb.pipeline` is the form used throughout this file.

## Adding more papers

Drop the PDFs in `papers/` and run the same command. Everything is incremental
by construction — **nothing already in the catalog is recomputed or reshuffled.**

```bash
python -m mhtdb.pipeline run papers/*.pdf --build
```

That is the whole loop. Point it at the **entire** folder, not just the new
files: everything already processed is served from cache, so re-running the full
set costs nothing. Measured on the current corpus — 5 papers, 20 passes, all
cached: **2.8 seconds, $0.00.**

There is no long-running service to restart. The dashboard is a build artifact,
so new records stay invisible until it is regenerated; `--build` does that at the
end of the run. Then reload `app/dist/index.html` in the browser.


### When new papers need vocabulary you don't have

The model cannot invent a facet value; it emits `propose_new` instead, and
those collect in `taxonomy/proposals/pending.json` grouped by term with the
records that wanted it:

```
4x  fluid: ethanol        (nearest existing parent: mixture)
1x  fluid: HFE-7300       (nearest existing parent: dielectric)
```

A term four papers asked for is a much stronger case than one paper's one-off —
which is the whole reason for grouping rather than deciding paper by paper.

To adopt one: add it under the right tier in `taxonomy/v1/facets.yaml`, then
re-run **only S3** for the affected records:

```bash
rm pipeline/cache/*_s3_taxonomy_*.json     # drop just that pass
python -m mhtdb.pipeline run papers/<affected>.pdf
```



### Manual checks worth doing once

```bash
# 1. See what the dashboard looks like
python app/build.py && start app/dist/index.html          # Windows
#                     open  app/dist/index.html           # macOS

# 2. Cheap end-to-end without spending anything
python -m mhtdb.pipeline run --rules papers/*.pdf

# 3. One paper through the real model, watch the per-pass token/cost line
python -m mhtdb.pipeline run papers/<one>.pdf

# 4. Prove provenance is real: pick any quote in the dashboard, open the PDF
#    at the page it claims, and confirm the sentence is there.

# 5. Figure-digitizer round trip
python -m mhtdb.pipeline figures --record <id> --out figs.json
python -m mhtdb.pipeline ingest-points --record <id> --from points.json
```



## Pipeline stages

```
S0  ingest      PDF -> DocumentModel: normalized text, page index, section index,
                figure crops + captions.        mhtdb/s0_ingest.py
S1  triage      paper type, contains-dataset gate.
S2  conditions  numeric envelope in the paper's own units + evidence quotes.
S3  taxonomy    facets from the controlled vocabulary, or propose_new.
S4  application application target, with confidence. Hallucination hotspot.
S5  normalize   DETERMINISTIC: units -> SI, CoolProp properties, dimensionless
                groups, plausibility gate, binning -> derived tags.
                                                mhtdb/normalize.py
S6  verify      (a) mechanical: every quote must occur in the document;
                (b) optional LLM audit pass for contradictions.
S7  commit      catalog/records/<id>.json, git-tracked.
S0b crops       every figure/table -> its own cropped PDF + PNG, caption
                included.                       mhtdb/figure_crops.py
S8  points      DETERMINISTIC: vector-path extraction, raster tracing as
                fallback. Axis values resolved, never guessed.
                                                mhtdb/digitize.py
S9  curves      point tables -> CSV + comparison plot with a CoolProp-backed
                Rohsenow overlay.               mhtdb/curves.py
```


## Figures: crop, digitize, compare

Three commands turn the PDFs' figures into numbers. All deterministic, no model
calls, no cost:

```bash
python -m mhtdb.pipeline crops                  # S0b: one file per figure/table
python -m mhtdb.pipeline digitize               # S8:  figures -> points
python -m mhtdb.pipeline curves --out out/      # S9:  points -> CSV + plot
```

`crops` finds the graphic itself rather than rendering the whole page: it
clusters the page's images and vector paths into connected components, matches
each to the caption that refers to it, and writes the union as its own
single-page PDF (vector fidelity kept) plus a preview image — PNG for line
art, JPEG for scans — in
`pipeline/figures/<record-id>/`, keyed by the same `fig-N` ids the manifest and
point schema use. Sub-panels of one figure merge; table ruling is not mistaken
for a plot; a graphic with no caption is kept under a positional id rather than
dropped. Every crop carries a `caption_confidence`.

`digitize` reads the numbers back. **Vector first:** a plot placed as native
vector art still contains the drawing commands that produced it, so marker
centres and polyline vertices come back at full precision — error is limited to
the axis calibration, hence `confidence` 0.85-0.98. Series are separated by
colour and by filled-vs-open glyph (which is how a paper distinguishes
ascending from descending runs), and each curve is labelled from its own legend
entry. **Raster fallback:** a flattened or scanned figure is traced pixel by
pixel against a calibrated axis at ~0.62.

Axis *values* are resolved, not generated — from the PDF text layer, or from
OCR if `pytesseract` is installed, or from you:

```bash
python -m mhtdb.pipeline digitize --record huang-2023-pure-copper \
    --figure fig-2 --calib "x=0:30:dT_wall:degC,y=0:1200:q_flux:kW/m2"
```

Two numbers per axis — the value at each end of the plot frame — plus what the
axis measures. Add `--panel N` for a figure that stacks unrelated plots, or
`frame=x0/y0/x1/y1` (fractions of the crop) when panel detection picks the
wrong one; `--calib "x=dT_wall:degC"` just names an axis the extractor
calibrated but could not label. It is remembered in `pipeline/calibrations/`
and reused thereafter. Figures that cannot be calibrated are listed in
`pipeline/figures/<record-id>/needs_calibration.json` instead of being guessed
at. Points land in `catalog/points/<id>.points.json` through the existing
`ingest-points` contract, so the record gets its `points_ref` and the dashboard
sees them.

`curves` compiles every digitized point into a flat CSV (`paper_id`,
`figure_id`, `curve_id`, value and unit as printed, `source_type`,
`extraction_method`, `digitization_confidence`, `notes`, plus derived
`wall_superheat_K` and `heat_flux_W_m2`) and plots one curve per paper — the
plain untreated reference surface, chosen from the legend text, with the
caption used to check the working fluid. Every curve included prints why, and
every curve excluded prints why not; `--all-series` overrides. The Rohsenow
reference line takes its properties from CoolProp via
`normalize.fluid_properties`, so overlay and catalog cannot drift apart.

See [`docs/figure-pipeline.md`](docs/figure-pipeline.md) for the design notes
and the known limitations.

---

## Layout

```
taxonomy/v1/facets.yaml    controlled vocabulary — 8 facets, hand-curated tiers 1-2
taxonomy/v1/binning.yaml   numeric -> derived-tag thresholds
schema/point.schema.json   figure-digitizer return contract
mhtdb/                     pipeline stages
catalog/records/*.json     source of truth, git-tracked, diffable
catalog/pointers/*.json    reviews and correlation-only papers (not datasets)
catalog/points/*.json      digitized figure data
app/build.py               catalog -> dashboard
app/dist/index.html        the built dashboard
pipeline/docmodels/        cached DocumentModels
pipeline/figures/          cropped figures/tables + crops.json manifest
pipeline/calibrations/     axis calibrations you supplied, remembered
eval/                      gold set + scorer
```

## Dashboard

`python app/build.py` emits a single self-contained HTML file — inlined data,
no CDN, no server, opens by double-click. It provides faceted filters with live
counts, numeric range sliders over the SI envelope, full-text search across titles
and evidence quotes, a table view, light/dark themes, coverage charts, and a record
detail panel that shows **every extracted value beside the quote, page and section it
came from**.

The **Boiling curves** panel plots every digitized series in the catalog on one
set of axes — heat flux against wall superheat, with a Rohsenow reference line
computed from CoolProp properties. It defaults to the plain reference surfaces
(the cross-paper comparison), switches to all series, toggles a log flux axis
and a wall-temperature x axis, follows the sidebar filters, and on hover names
the paper, the series as its own legend labelled it, and whether the value was
read from vector paths or traced from pixels. Curves colour by paper, and the
line breaks rather than spanning a jump the data does not support. That last part is what makes an auto-labeled catalog trustworthy to a
domain reader.

For the eventual server move: `python app/build.py --mode server` emits the same page
plus `dist/data/catalog.json`. The catalog JSON is the contract either way, so
deploying behind FastAPI is a hosting change rather than a rewrite.


