# Figures: crop, digitize, compare

Two prompts drove this part of the pipeline:

1. *"Process every PDF. For each, extract every figure, vector/chart graphic, and
   table. Crop tightly around the visual element but include its caption. Save
   each as its own file, named after its caption."* → `mhtdb/figure_crops.py`
   (S8), `python -m mhtdb.pipeline crops`.
2. *"Build a literature-summary boiling curve by extracting real data out of the
   extracted figure files — vector-path extraction first, raster digitization
   as fallback — and plot it with a Rohsenow overlay."*
   (`prompts/prompt_pdf-figure_to_data-csv.txt`) → `mhtdb/digitize.py` (S9).

S8 turned that first prompt into deterministic Python once and for all — a
crop is produced the same way every time, costs nothing to re-run, and needs
no model. S9 went the other way: it hands the *second* prompt (adapted to one
figure and one schema at a time) to an agentic `claude` CLI call on every run,
rather than a bespoke ~1900-line vector/raster/OCR implementation. The
trade-off, stated plainly: a figure digitized twice can come back with
slightly different points, and every call costs tokens and takes real
wall-clock time — in exchange for a small amount of orchestration code
instead of a large amount of tuned heuristic code, and a digitizer that reads
a legend or an axis title the way a person would rather than through a
brittle OCR-and-pattern-match pipeline.

## S8 — crop (unchanged, deterministic)

**Crops go beside the existing figure store, not into `extracted_output/`.**
`s0_ingest` already writes to `pipeline/figures/<record-id>/` and the figure
manifest, `schema/point.schema.json` and the dashboard all key off `fig-N` ids.
A second tree named after PDF stems would have needed a mapping table between
the two, and mapping tables rot. Crops are therefore
`pipeline/figures/<record-id>/fig-4.pdf` + `.png`, plus `crops.json`.

**Previews follow their content.** Each crop is written twice: a PDF that
keeps the original drawing commands and a preview image. Line art gets PNG; a
scan or photograph gets JPEG, which is about 8x smaller on exactly the
material where lossless compression buys nothing but noise.

**Pointer papers are cropped as well.** A review or correlation-only paper is
filed under `catalog/pointers/`, but its figures are still figures — the 1951
HTL report is one of the richest sources of plain-copper boiling curves here.
Cropping and digitizing walk both directories; only dataset records get a
`points_ref` written back.

**Uncaptioned graphics are kept, not skipped.** They become `fig-p7-01` —
page 7, first cluster — rather than being dropped.

## S9 — digitize (prompt-driven, one figure per paper)

Exactly one figure per paper reaches the real digitizer.
`select_boiling_curve_figure()` is a single cheap, tools-disabled,
structured-output call (the same fast contract S1–S4 use) that sees every
figure crop's caption at once and names the ONE that is this paper's primary
boiling-curve comparison plot — not "is this A plot" per figure, but "which
figure is THE plot" for the whole paper. Measured against both papers in
this corpus: it correctly picked Dharmendra's Fig. 8 over Fig. 7 ("a
single-surface validation") and Fig. 9 ("HTC only") — genuine discrimination
between multiple boiling-curve-*shaped* candidates, not just a yes/no filter
— and Shi's Fig. 6, in 5–7 seconds each. A paper with no boiling-curve
figure at all costs only this one cheap call and is skipped entirely; a
paper with several boiling-curve-shaped figures still only costs one real
extraction.

Only the winning crop goes to the real digitizer. For that one figure,
`mhtdb/digitize.py` builds a single-page scratch PDF
(the source page, clipped to the figure's own bounding box — not the
pre-existing crop file, since a crop written with `show_pdf_page` wraps its
content in a Form XObject whose transform an agent's own `get_drawings()`
call would not fold in) and runs:

```
claude -p <prompt> --output-format json --permission-mode bypassPermissions --max-turns 60
```

with tool use *enabled* — the one place in this pipeline that departs from
S1–S4's `--tools ""` structured-output-only calls, because the task
(inspect a PDF, decide vector vs. raster, write and run extraction code,
produce a plot to check itself if it wants to) genuinely needs a model that
can act, not just answer. The prompt (in `mhtdb/digitize.py`, adapted from
`prompts/prompt_pdf-figure_to_data-csv.txt`) walks the same steps the
original deterministic engine did:

1. **Probe for vector data** — real drawing primitives vs. one flat raster image.
2. **Vector-path extraction** (preferred) — read tick calibration from the PDF
   text layer, separate series by stroke/fill color and marker shape, read
   coordinates directly off path vertices.
   `extraction_method: vector_path_extraction`.
3. **Raster digitization** (fallback) — calibrate from tick pixel positions,
   isolate each series by legend color/marker, trace visually otherwise.
   `extraction_method: pixel_calibrated_digitization` or
   `manual_visual_digitization`.
4. **Axis identity** — read the physical quantity and unit off each axis
   title, preferring the taxonomy's own field names (`dT_wall`, `q_flux`,
   `htc`, ...) so output lines up with `mhtdb.normalize` and `mhtdb.curves`.
   An axis whose title is genuinely unreadable comes back with `quantity: ""`
   rather than a guess.
5. **Indistinguishable series** merge into one curve with a note, rather than
   a fabricated split.

The model writes its answer as `output.json` matching
`schema/point.schema.json`'s per-series shape; `digitize_figure()` reads that
file back and validates it with `mhtdb.figure_points.validate_series` before
returning it. A crop that isn't actually a data plot (a micrograph, a
schematic, a table) comes back as `{"series": []}` — not an error. Only the
*mechanism* failing — no `claude` CLI on PATH, the call erroring or timing
out, or output that doesn't validate — raises `NeedsCalibration`, the same
exception name and `needs_calibration.json` bookkeeping the old engine used,
so `pipeline.py`'s handling of a failed figure didn't need to change.

In practice the model does noticeably better than the old heuristics at
exactly the cases that used to need a human: on Shi 2015's Fig. 6 (a fully
rasterized, six-series boiling-curve comparison with a black-on-black CHF
annotation arrow crossing the plot), it read every axis title and every
legend label directly, correctly separated the black "3 μm" curve from the
black annotation arrow by shape, and needed zero manual `--calib`/relabeling
— all six curves, previously only five were recoverable safely.

### Making it faster

The biggest lever is the one-figure-per-paper selection above: instead of
every boiling-curve-*shaped* figure paying for a full agentic call, at most
one per paper ever does. Two more shortcuts on top of that, both free — they
reuse detection S8 already did rather than paying for the model to redo it:

- **`--jobs N`** (default 4) runs N *papers'* selected figures concurrently.
  Each is an independent subprocess and scratch directory, so this is pure
  wall-clock savings — no change in cost or accuracy.
- **Known-vector/known-raster shortcut** — `Crop.has_vector`/`has_raster`
  (computed for free during `crops`) is passed straight into the prompt as a
  fact ("this crop was already scanned and found to contain real vector
  paths -- skip straight to Step 2"), instead of asking the model to spend
  turns rediscovering something already known. A clean known-vector crop
  also gets a smaller turn/effort budget (`VECTOR_MAX_TURNS`/`VECTOR_EFFORT`
  in `mhtdb/digitize.py`) than the default, since reading coordinates off
  paths that already exist needs far less iteration than the open-ended
  raster case. A crop with both flags set (or neither known) keeps the full
  probing instructions and budget — that ambiguity is real.

### Automated recovery, before asking a human

Two failure modes that used to always need a person noticing
`needs_calibration.json` and re-running by hand are now handled inside
`digitize_figure()` itself:

- **Multi-panel selection.** `select_boiling_curve_figure()` also reads which
  lettered sub-panel — `(a)`, `(b)`, ... — is the boiling curve, straight off
  the same captions it already reads to pick the figure. `cmd_digitize` wires
  that straight into the digitizer's `panel=` argument, so a figure like
  "(a) heat flux vs wall superheat, (b) HTC vs heat flux" is scoped to panel
  1 on the very first attempt instead of handing the model the whole stacked
  figure and hoping it self-restricts within its turn budget. `--panel` on
  the command line still overrides this.
- **Mechanism-failure retry.** If a call still fails mechanically — no
  `output.json` written, a timeout, unparsable JSON, or a schema-invalid
  series — `digitize_figure()` retries once automatically (`retries=1` by
  default) with a bumped turn budget and a prompt that names what went wrong
  and asks for a faster, narrower attempt (and, on a schema failure, what
  specifically to fix). A figure the model affirmatively judges not to be a
  plot (`{"series": []}`, no exception) is never retried — only genuine
  `NeedsCalibration` is.

A panel-restricted crop also gives up the known-vector turn/effort shortcut
above, even when `has_vector`/`has_raster` says "vector" — isolating one
panel's series out of a figure that stacks several overlapping plots is real
work, not the mechanical "just read the paths" case that shortcut assumes.

### When a figure still needs a human

`--figure` targets one specific figure directly instead of letting selection
pick it — for correcting a bad auto-selection, or to supply `--calib`.
`--calib` and `--panel` work exactly as before — they're delivered as an
explicit hint in the next prompt instead of a numeric input to a Python
algorithm:

```bash
# name an axis the model couldn't read a title for
python -m mhtdb.pipeline digitize --record huang-2023-pure-copper \
    --figure fig-2 --calib "x=dT_wall:degC,y=q_flux:kW/m2"

# confirm the axis range at the frame edges
... --figure fig-2 --calib "x=0:30:dT_wall:degC,y=0:1200:q_flux:kW/m2"

# a figure that stacks unrelated plots — only digitize panel 2
... --figure fig-2 --panel 2 --calib "x=0:30:dT_wall:degC,y=0:1200:q_flux:kW/m2"

# panel/frame detection needs pinning down as fractions of the crop (x0/y0/x1/y1)
... --figure fig-3 --calib "frame=0.075/0.02/0.455/0.73,x=0:30:dT_wall:K,y=0:1000:q_flux:kW/m2"
```

Given how much of the old need for this came from OCR/heuristic brittleness
(an axis title baked into pixels, a legend the color-mask couldn't read),
expect to need it less often now — but a genuinely ambiguous multi-panel
figure, or a crop whose caption sits outside the clip, can still trip up a
model the same way it tripped up the old code. `_save_calib`/`_load_calib`
still remember it in `pipeline/calibrations/<record>.json` and reuse it on
every later run.

## Choosing which curve to compare

`curves` plots one curve per paper: the plain, untreated reference surface.
That choice is made from the legend text the digitizer recovered, and the
figure caption is used only to check the working fluid — mchale-2011 reports
HFE-7300 and DI water in adjacent figures with identical legends, and without
the fluid check the "literature scatter" would really be a fluid change.

Every included curve prints the reason it was included, and every excluded one
the reason it was not. `--all-series` overrides the filter.

The Rohsenow overlay takes its properties from CoolProp through
`normalize.fluid_properties`, not from constants in the plotting code, so the
reference line and the catalog's dimensionless groups cannot drift apart.
`c_sf = 0.013`, `n = 1` is the water-on-copper pair; both are surface-dependent,
which is why it is drawn as a reference and never fitted.

## Known limitations

- **Not deterministic.** Digitizing the same figure twice can return slightly
  different point counts or values. `--calib`/relabeling hints reduce but
  don't eliminate this.
- **Cost and time.** Each figure is a real agentic call — turns spent
  inspecting the PDF, writing extraction code, and iterating on it. A dense
  multi-series raster figure can take several minutes and cost noticeably
  more than a simple vector one; `DEFAULT_MAX_TURNS` and `DEFAULT_TIMEOUT` in
  `mhtdb/digitize.py` are sized for the hardest figure in this corpus, not
  the average one.
- **Needs a local, tool-capable Claude Code CLI.** Unlike S1–S4 (which also
  support the raw Anthropic API or Codex), S9 currently only drives the
  `claude` CLI, and specifically with tool use enabled — `ApiBackend` and
  `CodexBackend`'s structured-output-only contracts don't give a model
  anywhere to actually open a PDF.
- **Table crops capture the table's *region*; no cell-level parsing yet.**
  The original prompt's "transcribe the exact values directly" path is
  therefore still manual — the crop is produced, the transcription is not.
- **Two-column legends and other layout quirks can still confuse a reading,**
  the same way they could confuse a person skimming quickly; a messy or
  oddly-labeled series is a signal to look at the crop yourself.
