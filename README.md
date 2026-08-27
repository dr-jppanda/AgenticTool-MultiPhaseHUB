# MHT-DataHub

An automatically-labeled, searchable catalog of **multiphase heat transfer** datasets.
Each entry is grounded in its source paper and carries a normalized numeric operating
envelope plus multi-tier faceted tags derived from it — with every value traceable to
a verbatim quote, its page number(s), and its section(s).

See [`PLAN.md`](PLAN.md) for the design rationale. This file is how to run it.

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

Afterwards, if the run reported anything:

```bash
python -m mhtdb.pipeline review      # low-confidence items, one row each
python -m mhtdb.pipeline propose     # vocabulary the taxonomy is missing
```

What is and isn't reused:

| | Keyed on | Re-runs when |
|---|---|---|
| `S0` document model | record id, on disk | the PDF is new |
| `S1-S4` extraction | doc hash + prompt version + model + schema + backend | any of those change |
| `S5` normalize | nothing — pure function | every run (it's free) |
| Existing records | — | **never touched by a new paper** |

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

**Accepted tiers are never reshuffled.** This is deliberate, and it is the one
place a naive design goes wrong: re-clustering the whole taxonomy each time
would return different parents for the same tags across runs and silently undo
groupings you had already accepted. Curation only ever *adds*.

### When you change a binning threshold

```bash
python -m mhtdb.pipeline renormalize
```

Re-applies units, CoolProp properties, dimensionless groups, the plausibility
gate and all binning rules across the whole catalog, and reports what changed.
**Zero model calls** — this is the payoff for extracting numbers in the paper's
own units rather than asking for tags.

### Review queue

Low-confidence and new-vocabulary items land in `catalog/review/queue.json`.

```bash
python -m mhtdb.pipeline review
python -m mhtdb.pipeline review --accept 'new_term::hfe-7300'
python -m mhtdb.pipeline review --reject 'new_term::benzene' --note "1951 survey one-off"
```

Three properties worth knowing:

- **Review never blocks use.** A gated value is still in the record and still
  searchable — just marked unconfirmed, and rendered with a dashed border in the
  dashboard so "the model inferred this" is distinguishable at a glance from
  "this is confirmed".
- **Rejections are remembered** (`catalog/review/rejections.json`). A rejected
  suggestion is never raised again; without that the same term returns with the
  next paper and the queue never empties.
- **Thresholds are asymmetric.** Reuse auto-applies at 0.75; proposing a *new*
  vocabulary term needs 0.85. Getting a reuse wrong costs one record and is
  trivially undone; a bad new term permanently enlarges the shared vocabulary
  and colours every later judgement.

`fundamental` is never gated — gating it would push reviewers toward inventing
an application, which is the exact failure that facet is prone to.

## Testing it

```bash
pytest -q            # 69 deterministic tests, ~35s, no API calls, no cost
```

`tests/test_smoke.py` covers the parts that must not silently break: the locator
(exact, noisy, page-crossing, and *fabricated* quotes), unit conversion and the
plausibility gate, dimensionless groups against CoolProp, binning, the
figure-digitizer contract, taxonomy/code agreement, and the dashboard build. It
also asserts that **no catalog record contains an unresolvable quote**, which is
the invariant the whole design rests on.

`tests/test_digitize.py` covers the figure pipeline the way it has to be
covered: it *draws* a figure from known data, runs it through the same code a
paper takes, and asserts the numbers come back — within 0.35 K and 1 W/cm² on
the vector path, 1 K and 4 W/cm² on the raster path. It also asserts that a
figure with unreadable axes raises rather than inventing a calibration.

It does not test extraction quality — that needs `eval/` and a gold set.

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

## Model backends

Extraction runs against the Anthropic API, a local **Claude Code** installation,
or a local **Codex CLI** installation. The CLI backends need no separate API key:
each reuses its existing signed-in session.

```bash
python -m mhtdb.pipeline backends
#   api          no credentials
#   claude-code  C:\...\claude.EXE  (2.1.228 (Claude Code))
#   codex        C:\...\codex.EXE   (codex-cli ...)
#   auto would select: claude-code
```

Selection order: `--backend` flag → `MHTDB_BACKEND` env var → API credentials if
present → `claude` on PATH → `codex` on PATH → error. Force one with
`--backend api`, `--backend claude-code`, or `--backend codex`; pick a model with
`--model`. If `--model` is omitted for Codex, its configured CLI default is used.

```bash
# One-time setup if `python -m mhtdb.pipeline backends` says "Not logged in"
codex login

# Reuse the existing Codex login and rebuild the dashboard afterwards
python -m mhtdb.pipeline run papers/*.pdf --backend codex --build

# Or select an explicit Codex model
python -m mhtdb.pipeline run papers/one.pdf --backend codex --model <model-id>
```

On Windows, the pipeline checks PATH and also detects the Codex executable
bundled with the OpenAI VS Code or VS Code Insiders extension. If Codex lives
somewhere else, point to it explicitly before running:

```powershell
$env:MHTDB_CODEX_BINARY = "C:\full\path\to\codex.exe"
python -m mhtdb.pipeline backends
```

| | `api` | `claude-code` | `codex` |
|---|---|---|---|
| Needs a key | yes | **no** — existing Claude login | **no** — existing Codex login |
| Structured output | `output_config.format` | `--json-schema` | `--output-schema` |
| Paper placement | cached system block | `--system-prompt-file` | stdin to `codex exec` |
| Multi-pass reuse | cache breakpoint | server-side prompt cache | content-addressed disk cache; provider caching is CLI-managed |
| Tool access | n/a | disabled | empty read-only temporary workspace |
| Per-paper overhead | none | ~10-12k agent preamble/call | Codex agent preamble/call |

**Cost note.** Claude Code adds its own system preamble to every invocation, so
per-paper cost is above the API path's. Measured across this corpus, four passes
per paper: **$0.85/paper API-equivalent** — down from $1.88 before disabling
tools properly (see `docs/lumina-comparison.md`). Note `total_cost_usd` is what
the calls *would* cost at API rates, which is not what a Pro/Max subscriber is
billed. Use `--backend api` for large batches if you have a key.

`--bare` would strip the remaining preamble but forces `ANTHROPIC_API_KEY` auth,
defeating the point of this backend, so it is not used.

## The extractors

| | `--rules` (`extract_rules.py`) | default (`extract.py`) |
|---|---|---|
| Needs a model at all | no | yes (any configured backend) |
| Reads tables, resolves "as above" | no | yes |
| Distinguishes the paper's own work from its literature review | **no** | yes |
| Recall | low | high |
| Marked in records as | `extractor: "rules-v0"` | for example `extractor: "claude-opus-5/p1@claude-code"` or `extractor: "configured-default/p1@codex"` |

The rule extractor exists so the catalog and dashboard work end-to-end without an API
key, and so Phase-0 hand-labeling starts from a draft. **Treat every field it produces
as a draft.** Its known failure mode is quoting a literature-review sentence about
someone else's apparatus as if it described this paper's.

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

### Provenance is resolved, not generated

The model returns **only a verbatim quote**. `DocumentModel.locate()` finds that quote
in a page- and section-indexed document and reports which pages and sections it
overlaps — so multi-page and multi-section spans fall out naturally, and a quote that
cannot be found is rejected. One function is both the provenance resolver and the
hallucination check. The model is never asked for a page number.

Matching is exact after normalization, with an anchored fuzzy fallback (≥0.86) for
ligature and hyphenation noise.

### Prompt caching

The paper text is the cached prefix; each pass's instruction is the varying suffix. S2,
S3 and S4 read the cache S1 wrote, so a four-pass extraction costs roughly one paper's
input tokens. Extraction results are also content-addressed on disk under
`pipeline/cache/`, keyed by document, prompt version, model, and schema — so iterating
on one pass's prompt re-bills only that pass.

---

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

### Plugging in your own digitizer instead

`mhtdb/figure_points.py` is still the seam, and `mhtdb/digitize.py` is simply
the first thing plugged into it. To substitute your own:

**What we hand you** — `s0_ingest` emits one `FigureInput` per detected figure
(`figure_id`, printed label, caption, page, rendered PNG path, bbox):

```bash
python -m mhtdb.pipeline figures --record kim-2016-roughness-moderate-wettability \
                                 --out /tmp/figs.json
```

**What you hand back** — JSON matching [`schema/point.schema.json`](schema/point.schema.json):
one or more series per figure, each with named x/y axes, units as printed, and
`[x, y]` pairs. Then:

```bash
python -m mhtdb.pipeline ingest-points --record <id> --from points.json
```

Points land in `catalog/points/<id>.points.json`; the record gets a `points_ref` and a
summary, so the catalog stays small. For in-process use, implement the
`FigurePointProvider` protocol and call `register_provider()` —
`digitize.DeterministicDigitizer` is a worked example.

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

## Changing the taxonomy is cheap; changing extraction is not

Edit a threshold in `binning.yaml`, re-run `normalize_record` over the corpus, and every
record re-tags with zero LLM calls. That is the entire reason S2 extracts numbers in the
paper's own units instead of asking the model for tags.

## Known limitations

- Section detection is a font-size/numbering heuristic (`s0_ingest._extract_sections`).
  It over-segments scanned reports — the 1951 HTL report yields 314 "sections". Swap in
  GROBID or docling if this matters; only the `DocumentModel` shape is depended on.
- Title extraction is a scored heuristic and falls back to the filename. The LLM path
  takes the title from S1 instead.
- `mhtdb/extract.py` is written but **has not been executed** — this environment had no
  API credentials. Expect to debug the first real run.
- The eval harness (`eval/`) is a skeleton. It needs a gold set before it means anything.
- Two-column figure legends extract as one interleaved text block, so a
  digitized curve's label can come back scrambled (`"Sm, Micro1, r = r 1.0 ="`).
  The points are unaffected.
- The raster digitizer names its series by colour, not by legend text, and
  table crops are not parsed into cells — the crop is produced, the
  transcription is still manual.
