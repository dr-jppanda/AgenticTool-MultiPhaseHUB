"""S9 - figure digitization, prompt-driven, one figure per paper.

Every paper gets exactly one digitized figure: `select_boiling_curve_figure`
looks at every figure crop's caption in a single cheap call and picks the ONE
that is this paper's primary boiling-curve comparison plot -- not "is this A
plot" per figure, but "which ONE figure is THE plot" for the whole paper.
Only that winning crop is handed to the real digitizer. A paper with several
boiling-curve-shaped figures (a main comparison plus a zoomed inset, say)
still only costs one real extraction; a paper with none costs only the cheap
selection call.

The winning crop is then handed to an agentic Claude Code CLI call with real
tool access -- unlike every other model call in this pipeline (S1-S4 in
extract.py), which runs with `--tools ""`. It opens the PDF itself, decides
whether the plot is vector or raster, calibrates the axes, separates series,
and writes its answer back as JSON matching schema/point.schema.json. This
module is orchestration only: build the prompts, run the subprocesses,
validate what comes back. The digitization reasoning (vector-path vs
pixel-tracing, tick calibration, series separation) lives in the prompt
(adapted from prompts/prompt_pdf-figure_to_data-csv.txt), not in Python.

Trade-off, stated once: a figure digitized twice can return slightly
different points, and every call costs tokens and takes longer than a
function call. The prior implementation (vector-path parsing + pixel tracing
+ OCR + hue-bin color matching, ~1900 lines, no model) traded flexibility and
maintenance cost for exact reproducibility and zero marginal cost per figure.
This trades back the other way.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

from .figure_points import validate_series

DEFAULT_TIMEOUT = 1800
DEFAULT_MAX_TURNS = 60
DEFAULT_EFFORT = "high"

# A known-vector-only crop (S8's has_vector=True, has_raster=False) skips
# Step 1's self-discovery and gets a smaller budget: reading coordinates off
# paths that already exist needs far less iteration than a raster figure
# whose calibration has to actually be checked against the image.
VECTOR_MAX_TURNS = 20
VECTOR_EFFORT = "medium"

# Canonical axis-quantity names, so the model's output lines up with the rest
# of the catalog (mhtdb.normalize, mhtdb.curves) instead of inventing its own
# vocabulary for the same physical quantities.
_QUANTITY_HINTS = """Use these field names for axis quantities when they apply (free text is fine for anything else, but prefer these for consistency with the rest of the catalog):
  dT_wall    - wall superheat, delta-T = T_wall - T_sat
  q_flux     - heat flux, q''
  htc        - heat transfer coefficient, h
  T_wall     - wall temperature (absolute, not a difference)
  x_quality  - vapor quality
  G          - mass flux
  p_sat      - saturation pressure
  chf        - critical heat flux (only when the axis specifically reports CHF values, not a regular heat-flux axis that happens to include the CHF point)
"""


def select_boiling_curve_figure(crops, model: str | None = None) -> tuple[str | None, int | None, str]:
    """Pick the ONE figure crop, if any, that is this paper's primary
    boiling-curve comparison plot -- and, if that figure stacks multiple
    lettered sub-panels (a)/(b)/(c) covering different quantities, which one
    of them actually is the boiling curve.

    Every paper's crops include plenty that are never going to be boiling
    curves -- SEM micrographs, schematics, XRD patterns, Raman spectra,
    contact-angle photos -- and can include more than one boiling-curve-
    *shaped* figure (a main comparison plot plus a zoomed inset or a single-
    surface repeat). A single cheap, tools-disabled, structured-output call
    (the same fast contract S1-S4 use) sees every crop's caption/label at
    once and picks the best one, so only one figure per paper ever reaches
    the expensive real digitizer.

    Multi-panel captions ("(a) heat flux q vs wall superheat, (b) heat
    transfer coefficient h vs heat flux") are common in this literature, and
    handing the whole stacked figure to the digitizer with no panel hint
    makes it spend its limited turn budget on every panel instead of just
    the one that matters -- the exact failure mode behind a `NeedsCalibration`
    from a figure that should have been easy. Naming the panel here, from the
    same captions already being read, means the digitizer is told up front
    rather than a human having to notice the failure and re-run with
    `--panel` by hand.

    Returns (figure_id, panel, reason). figure_id is None whenever there's
    nothing to go on (no captioned crops), the backend isn't available, the
    call fails, or -- just as validly -- none of the captions actually
    describe a boiling curve. This never invents a winner it hasn't seen
    evidence for. panel is None unless the chosen figure's own caption
    describes multiple lettered sub-panels, in which case it is the 1-based
    index (reading order) of the one that is the boiling curve.
    """
    candidates = [
        (c.element_id, f"{c.label} {c.caption}".strip())
        for c in crops if (c.label or c.caption).strip()
    ]
    if not candidates:
        return None, None, "no captioned figures to choose from"

    try:
        from pydantic import BaseModel

        from .backends import ClaudeCodeBackend
    except ImportError:
        return None, None, "backend unavailable"

    class _Selection(BaseModel):
        figure_id: str | None
        panel: int | None
        reason: str

    try:
        backend = ClaudeCodeBackend(model=model) if model else ClaudeCodeBackend()
    except RuntimeError:
        return None, None, "backend unavailable"

    listing = "\n".join(f'- {fid}: "{caption}"' for fid, caption in candidates)
    instruction = (
        "A paper on multiphase heat transfer has these figures (id: caption):\n\n"
        f"{listing}\n\n"
        "Pick the ONE figure that is this paper's primary boiling-curve plot -- "
        "a chart of heat flux (or CHF) against wall superheat/temperature, "
        "typically comparing several surfaces including a plain/reference one. "
        "If more than one figure shows boiling-curve-shaped data, pick the main "
        "comparison plot -- not a zoomed-in inset, a single-surface repeat, or a "
        "genuinely different quantity with no heat-flux-vs-superheat axes at all "
        "(HTC-only, contact-angle, pressure drop). If NONE of these captions "
        "describe a boiling curve, answer figure_id=null -- do not guess.\n\n"
        "A caption naming a panel just \"CHF\" (rather than spelling out both "
        "axes) is NOT by itself evidence of a bar chart of single CHF values -- "
        "in this literature, the q''-vs-superheat curve that runs up to and "
        "includes the CHF point is itself conventionally called \"the CHF plot\" "
        "or \"CHF curve\", especially when it is paired with a heat-transfer-"
        "coefficient companion panel (\"(a) CHF ... (b) heat transfer "
        "coefficient ... comparing several surfaces\" is the same twin-panel "
        "pattern as \"(a) heat flux vs wall superheat, (b) HTC vs heat flux\", "
        "just worded differently). When a caption is genuinely ambiguous "
        "between a continuous curve and a bar/scalar comparison, prefer "
        "selecting it over guessing it away: the real digitizer opens the "
        "figure itself and safely returns no series if it turns out not to be "
        "a curve, so a wrong guess here costs one extra check, while wrongly "
        "ruling a real curve out here means it never gets digitized at all.\n\n"
        "If the caption of the figure you pick describes it as multiple "
        "lettered sub-panels ('(a) ... , (b) ...') covering different "
        "quantities, set panel to the 1-based index, in reading order "
        "(top-left first, left to right, then down a row), of the ONE "
        "sub-panel that is the heat-flux-vs-wall-superheat boiling curve -- "
        "not the one with heat transfer coefficient, pressure drop, or any "
        "other quantity on its axes. Leave panel null if the figure is not "
        "split into lettered sub-panels at all."
    )
    try:
        result = backend.complete([], instruction, _Selection, effort="low")
        return result.parsed.figure_id, result.parsed.panel, result.parsed.reason
    except Exception as e:
        return None, None, f"selection call failed: {type(e).__name__}: {e}"


class NeedsCalibration(RuntimeError):
    """Raised when the digitizer call itself could not produce a usable result.

    A figure the model examined and judged not to be a data plot is NOT an
    error -- it comes back as an empty series list. This is for failures of
    the mechanism: no backend available, the call errored or timed out, or
    what came back doesn't match the schema.
    """

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


def _resolve_calibration(calibration: dict | None, panel: int | None) -> dict | None:
    """Pick the calibration that applies to the panel being digitized this run.

    A flat dict applies regardless of panel. `{"panels": {"2": {...}}}`
    targets one panel specifically -- resolved here rather than in the prompt
    builder so the prompt only ever sees a flat set of hints.
    """
    if not calibration:
        return None
    panels = calibration.get("panels")
    if not panels:
        return calibration
    if panel is not None:
        return panels.get(str(panel)) or panels.get(panel)
    return calibration


def _calibration_hints(calibration: dict | None, panel: int | None) -> list[str]:
    hints = []
    if panel is not None:
        hints.append(
            f"This is a multi-panel figure. Digitize ONLY panel {panel} "
            "(1-based, reading order: top-left first, left to right, then down "
            "a row) and ignore every other panel."
        )
    resolved = _resolve_calibration(calibration, panel)
    if not resolved:
        return hints
    if resolved.get("x_quantity") or resolved.get("y_quantity"):
        hints.append(
            "A human has already confirmed these axis identities -- use these "
            "exact strings rather than reading the axis titles yourself: "
            f"x_axis.quantity={resolved.get('x_quantity', '')!r}, "
            f"x_axis.unit={resolved.get('x_unit', '')!r}, "
            f"y_axis.quantity={resolved.get('y_quantity', '')!r}, "
            f"y_axis.unit={resolved.get('y_unit', '')!r}."
        )
    if resolved.get("x_range"):
        lo, hi = resolved["x_range"]
        hints.append(
            f"A human has confirmed the x-axis runs from {lo} to {hi} at the "
            "plot frame's left/right edges."
        )
    if resolved.get("y_range"):
        lo, hi = resolved["y_range"]
        hints.append(
            f"A human has confirmed the y-axis runs from {lo} to {hi} at the "
            "plot frame's bottom/top edges."
        )
    if resolved.get("frame"):
        x0, y0, x1, y1 = resolved["frame"]
        hints.append(
            "Restrict extraction to the sub-region of this crop given as "
            "fractions of the crop's own bounding box (0,0 = top-left, "
            f"1,1 = bottom-right): x0={x0}, y0={y0}, x1={x1}, y1={y1}."
        )
    return hints


def _step1_block(known_kind: str | None) -> str:
    """Step 1 either asks the model to probe for vector data, or -- when S8's
    own crop-time detection already knows the answer -- tells it outright and
    skips straight to the relevant step. Reading that off `Crop.has_vector`/
    `has_raster` (already computed, for free, during `crops`) instead of
    making the agent rediscover it saves several turns on every figure.
    """
    if known_kind == "vector":
        return (
            "### Step 1 -- vector data confirmed\n\n"
            "This crop was already scanned during cropping and found to contain "
            "real vector paths (no embedded raster image in the plot area). Skip "
            "straight to Step 2 -- do not spend turns re-probing for this."
        )
    if known_kind == "raster":
        return (
            "### Step 1 -- raster image confirmed\n\n"
            "This crop was already scanned during cropping and found to be a flat "
            "embedded raster image (no vector paths in the plot area). Skip "
            "straight to Step 3 -- do not spend turns re-probing for this."
        )
    return (
        "### Step 1 -- probe for vector data\n\n"
        "Check whether the plot area contains real vector paths/lines/marker "
        "glyphs (`page.get_drawings()` in PyMuPDF), or is one flat embedded "
        "raster image.\n\n"
        "- Vector paths present: extract each series directly from its own path "
        "objects' vertices/marker centers (Step 2) -- far more accurate than "
        "pixel tracing.\n"
        "- One flat raster image: fall back to pixel-calibrated digitization "
        "(Step 3).\n"
        "- A crop can mix both; inspect the actual plot region, not just the page."
    )


def _prompt(figure_id: str, record_id: str, caption: str,
            calibration: dict | None, panel: int | None,
            known_kind: str | None = None, retry_note: str | None = None) -> str:
    hints = _calibration_hints(calibration, panel)
    hint_block = (
        "\n\nHuman-supplied hints for this figure:\n" + "\n".join(f"- {h}" for h in hints)
        if hints else ""
    )
    retry_block = f"\n\n{retry_note}\n" if retry_note else ""

    return f'''{retry_block}Extract data points from ONE cropped figure taken from a scientific paper on multiphase heat transfer, for a structured catalog.

## Input


The current directory contains exactly one file, `figure.pdf` -- a tightly cropped region of one page of the source paper, including its caption. Open it with a PDF library (PyMuPDF/fitz is available) to inspect it -- do not guess from the caption alone.

Caption: "{caption}"
Figure id: {figure_id}
Record id: {record_id}
{hint_block}

This figure was already identified, from its caption among every figure in this paper, as the paper's primary boiling-curve comparison plot -- so it should genuinely be a chart of heat flux against wall superheat/temperature with one curve per surface tested, including a plain/reference one if the paper has one.

## Task

Confirm it actually is that kind of plot by opening it -- do not simply trust the caption-based selection. If it turns out not to be a data plot after all (a photograph, an SEM/TEM micrograph, a schematic diagram, a table), write `{{"series": []}}` to `output.json` and stop -- do not fabricate axes for a non-plot image.

If it is a plot, extract every distinguishable curve/series:

{_step1_block(known_kind)}

### Step 2 -- vector-path extraction (preferred)

- Get the pixel/point positions of axis tick marks and match them to their printed labels (from the PDF's text layer) to calibrate x and y independently.
- Separate series by stroke/fill color and, where distinguishable, dash pattern or marker glyph shape.
- Read (x, y) coordinates directly off each series' path vertices/marker centers, then convert through the calibration.
- Sample enough points to represent the full curve shape, not just endpoints.
- Set `"uncertainty": {{"method": "vector_path_extraction"}}`.

### Step 3 -- raster digitization (fallback)

- Rasterize at high resolution (300-600 dpi).
- Calibrate the pixel-to-data mapping from tick mark positions and their printed labels (read them visually from the image if there's no text layer for them).
- Isolate each series by its legend color/marker where the legend is distinguishable; otherwise trace visually.
- Convert traced pixel positions through the calibration. Sample enough points to represent the curve shape.
- Set `"uncertainty": {{"method": "pixel_calibrated_digitization"}}` (or `"manual_visual_digitization"` if you read values by eye rather than any pixel-color mask).

### Step 4 -- axis identity

Read each axis's physical quantity and unit from its printed title.

{_QUANTITY_HINTS}
If an axis title is genuinely unreadable but you can still read its tick values, leave that axis's `quantity` as `""` rather than guessing -- do not invent a title you cannot see.

### Step 5 -- indistinguishable series

If two near-identical series (repeated runs, hysteresis branches) can't be reliably told apart, merge them into one curve and say so in its `notes` -- never fabricate a false split. When in doubt about a whole curve, leaving it out (returning fewer series) is strongly preferred to inventing points.

## Output contract

Write your result as JSON to `output.json` in the current directory, matching EXACTLY this shape (this is `schema/point.schema.json`'s per-series contract):

```json
{{
  "series": [
    {{
      "series_id": "{figure_id}-<short-slug-for-this-curve>",
      "figure_id": "{figure_id}",
      "label": "<the curve's legend text as printed, or its color/marker if there is no legend text>",
      "x_axis": {{"quantity": "<see Step 4>", "unit": "<unit as printed>", "scale": "linear or log"}},
      "y_axis": {{"quantity": "<see Step 4>", "unit": "<unit as printed>", "scale": "linear or log"}},
      "points": [[x1, y1], [x2, y2]],
      "uncertainty": {{"method": "vector_path_extraction | pixel_calibrated_digitization | manual_visual_digitization"}},
      "notes": "<which color/marker isolated this curve, why this method, any merges, anything a reviewer should know>"
    }}
  ]
}}
```

Write ONLY `output.json`. Do not modify `figure.pdf`. Do not install any packages -- use only what is already available (PyMuPDF/fitz, numpy, Pillow, matplotlib if you need to render a preview for yourself).

Work efficiently: write one script that does the calibration, extraction, and JSON-writing together and run it, rather than many small exploratory tool calls. Iterate only as much as you need to get a calibration that actually fits and points that actually match the plot -- you have a limited number of turns.
'''


def _known_kind(has_vector: bool | None, has_raster: bool | None) -> str | None:
    """Collapse S8's own has_vector/has_raster flags into a Step-1 shortcut.

    Only a clean vector-only or raster-only crop gets a shortcut -- a crop
    with both (or with neither flag known) still gets the full probing
    instructions, since "which one actually holds the plot" is exactly the
    ambiguous case Step 1 exists to resolve.
    """
    if has_vector and not has_raster:
        return "vector"
    if has_raster and not has_vector:
        return "raster"
    return None


def digitize_figure(
    pdf_path: str | Path,
    page_no: int,
    bbox,
    figure_id: str = "fig",
    caption: str = "",
    record_id: str = "",
    calibration: dict | None = None,
    panel: int | None = None,
    model: str | None = None,
    has_vector: bool | None = None,
    has_raster: bool | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 1,
) -> list[dict]:
    """Digitize one figure region via an agentic Claude Code CLI call.

    `has_vector`/`has_raster` (from S8's `Crop`, already computed for free
    during `crops`) let a known-vector-only or known-raster-only figure skip
    Step 1's self-discovery and use a smaller turn/effort budget -- vector
    extraction is comparatively mechanical, and re-deriving something we
    already know for free just burns turns. A mixed or unknown crop keeps the
    full budget: that ambiguity is real and worth spending turns on. A
    `panel` restriction forfeits that shortcut even on an otherwise-known-
    vector crop: isolating one panel's series out of a figure that stacks
    several overlapping plots is real work, not the mechanical "read the
    paths" case the shortcut assumes.

    Returns PointSeries dicts (point.schema.json). Raises NeedsCalibration
    when the mechanism itself fails (no backend, the call errored or timed
    out, or the output didn't validate) -- a figure the model judged not to
    be a plot instead comes back as `[]`, which is not an error.

    Mechanism failures (no output written, a timeout, unparsable JSON, or a
    schema-invalid series) are retried automatically up to `retries` times,
    each with a bumped turn budget and a prompt that names what went wrong
    last time and asks for a faster, narrower attempt -- this is the
    automated stand-in for a human noticing `needs_calibration.json` and
    re-running the CLI with `--figure`/`--panel`/`--calib` by hand.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for digitization: pip install pymupdf")
    binary = shutil.which("claude")
    if not binary:
        raise NeedsCalibration(
            f"{figure_id}: the claude CLI is not on PATH -- prompt-based "
            "digitization needs it",
            {"figure_id": figure_id, "reason": "no_backend"},
        )

    kind = _known_kind(has_vector, has_raster)
    # Vector extraction is comparatively mechanical -- but isolating one
    # panel's series out of a figure that stacks several overlapping plots
    # is real work, not that mechanical case, so a panel restriction
    # forfeits the reduced *vector* budget specifically (raster was never
    # given the shortcut budget in the first place). Step 1's probing skip
    # (telling the model outright whether vector paths exist) still helps
    # regardless of panel, so `known_kind` itself stays unaffected below.
    use_vector_shortcut_budget = kind == "vector" and panel is None

    last_err: NeedsCalibration | None = None
    attempt = 0
    while True:
        tmp = Path(tempfile.mkdtemp(prefix="mhtdb-digitize-"))
        try:
            # A single-figure, single-page scratch PDF, so the agent's working
            # directory holds exactly the material it's allowed to read.
            src = fitz.open(str(pdf_path))
            page = src[page_no - 1]
            clip = fitz.Rect(*bbox) & page.rect
            out = fitz.open()
            out.insert_pdf(src, from_page=page_no - 1, to_page=page_no - 1)
            out[0].set_cropbox(clip)
            out.save(tmp / "figure.pdf")
            out.close()
            src.close()

            # Vector extraction is comparatively mechanical (read coordinates
            # off paths that already exist) -- a known-vector, single-panel
            # crop gets a smaller budget than the genuinely open-ended raster
            # or multi-panel case, which can need real iteration to get a
            # calibration that actually fits.
            resolved_turns = max_turns if max_turns is not None else (
                VECTOR_MAX_TURNS if use_vector_shortcut_budget else DEFAULT_MAX_TURNS
            )
            resolved_effort = effort or (
                VECTOR_EFFORT if use_vector_shortcut_budget else DEFAULT_EFFORT
            )

            retry_note = None
            if attempt > 0:
                reason = last_err.detail.get("reason", "unknown") if last_err else "unknown"
                retry_note = (
                    f"### RETRY {attempt} of {retries}\n\n"
                    f"A previous attempt at this same figure failed for a mechanical "
                    f"reason ({reason}), not because the figure wasn't a plot -- it ran "
                    "out of turns/time before writing output.json, or wrote something "
                    "that didn't parse or validate. This attempt has a larger turn "
                    "budget, but still work fast: write and run ONE script that does "
                    "calibration, extraction, and JSON-writing together, rather than "
                    "many small exploratory tool calls. If this figure stacks multiple "
                    "lettered sub-panels and you are not told to restrict to one, "
                    "extract ONLY the panel whose axes are heat flux (or CHF) vs wall "
                    "superheat/temperature -- ignore the other panel(s) entirely. If "
                    "you are running low on turns, write output.json with whatever "
                    "complete series you have so far rather than nothing."
                )
                if last_err and last_err.detail.get("reason") == "invalid_schema":
                    retry_note += (
                        f"\n\nThe previous output.json failed schema validation: "
                        f"{last_err.detail.get('problems')}. Fix these specific problems."
                    )
                # A mechanical failure means the prior budget itself was
                # likely too tight -- give the retry more room rather than
                # repeating the same limit and expecting a different result.
                resolved_turns = max(resolved_turns, DEFAULT_MAX_TURNS) + 15 * attempt
                resolved_effort = DEFAULT_EFFORT

            prompt = _prompt(
                figure_id, record_id, caption, calibration, panel,
                known_kind=kind, retry_note=retry_note,
            )
            cmd = [
                binary, "-p", prompt,
                "--output-format", "json",
                "--effort", resolved_effort,
                "--permission-mode", "bypassPermissions",
                "--max-turns", str(resolved_turns),
            ]
            if model:
                cmd += ["--model", model]

            try:
                proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                                      encoding="utf-8", timeout=timeout)
            except subprocess.TimeoutExpired:
                last_err = NeedsCalibration(
                    f"{figure_id}: digitization timed out after {timeout}s",
                    {"figure_id": figure_id, "reason": "timeout"},
                )
                if attempt >= retries:
                    raise last_err
                attempt += 1
                continue

            out_path = tmp / "output.json"
            if not out_path.exists():
                last_err = NeedsCalibration(
                    f"{figure_id}: model did not write output.json "
                    f"(exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}",
                    {"figure_id": figure_id, "reason": "no_output"},
                )
                if attempt >= retries:
                    raise last_err
                attempt += 1
                continue

            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                last_err = NeedsCalibration(
                    f"{figure_id}: output.json was not valid JSON",
                    {"figure_id": figure_id, "reason": "bad_json"},
                )
                if attempt >= retries:
                    raise last_err
                attempt += 1
                continue

            series = payload.get("series", [])
            problems: dict[str, list[str]] = {}
            for s in series:
                probs = validate_series(s)
                if probs:
                    problems[s.get("series_id", "?")] = probs
            if problems:
                last_err = NeedsCalibration(
                    f"{figure_id}: model output failed schema validation: {problems}",
                    {"figure_id": figure_id, "reason": "invalid_schema", "problems": problems},
                )
                if attempt >= retries:
                    raise last_err
                attempt += 1
                continue
            return series
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
