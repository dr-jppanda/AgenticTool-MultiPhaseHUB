# Figures: crop, digitize, compare

Two prompts drove this part of the pipeline, and both are now code:

1. *"Process every PDF. For each, extract every figure, vector/chart graphic, and
   table. Crop tightly around the visual element but include its caption. Save
   each as its own file, named after its caption."* → `mhtdb/figure_crops.py`
   (S0b), `python -m mhtdb.pipeline crops`.
2. *"Build a literature-summary boiling curve by extracting real data out of the
   extracted figure files — vector-path extraction first, raster digitization
   as fallback — and plot it with a Rohsenow overlay."* → `mhtdb/digitize.py`
   (S8) and `mhtdb/curves.py`, `python -m mhtdb.pipeline digitize` and
   `curves`.

They are deterministic Python rather than prompts handed to a model on each
run. Same reason S5 is deterministic: a figure digitized twice must give the
same numbers, re-running must cost nothing, and every recovered value has to
carry the evidence of how it was obtained.

## Where they differ from the prompts, and why

**Crops go beside the existing figure store, not into `extracted_output/`.**
`s0_ingest` already writes to `pipeline/figures/<record-id>/` and the figure
manifest, `schema/point.schema.json` and the dashboard all key off `fig-N` ids.
A second tree named after PDF stems would have needed a mapping table between
the two, and mapping tables rot. Crops are therefore
`pipeline/figures/<record-id>/fig-4.pdf` + `.png`, plus `crops.json`.

**Previews follow their content.** Each crop is written twice: a PDF that
keeps the original drawing commands (what the vector engine reads) and a
preview image. Line art gets PNG; a scan or photograph gets JPEG, which is
about 8x smaller on exactly the material where lossless compression buys
nothing but noise. A photo-heavy crop whose PDF runs over 500 KB is flattened
to its own pixels too — there are no paths in an SEM panel worth preserving,
and on the 1951 scanned report keeping them cost 77 MB for 31 crops.

**Pointer papers are cropped as well.** A review or correlation-only paper is
filed under `catalog/pointers/`, but its figures are still figures — the 1951
HTL report is one of the richest sources of plain-copper boiling curves here.
Cropping and digitizing walk both directories; only dataset records get a
`points_ref` written back.

**Uncaptioned graphics are kept, not skipped.** The prompt allows for
`figure_01.pdf` when no caption exists; here they become `fig-p7-01` — page 7,
first cluster. A plot whose caption sits in an adjacent column is still
digitizable, and thesis-style documents (mchale-2011) have many.

**The digitizer reads the source page with a clip rect, not the cropped PDF.**
A crop written with `show_pdf_page` wraps its content in a Form XObject whose
transform PyMuPDF does not fold into `get_drawings()`, so coordinates come back
in the wrong space. `Crop.bbox` is the real input; the crop files are the
human-facing artifact and the input to any external digitizer you plug in.

**Axis values are never invented.** The prompt's Step 3 assumes tick labels can
be read off the image. They can, if there is a text layer or an OCR engine —
otherwise this refuses. `NeedsCalibration` names the figure, reports the ticks
it *could* read, and the run writes `needs_calibration.json`. You then supply
the two numbers per axis once:

```bash
python -m mhtdb.pipeline digitize --record huang-2023-pure-copper \
    --figure fig-2 --calib "x=0:30:dT_wall:degC,y=0:1200:q_flux:kW/m2"
```

That is remembered in `pipeline/calibrations/<record>.json` and reused on every
later run. Two more forms exist for the cases a single axis range cannot cover:

```bash
# a figure that stacks unrelated plots — panel 2 is the boiling curve
... --figure fig-2 --panel 2 --calib "x=0:30:dT_wall:degC,y=0:1200:q_flux:kW/m2"

# panel detection picked the wrong plot: pin the rectangle yourself,
# as fractions of the crop (x0/y0/x1/y1)
... --figure fig-3 --calib "frame=0.075/0.02/0.455/0.73,x=0:30:dT_wall:K,y=0:1000:q_flux:kW/m2"

# the axes calibrated from the text layer but came back unnamed
... --figure fig-8 --calib "x=dT_wall:degC,y=q_flux:kW/m2"
```

The frame's edges are where the supplied values live, so it need not be the
axis box — pinning it between two labelled ticks works and is often easier to
read off the figure. Guessing an axis instead would put fabricated numbers into a catalog
whose whole premise is that values are traceable.

## What each engine does

| | vector | raster |
|---|---|---|
| Trigger | plot area contains drawing primitives | plot area is one flat image |
| Points from | marker centres, polyline vertices | colour-masked pixel tracing |
| Series split by | stroke/fill colour + filled-vs-open glyph | hue cluster |
| Curve labels | legend glyph → legend text on the same line | colour name only |
| Confidence | 0.85–0.98 | ≈0.62, less when OCR supplied the ticks |
| `extraction_method` | `vector_path_extraction` | `pixel_calibrated_digitization` |

Details that matter more than they look:

- **Tick marks are not data.** A hairline touching a frame edge is a tick; a
  marker touching the same edge is the CHF point. Thinness discriminates, not
  position — filtering on position alone truncates exactly the end of the curve
  a boiling paper is about.
- **Calibration is consensus-fitted.** Every pair of tick labels proposes a
  mapping and the most-agreed mapping wins, so a panel letter, an inset's
  numbers, or an arrowhead OCR'd as "1" cannot drag the axis. A frame whose
  agreed ticks do not span at least 45% of it is rejected outright — that check
  is what stops kim-2016 Fig. 8 calibrating against its own legend.
- **Ascending and descending runs stay separate.** Two style groups merge only
  when the smaller point cloud is contained in the larger one (a marker's fill
  and its outline). Hysteresis branches share a colour but diverge, so they
  survive as two curves — merging them would erase the effect the paper is
  reporting. Genuinely indistinguishable groups merge and say so in `notes`.
- **In-plot legends are excluded from data.** Their glyphs are drawn in the
  series' own style and would otherwise contribute phantom points at whatever
  data coordinates the legend box occupies.

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

- Panel detection is a heuristic. It merges sliced image strips and splits at
  blank gutters, but on a figure whose panels have different axis styles it can
  return the wrong plot with full confidence — which is what `frame=` is for.
- A raster-traced series is named for its colour, so the plain-surface filter
  falls back to the figure caption, and only when the figure holds three or
  fewer traced series. "Boiling curves of the reference Cu plate and of
  surfaces with nanowire arrays" describes both kinds at once; picking the
  reference out of five traced colours is not something this can honestly do.
- Two-column legends extract as one interleaved text block, so a label can come
  back as `"Sm, Micro1, r = r 1.0 = 2.6"`. The points are unaffected; the label
  is. chu-2013 Fig. 4 is the example in this corpus.
- The raster engine names series by colour (`red`, `blue`), not by legend text.
  Labelling a traced curve needs the legend read from pixels, which is the same
  OCR problem as the tick labels and no more reliable.
- Table crops capture the table's *region*; no cell-level parsing yet. The
  prompt's "transcribe the exact values directly" path is therefore still
  manual — the crop is produced, the transcription is not.
- OCR is optional and off the dependency list. Install `pytesseract` and the
  tesseract binary to calibrate rasterized figures automatically; without them
  they land in `needs_calibration.json`.
