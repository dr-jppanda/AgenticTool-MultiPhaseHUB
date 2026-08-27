# MHT-DataHub — Plan

> **Status (2026-08-11):** built and running end-to-end on 5 papers via the offline
> rule extractor. Provenance, normalization, and the dashboard are working; the LLM
> extraction stages are written but unexecuted (no API credentials in the build
> environment). See `README.md` for how to run it and what is known-limited.
> Deployment assumption below was confirmed: offline dashboard now, server later.

An automatically-labeled, searchable catalog of **multiphase heat transfer** datasets, where each
entry is grounded in its source paper and carries both a **normalized numeric operating envelope**
and a **multi-tier faceted tag set** derived from it.

**Decisions locked in (2026-08-11):**

| Question | Answer | Consequence |
|---|---|---|
| Consumer | Both, **numerics first** | LLM extracts numbers + evidence; tags are *derived* from numbers by deterministic rules, not generated |
| Record granularity | **Two-tier** — dataset record now, point table later | Schema reserves `points_ref`; labeling and digitization run on separate clocks |
| Assets in hand | **Paper only** (PDF or DOI) | Everything comes from text/tables/captions; evidence-grounding is mandatory |
| Scale | **~200–1000** datasets | SQLite + JSON-in-git. Postgres would be premature |
| Deployment | *not specified* — **assumed**: local dashboard → static export | Ingestion stays a CLI/agent process, never a web feature. Revisit if you want user submissions |

---

## 1. The central design decision

> **The LLM extracts and cites. Python computes and classifies.**

Every downstream error in a pipeline like this traces back to letting a language model do arithmetic
or invent vocabulary. So the boundary is hard:

- The **LLM** reads the paper and emits *raw measured quantities with their original units* plus a
  verbatim evidence span for each. It also picks facet values **from a closed controlled vocabulary**,
  or explicitly emits `propose_new` — it may never free-text a tag.
- **Python** converts units to SI, pulls fluid properties from CoolProp, computes dimensionless
  groups, and applies binning rules to produce derived tags.

This buys three things: tags are *reproducible* (regenerate the entire tag set with zero LLM calls),
dimensionless groups are *correct*, and a taxonomy change is a re-run of a deterministic function
rather than a re-labeling campaign.

---

## 2. Taxonomy: a faceted polyhierarchy, not one deep tree

A single 5-level tree forces bad choices (is "microchannel R134a subcooled flow boiling" filed under
geometry or under regime?). Instead: **independent facets, each 2–3 tiers deep**. A record carries a
path in each facet. This is far more searchable and much easier for a model to fill reliably, because
each extraction call targets a small enum rather than a 400-leaf tree.

### Facet A — Phenomenon (3 tiers, mutually exclusive path)

```
pool_boiling
  ├─ nucleate           → { isolated_bubble, fully_developed }
  ├─ transition
  ├─ film
  └─ quenching_transient
flow_boiling
  ├─ subcooled          → { onset_of_nucleate_boiling, partial, fully_developed }
  ├─ saturated          → { bubbly_slug, annular, dryout_post_dryout }
  └─ confined           → { microchannel_confined, slug_dominated }
condensation
  ├─ filmwise           → { in_tube, external_tube, plate, falling_film }
  ├─ dropwise
  ├─ direct_contact
  └─ with_noncondensables
evaporation
  ├─ thin_film
  ├─ spray_cooling
  ├─ jet_impingement
  └─ falling_film
adiabatic_two_phase     → { flow_regime_map, pressure_drop, void_fraction }
```

### Facet B — Measured quantity (flat, multi-valued)

`htc`, `chf`, `pressure_drop`, `void_fraction`, `flow_regime`, `boiling_curve`,
`wall_superheat`, `bubble_dynamics`, `nucleation_site_density`, `dryout_quality`,
`onb`, `rewetting_temperature`, `interfacial_area`

> **CHF is a measured quantity, not a phenomenon.** Filing it as a tier-1 phenomenon is a common
> modeling mistake — CHF is measured *within* pool boiling or flow boiling.

### Facet C — Configuration (2 tiers)

```
channel  → { circular_tube, rectangular, annulus, microchannel_array, minichannel, plate_heat_exchanger }
surface  → { flat_plate, wire, cylinder, sphere, tube_bundle }
device   → { heat_pipe, thermosyphon, vapor_chamber, cold_plate, spray_chamber }
```
Plus numeric fields: `D_h`, `L`, `L/D`, `aspect_ratio`, `n_channels`, `orientation`, `inclination_deg`.

### Facet D — Working fluid (2 tiers)

```
water | refrigerant_hfc | refrigerant_hfo | refrigerant_hcfc | natural (CO2, NH3, hydrocarbons)
| dielectric (FC-72, HFE-7100, Novec) | cryogen (LN2, LH2, LHe) | liquid_metal | nanofluid | mixture
```
Tier 2 is the specific fluid designation (`R134a`, `R1234ze(E)`, `FC-72`, …), which doubles as the
CoolProp lookup key.

### Facet E — Surface / enhancement (2 tiers)

```
plain | roughened | structured (fins, microfins, reentrant_cavities, pin_fins)
| porous_coating (microporous, sintered, foam) | nanostructured (nanowire, CNT, nanoparticle)
| wettability_engineered (hydrophobic, hydrophilic, biphilic, SLIPS)
```
Plus numerics: material, `Ra`, static/advancing/receding contact angle.

### Facet F — Application target (2 tiers) — **the hallucination hotspot**

```
electronics_thermal   → { datacenter, power_electronics, chip_cold_plate, immersion_cooling }
nuclear               → { pwr, bwr, smr, accident_loca, fuel_assembly_chf }
hvacr                 → { refrigeration, heat_pump, air_conditioning, chiller }
power_generation      → { steam_generator, condenser, orc, csp_receiver }
aerospace             → { spacecraft_loop, microgravity, cryogenic_propellant }
cryogenics            → { lng, lh2_storage, superconducting }
battery_thermal       → { ev_pack, immersion_cooled }
process               → { desalination_med_msf, evaporator, distillation }
fundamental           ← use when the paper states no application
```

This facet is usually **inferred**, not stated, so it gets special handling: it requires an evidence
span, carries an explicit confidence, and `fundamental` is an always-available, non-penalized escape.
A model asked to name an application will always name one; the schema has to make "none stated" the
easy answer.

### Facet G — Method / provenance

`experimental` | `numerical_cfd` (+ solver, interface method, mesh) | `correlation_only` | `review_compilation`
Measurement modality: `thermocouple`, `rtd`, `ir_thermography`, `high_speed_visualization`, `piv`,
`lif`, `x_ray`, `neutron_radiography`, `optical_interferometry`.

### Facet H — Operating envelope → **numeric, not tags**

Stored as `{min, max, unit_original, value_si, evidence}` per field:

`p_sat`, `p_reduced`, `T_sat`, `G` (mass flux), `q''` (heat flux), `x` (vapor quality),
`ΔT_sub`, `ΔT_wall`, `Re_lo`, `gravity_level`, `n_data_points`, `heating_mode`.

**Derived by Python from H + CoolProp — never by the LLM:**
Bond `Bo`, Confinement `Co = √(σ/gΔρ)/D_h`, Boiling number `Bl = q''/(G·h_fg)`,
Weber, Froude, `Re_lo`, reduced pressure.

### Derived tags (binning rules, `taxonomy/v1/binning.yaml`)

| Rule | Derived tag |
|---|---|
| `D_h < 1 mm` / `1–3 mm` / `> 3 mm` | `scale:microchannel` / `minichannel` / `conventional` |
| `Co > 0.5` (Kew & Cornwell) | `confinement:confined` |
| `p_reduced < 0.1` / `0.1–0.5` / `> 0.5` | `pressure:low` / `moderate` / `high` |
| `x < 0` | `regime:subcooled` |
| `gravity_level < 0.01 g` | `gravity:microgravity` |

Change a threshold → re-run `s5_normalize` → whole corpus re-tagged, zero LLM cost. **This is the
main payoff of "numerics first."**

---

## 3. Agentic pipeline

Patterns borrowed, and from where:

| Pattern | Source | Why here |
|---|---|---|
| Evidence-grounded extraction (quote or `null`) | RAG citation / attributed QA | Single biggest accuracy lever; enables a *deterministic* hallucination check |
| Constrained decoding over closed vocab | structured-output / grammar-constrained generation | Kills tag drift (`HPLC`/`hplc`/`high-performance…`) |
| Triage router before the expensive path | classifier-router agent designs | Reviews and correlation-only papers must not enter as datasets |
| Narrow multi-pass over one wide pass | schema-decomposition | Small output schemas are markedly more reliable than one 60-field blob |
| Extract → verify → reconcile | self-consistency / LLM-as-critic | Second pass hunts contradictions instead of re-guessing |
| Content-addressed caching | build systems | 1000 papers × prompt iterations; re-runs must be free |
| Inner/outer loop | autonomous-research two-loop designs | Inner = label a paper. Outer = evolve the taxonomy from accumulated proposals |
| Review queue for low confidence only | human-in-the-loop triage | You review ~10%, not 100% |

### Stages

```
S0  ingest      DOI/arXiv → OpenAlex/Crossref metadata + PDF;  PDF → text+tables (docling / GROBID
                + camelot for born-digital tables).  Content hash → cache key.
S1  triage      paper_type, contains_dataset?, primary phenomenon.  Cheap model.  GATE — a
                review_compilation exits here into `pointers/`, not `records/`.
S2  conditions  Facet H numeric envelope, original units + evidence span per field.
S3  taxonomy    Facets A–E, G from controlled vocab, or `propose_new` + rationale.
S4  application Facet F only, with confidence + evidence, `fundamental` freely available.
S5  normalize   DETERMINISTIC. units→SI, CoolProp properties, dimensionless groups, binning→tags.
S6  verify      Given record + paper: flag unsupported/contradicted fields.  Plus a *mechanical*
                check that every evidence quote literally occurs in the source text.
S7  commit      catalog/records/{id}.json (git) → rebuild SQLite index.
S8  points      LATER. Digitize operating points for a priority subset → point table.
```

Splitting S2/S3/S4 is deliberate: three ~15-field schemas beat one 60-field schema by a wide margin,
and it lets you re-run only the application pass when Facet F changes.

### Outer loop — taxonomy curation

Every ~100 papers: cluster the accumulated `propose_new` terms, review the top clusters, amend
`taxonomy/v2`, bump the version, re-run S3+S5 (S2 comes from cache — no re-extraction cost).
Taxonomy versions are recorded per record so you always know what vocabulary produced a label.

---

## 4. Do you need a harness? Yes — an *evaluation* harness

Not an agent harness. Claude Code already is that, and SCP's approach of adopting rather than
building one is the right call. What's needed is the thing SCP conspicuously lacked: a way to tell
whether a change helped.

`eval/` contains:

- **`gold/`** — 30–40 hand-labeled records, stratified across tier-1 phenomena plus deliberate edge
  cases: a review paper, a mixed-mode study, a microgravity study, an unusual fluid, a paper with no
  stated application.
- **Metrics**, per facet:
  - **Hierarchical F1**, not just exact match — right parent / wrong child deserves partial credit,
    and flat accuracy will mislead you about whether the tree is working.
  - Numeric: within-tolerance rate on range endpoints (±5%), reported separately from **null rate**
    (missed) and **unsupported rate** (asserted without valid evidence). Conflating these hides
    whether a prompt change made the model bolder or better.
  - **Evidence validity** — does the quoted span actually occur in the paper? Pure string check, no
    model needed, and it catches fabrication outright.
  - Facet F precision, tracked separately since it's the known weak point.
- **`run.py`** → a versioned scorecard keyed by (prompt version, schema version, taxonomy version).
  Regression gate: no taxonomy amendment lands without a scorecard.

Cost is roughly a day to build the gold set. It pays for itself the first time a taxonomy edit
silently degrades 200 records — which, at 1000 records and an evolving tree, will happen.

---

## 5. Storage and search

- **Source of truth:** `catalog/records/*.json`, one file per dataset, in git. Diffable, reviewable in
  PRs, no database lock-in. At this scale that is a feature, not a compromise.
- **Derived index:** SQLite — FTS5 over title/abstract/tags, a join table for facet filtering, plain
  columns for numeric range queries (`G_min <= :g AND G_max >= :g` is instant at 1000 rows), and an
  embedding column for semantic search. **Rebuilt from JSON, never hand-edited, gitignored.**
- Postgres/pgvector is unnecessary below a few thousand records and would add ops burden for nothing.

## 6. Frontend

At ≤1000 records the entire catalog is a few MB of JSON, so **search can run entirely client-side** —
no backend at all.

- **Phase 1:** local dashboard — faceted filters, numeric range sliders (G, q″, x, D_h, p_red), text
  search, record detail with evidence spans shown inline next to each extracted field. That last part
  matters: showing the evidence is what makes an auto-labeled catalog trustworthy to a domain reader.
- **Phase 2:** static export to GitHub Pages, or publish as an Artifact for sharing.
- **Phase 3 (defer):** FastAPI service, only if you want submissions through the UI.

A coverage view is worth building early — a 2D density plot over the (G, q″) or (x, D_h) plane per
phenomenon, showing where the corpus is dense and where it's empty. For an ML surrogate corpus that
map *is* the research finding, and it tells you which papers to ingest next.

---

## 7. Layout

```
mht-datahub/
├── PLAN.md                    ← this file
├── taxonomy/
│   ├── v1/{facets.yaml, binning.yaml, CHANGELOG.md}
│   └── proposals/             ← propose_new terms awaiting curation
├── schema/{record.schema.json, point.schema.json}
├── pipeline/
│   ├── s0_ingest.py … s7_commit.py
│   ├── prompts/               ← versioned, one per stage
│   └── cache/                 ← content-addressed, gitignored
├── catalog/
│   ├── records/*.json         ← source of truth, git-tracked
│   ├── pointers/*.json        ← review/correlation papers
│   └── index.sqlite           ← derived, gitignored
├── eval/{gold/, run.py, metrics.py, reports/}
├── app/{build.py, web/}
└── papers/                    ← PDFs, gitignored
```

---

## 8. Phasing

| Phase | Work | Exit criterion |
|---|---|---|
| **0** | Hand-label 10 papers. Write taxonomy v0.1 tiers 1–2 **by hand**. | A taxonomy you believe in |
| **1** | S0–S5 on those same 10; diff against hand labels; iterate prompts | Numeric envelope within tolerance on 8/10 |
| **2** | Eval harness; expand gold to 30–40 | Scorecard reproducible, evidence check passing |
| **3** | Batch 100–200. Curate proposals → taxonomy v0.2. Re-run S3+S5 | Hierarchical F1 ≥ 0.8 on facets A–E |
| **4** | Dashboard + coverage map | Searchable locally |
| **5** | Full corpus; S8 point digitization for priority subset | — |

**Phase 0 is not optional and the LLM does not do it.** Hand-designing tiers 1–2 from 10 real papers
is what prevents a taxonomy that looks reasonable and classifies nothing. Let the model propose
*leaves*; you own the *trunk*.

---

## 9. Known risks

| Risk | Mitigation |
|---|---|
| **Unit chaos** — W/cm² vs kW/m², bar/MPa/psia, G in lb/ft²·h | Normalization layer is mandatory, never optional; store original unit + SI side by side |
| **Application-target hallucination** | Evidence required; `fundamental` free; confidence tracked; scored separately in eval |
| **Duplicate data across papers** — reuse of PU-BTPFL, Groeneveld LUT | `derived_from` provenance field; dedup on (fluid, geometry, envelope) fingerprint |
| **Review/correlation papers entering as datasets** | S1 triage gate → `pointers/` |
| **Taxonomy drift across versions** | Version stamped per record; regression scorecard gates amendments |
| **Figure-only data** — many papers report envelopes only in plots | Accept `null` in Phase 1–4; defer to S8 digitization. Do *not* let the model estimate from captions |
| **Paywalled PDFs** | DOI path degrades to abstract-only; mark `extraction_completeness: partial` so it never silently looks like a full record |

---

## 10. Prior art to index rather than rebuild

These are consolidated compilations that already exist. The hub is most valuable as a **labeled index
over** them plus the primary literature — so `provenance: consolidated_db` is a first-class case.

- **PU-BTPFL** (Purdue, Mudawar) — large consolidated flow-boiling and condensation databases
- **Groeneveld CHF look-up table** (2006) — the standard tabulated CHF reference
- Microchannel flow-boiling compilations from the Bertsch / Tibiriçá–Ribatski lines of work
- **BubbleML** — simulation-derived multiphase dataset released through the NeurIPS datasets track

Verify current scope and licensing of each before ingesting; treat this list as leads, not settled facts.
