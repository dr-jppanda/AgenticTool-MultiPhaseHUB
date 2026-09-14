# MHT-DataHub

MHT-DataHub turns a folder of PDF papers on **multiphase heat transfer** — pool
boiling, flow boiling, condensation, spray cooling, and related regimes — into a
structured, browsable dataset catalog, without ever asking an LLM to compute a
number itself.

For each paper, an agentic extraction pipeline (S0–S10) parses the document,
decides whether it reports its own dataset, pulls the numeric operating envelope
in the paper's own units together with the verbatim quote and the page(s) and
section(s) it came from, tags it against a controlled multi-tier taxonomy of
physical facets, and records the application it targets — stated by the paper or
inferred, either way flagged for a human to confirm. A separate, fully
deterministic stage converts everything to SI, computes CoolProp fluid
properties and dimensionless groups (Bond, Weber, Froude, Reynolds, boiling
number, reduced pressure, ...), and buckets values into reproducible derived
tags — no model call, no randomness. A mechanical verification pass then
re-locates every quote in the source text before anything is trusted; unlocatable
evidence is quarantined rather than kept.

Where a paper's boiling-curve figure carries data that text extraction can't
reach, a second pipeline crops the figure, has an agentic digitizer read it
directly — vector paths where available, calibrated pixel-tracing otherwise —
and compiles the digitized points into per-paper CSVs and a cross-paper
comparison plot against a CoolProp-backed Rohsenow correlation.

Everything lands in a versioned, diffable JSON catalog
(`catalog/records/*.json`), with anything uncertain routed to a review queue
that never blocks use of the data. `app/build.py` compiles the whole catalog
into a single self-contained, offline HTML dashboard: faceted filters, numeric
range sliders, full-text search, a coverage map, per-paper boiling curves, and
a record detail panel where every value links back to its quote, page, and
section — a click opens the source PDF at that exact page.

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

### Manual checks

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
S4  application application target, with evidence. Hallucination hotspot.
S5  normalize   DETERMINISTIC: units -> SI, CoolProp properties, dimensionless
                groups, plausibility gate, binning -> derived tags.
                                                mhtdb/normalize.py
S6  verify      (a) mechanical: every quote must occur in the document;
                (b) optional LLM audit pass for contradictions.
S7  commit      catalog/records/<id>.json, git-tracked.
S8  crops      every figure/table -> its own cropped PDF + PNG, caption
                included.                       mhtdb/figure_crops.py
S9  points      PROMPT-DRIVEN, one figure per paper: a cheap call picks the
                paper's one boiling-curve figure, then an agentic `claude`
                CLI call (tool use enabled) opens it, extracts vector paths
                or traces raster pixels, and resolves axis values — never
                guesses them.                  mhtdb/digitize.py
S10 curves      point tables -> CSV + comparison plot with a CoolProp-backed
                Rohsenow overlay.               mhtdb/curves.py
```


## Figures: crop, digitize, compare

Three commands turn the PDFs' figures into numbers. `crops` and `curves` are
deterministic, no model calls, no cost; `digitize` is prompt-driven — see
below:

```bash
python -m mhtdb.pipeline crops                  # S8:  one file per figure/table
python -m mhtdb.pipeline digitize               # S9:  figures -> points (model calls)
python -m mhtdb.pipeline curves --out out/      # S10: points -> CSV + plot
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

`digitize` reads the numbers back — from exactly one figure per paper, not
every figure crop. A cheap, tools-disabled call first sees every crop's
caption at once and names the ONE that is this paper's primary boiling-curve
comparison plot; only that winner is handed to `claude -p` with tool use
*enabled* (the one call in this pipeline that isn't structured-output-only)
for the real extraction — not a bespoke Python algorithm. It opens the PDF
itself, decides vector vs. raster, calibrates the axes, separates series by
color/marker, and writes its answer back as JSON matching
`schema/point.schema.json`. **Vector paths**, when present, are read
directly off the drawing commands. **Raster fallback** traces pixels against
a calibrated axis. See `docs/figure-pipeline.md` for the full prompt and
design rationale.

### End-to-end notebook

[`mht_datahub_pipeline.ipynb`](mht_datahub_pipeline.ipynb) runs the whole
pipeline top to bottom against whatever PDFs are in `papers/`: install deps,
pick a model backend (`api` / `claude-code` / `codex` / offline rules),
optionally reset the catalog, S0–S7 ingest and extract each paper into a
catalog record, S8 crop every figure and table, S9 digitize each paper's one
boiling-curve figure, S10 compile the digitized points into a CSV and
comparison plot, build the dashboard, then verify the result and preview it
inline. It's the fastest way to reproduce a full run — or to rerun everything
after adding new papers — without piecing the CLI commands together by hand.

### WebPlotDigitizer cross-check

To sanity-check `digitize` against an independent, human-driven digitization,
two of its outputs were compared against the same figures re-digitized by
hand in [WebPlotDigitizer](https://automeris.io/WebPlotDigitizer/): Allred et
al. (2018) Fig. 4 and Berce et al. (2024) Fig. 5. In both cases the
AgenticTool series and the WebPlotDigitizer series overlay closely across the
full boiling curve, including the transition and film-boiling regions.

| Allred et al. (2018) Fig. 4 | Berce et al. (2024) Fig. 5 |
| --- | --- |
| ![Allred 2018 Fig. 4: AgenticTool vs. WebPlotDigitizer](webplot-digitizer_comparison/allred2018_fig4_webplot_comparison.png) | ![Berce 2024 Fig. 5: AgenticTool vs. WebPlotDigitizer](webplot-digitizer_comparison/berce2024_fig5_webplot_comparison.png) |

See [`webplot-digitizer_comparison/webplot_comparison.ipynb`](webplot-digitizer_comparison/webplot_comparison.ipynb)
for the comparison code and data behind both plots.

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


