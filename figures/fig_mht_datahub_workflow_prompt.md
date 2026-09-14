# MHT-DataHub workflow figure specification

Create a polished, publication-quality illustrated technical workflow diagram in a wide landscape format (16:9, approximately 2048 x 1152). Use the attached reference image only as a visual-language and composition reference: soft pastel panels, bold black outlines, subtle diagonal hatching, dashed section borders, compact hand-drawn scientific icons, colored title ribbons, and strong directional arrows. Do not copy its content.

Title: **MHT-DataHub: Evidence-Grounded Literature-to-Dataset Workflow**

The composition must read clearly from left to right and contain four coordinated regions plus a footer. Keep all text horizontal, crisp, correctly spelled, and large enough to read. Prefer short labels over paragraphs. Do not invent stages or claims.

## Left column — Incremental research workspace

Show a vertical stack with looping arrows:

- **New Papers** — PDF icons entering `papers/`
- **Controlled Taxonomy** — `facets.yaml` and `binning.yaml`
- **Content-Addressed Cache** — document models and per-pass results
- **Incremental Update** — existing records stay unchanged; only new or affected passes rerun

This column feeds the central pipeline. Add a small loop labeled **add PDFs → run → rebuild app**.

## Central upper band — Document intelligence & structured extraction

Use a cool blue hatched panel and show:

1. **S0 PDF Ingest** — PDF becomes a **Document Model** containing normalized full text, page index, section index, tables, figure crops, and captions.
2. **Backend Router** — one hub connected to four choices: **Anthropic API**, **Claude Code**, **Codex CLI**, and **Offline Rules**.
3. Four narrow structured-output cards:
   - **S1 Triage** — own dataset? dataset record or pointer
   - **S2 Conditions** — raw ranges + printed units + verbatim quotes
   - **S3 Taxonomy** — controlled facets or `propose_new`
   - **S4 Application** — stated/inferred target, with evidence (no confidence score — the pipeline no longer tracks per-field confidence)

Show the paper text as a shared cached prefix feeding all four passes. Add a small badge: **Structured JSON**.

## Central lower band — Evidence grounding & deterministic science

Use a warm coral/orange hatched panel. Split it into two parallel modules that merge:

### S6 Verify (evidence grounding)

- exact normalized match, then anchored fuzzy fallback
- output: **verbatim quote → page(s) + section(s)**
- unlocatable evidence flows to a red **Reject / Review** quarantine
- optional small card: **LLM contradiction audit**

### S5 Normalize — deterministic science

- **Python only**
- units → SI
- plausibility gate / quarantine
- CoolProp fluid properties
- dimensionless groups: `Co`, `Bo`, `We`, `Fr`, `Re`, `Bl`, reduced pressure
- numeric binning → reproducible derived tags

Place a prominent equation-like design rule between the two modules:
**LLM extracts & cites → Python computes & classifies**

Merge both modules into **S7 Commit**.

## Right column — Quality control, catalog, figure pipeline, and app

Top green panel: **Review Queue & Taxonomy Feedback**

- tier1 "fundamental" facets → auto-confirmed
- stated by the paper → confirmed
- inferred / not stated → flagged for review, with a recorded `gate_reason`
- review queue never blocks use
- accept / reject; rejections remembered
- proposals grouped across papers
- dashed feedback arrow to **S3 Taxonomy** labeled **curate vocabulary; rerun affected pass only**
- dashed arrow to **S5 Normalize** labeled **renormalize; zero model calls**

(There is no numeric confidence threshold anymore — gating is binary: stated-and-confirmed vs. flagged-for-review.)

Middle database panel: **Versioned Catalog**

- `catalog/records/*.json`
- `catalog/pointers/*.json`
- **S8 Crops** — every figure/table cropped to its own PDF + PNG, matched to its caption
- **S9 Points** — one agentic `claude` CLI call per paper (tool use enabled) opens the chosen boiling-curve figure, reads vector paths directly or traces raster pixels against a calibrated axis, and writes `catalog/points/*.json`
- **S10 Curves** — point tables → CSV + a comparison plot with a CoolProp-backed Rohsenow reference line
- source of truth, diffable, cached, evidence-linked

Bottom gold/olive panel: **Interactive Research App**

- faceted filters and numeric range sliders
- **Coverage Map** — saturation temperature (K) on the x-axis, heat flux (W/m²) on the y-axis, with overlapping translucent dataset envelopes
- **Boiling Curves** — heat flux vs. wall superheat, every digitized series on one set of axes inside a drawn axis-box border, a CoolProp Rohsenow reference line, a log-flux toggle, a wall-temperature x-axis toggle, and on hover the paper, series name, and whether the value was read from vector paths or traced from pixels
- record detail + evidence quotes
- **Sources** (only this word; do not add "every value traced to the paper")
- clicking a source opens the in-app PDF viewer at the exact cited page
- resizable left sidebar
- build step: **catalog JSON → standalone dashboard**

## Footer invariants

Four icon badges across the bottom:

- **Evidence or nothing**
- **Never compute in the LLM**
- **Deterministic & reproducible**
- **Incremental & cached**

Use solid arrows for data flow and dashed arrows for human feedback/reprocessing. Make S0 through S10 visually trackable. Use a restrained palette of slate blue, dusty coral, sage green, warm gold, and off-white. Avoid gradients that reduce legibility. Avoid dense prose, tiny fonts, pseudo-code, decorative equations, or random unlabeled icons.
