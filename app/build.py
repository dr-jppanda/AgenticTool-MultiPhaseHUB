"""Compile catalog/records/*.json into a self-contained dashboard.

    python app/build.py                       # -> app/dist/index.html
    python app/build.py --mode server         # -> fetches data/catalog.json instead

The standalone build inlines the catalog, so the output opens by double-click
(no server, no CORS). The server build emits the same page plus a separate
data/catalog.json for when this moves behind FastAPI — the catalog JSON is the
contract either way, so the migration is a deploy change, not a rewrite.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "catalog" / "records"
POINTS = ROOT / "catalog" / "points"
QUEUE = ROOT / "catalog" / "review" / "queue.json"
DIST = ROOT / "app" / "dist"
PAPERS = ROOT / "papers"

# Facets rendered in the sidebar, in order. `tree` ones get a collapsible
# parent/child tree; the rest are flat lists.
FACETS = [
    ("phenomenon", "Phenomenon", True),
    ("fluid", "Working fluid", True),
    ("surface_enhancement", "Surface", True),
    ("configuration", "Configuration", True),
    ("application", "Application", True),
    ("measured_quantity", "Measures", False),
    ("derived_tags", "Derived tags", False),
    ("method", "Method", True),
]

NUMERIC = [
    ("q_flux", "Heat flux", "W/m²"),
    ("dT_wall", "Wall superheat", "K"),
    ("D_h", "Char. dimension", "m"),
    ("p_sat", "Pressure", "Pa"),
    ("G", "Mass flux", "kg/m²s"),
    ("contact_angle", "Contact angle", "°"),
    ("Ra_surface", "Roughness Ra", "m"),
    ("dT_sub", "Subcooling", "K"),
]


def _picks(rec: dict, key: str) -> list[dict]:
    """Normalize a facet into a list of pick dicts."""
    tax = rec.get("taxonomy", {}) or {}
    if key == "application":
        return [t for t in (rec.get("application", {}) or {}).get("targets", []) or []
                if isinstance(t, dict)]
    node = tax.get(key)
    if node is None:
        return []
    picks = node if isinstance(node, list) else [node]
    return [p for p in picks if isinstance(p, dict)]


def _facet_values(rec: dict, key: str) -> tuple[list[str], list[str]]:
    """Return (all values, unconfirmed values) for one facet."""
    if key in ("derived_tags", "measured_quantity"):
        # Deterministic: derived tags come from binning, measures are a flat
        # closed list. Neither is a model judgement call, so neither is gated.
        vals = (rec.get("derived_tags", []) if key == "derived_tags"
                else (rec.get("taxonomy", {}) or {}).get("measured_quantity", []))
        return list(vals), []

    vals, unconf = [], []
    for p in _picks(rec, key):
        t1, t2 = p.get("tier1"), p.get("tier2")
        confirmed = p.get("confirmed", True)
        if t1:
            vals.append(t1)
            if not confirmed:
                unconf.append(t1)
        if t2 and t2 != t1:
            path = f"{t1} / {t2}"
            vals.append(path)
            if not confirmed:
                unconf.append(path)
        if p.get("propose_new"):
            v = f"⊕ {p['propose_new']}"
            vals.append(v)
            unconf.append(v)
    return vals, unconf


def _evidence_items(rec: dict) -> list[dict]:
    """Flatten every evidence span with the field path that owns it."""
    items: list[dict] = []

    def walk(node, path):
        if isinstance(node, dict):
            if "quote" in node and "resolved" in node:
                items.append({
                    "field": path,
                    "quote": node["quote"],
                    "pages": node.get("pages", []),
                    "sections": [s.get("number") or s.get("title", "")
                                 for s in node.get("sections", [])],
                    "resolved": node.get("resolved", False),
                    "match": node.get("match", ""),
                })
                return
            for k, v in node.items():
                walk(v, path if k in ("evidence", "also_at") else (f"{path}.{k}" if path else k))
        elif isinstance(node, list):
            for v in node:
                walk(v, path)

    for section in ("conditions", "taxonomy", "application"):
        walk(rec.get(section, {}), section)
    return items


def compile_catalog() -> dict:
    records = []
    for p in sorted(RECORDS.glob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        facets, unconfirmed = {}, []
        for key, _, _ in FACETS:
            vals, unconf = _facet_values(r, key)
            facets[key] = sorted(set(vals))
            unconfirmed += unconf

        numeric = {}
        for field, *_ in NUMERIC:
            lo = r.get("si", {}).get(f"{field}_min")
            hi = r.get("si", {}).get(f"{field}_max")
            vals = [v for v in (lo, hi) if v is not None]
            if vals:
                numeric[field] = [min(vals), max(vals)]

        cond = r.get("conditions", {})
        gate_notes = [
            {"field": k, "reason": pk.get("gate_reason", "")}
            for k, _, _ in FACETS
            for pk in _picks(r, k)
            if pk.get("confirmed") is False
        ]

        records.append({
            "id": r["record_id"],
            "title": r.get("title", r["record_id"]),
            "pdf": r.get("source_pdf", ""),
            "extractor": r.get("extractor", "?"),
            "n_pages": r.get("n_pages", 0),
            "n_figures": r.get("n_figures", 0),
            "cited_pages": r.get("cited_pages", []),
            "facets": facets,
            "unconfirmed": sorted(set(unconfirmed)),
            "gate_notes": gate_notes,
            "numeric": numeric,
            "derived": {k: round(v, 4) for k, v in r.get("derived", {}).items()},
            "raw_conditions": {
                k: {"min": v.get("min"), "max": v.get("max"), "unit": v.get("unit")}
                for k, v in cond.items() if isinstance(v, dict) and "min" in v
            },
            "orientation": cond.get("orientation", "unspecified"),
            "heating_mode": cond.get("heating_mode", "unspecified"),
            "material": cond.get("surface_material"),
            "evidence": _evidence_items(r),
            "evidence_stats": r.get("evidence_stats", {}),
            "warnings": r.get("unit_warnings", []),
            "points_summary": r.get("points_summary"),
            "figures": r.get("figures", []),
        })

    queue = []
    if QUEUE.exists():
        try:
            queue = json.loads(QUEUE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            queue = []

    return {
        "records": records,
        "review": queue,
        "curves": compile_curves(),
        "facets": [{"key": k, "label": lbl, "tree": tree} for k, lbl, tree in FACETS],
        "numeric_meta": [{"field": f, "label": l, "unit": u} for f, l, u in NUMERIC],
    }


def compile_curves(max_points: int = 70) -> dict:
    """Digitized boiling curves, ready to draw: SI units, thinned, labelled.

    Reuses `curves.select_boiling_curves` rather than re-deriving the selection
    here, so the dashboard and the CSV agree on which series is a plain
    reference surface and on why each one was included. Points are thinned to
    a drawable count — a 400-point traced curve is indistinguishable from a
    70-point one at 760 px wide, and inlining all of them triples the page.
    """
    if not POINTS.exists():
        return {"series": [], "reference": None, "papers": []}
    sys.path.insert(0, str(ROOT))
    try:
        from mhtdb.curves import select_boiling_curves, rohsenow
    except Exception as exc:                       # CoolProp missing, say
        print(f"  curves panel skipped: {type(exc).__name__}: {exc}")
        return {"series": [], "reference": None, "papers": []}

    every, rejected = select_boiling_curves(include_all=True)
    plain, _ = select_boiling_curves()
    plain_ids = {s.series_id for s in plain}
    # First rejection reason per record -- e.g. an uncalibrated axis -- so the
    # legend can say *why* a digitized paper isn't plotted instead of the
    # generic "wrong axes" guess, which is wrong whenever the axes matched
    # fine and it was the units that didn't convert.
    rejected_reason = {}
    for r in rejected:
        rejected_reason.setdefault(r.record_id, r.reason)

    out = []
    for sel in every:
        pts = sel.points
        if len(pts) > max_points:
            step = len(pts) / max_points
            pts = [pts[min(len(pts) - 1, int(i * step))] for i in range(max_points)]
        out.append({
            "record": sel.record_id,
            "figure": sel.figure_id,
            "series": sel.series_id,
            "label": sel.label or sel.series_id.rsplit("-", 1)[-1],
            "plain": sel.series_id in plain_ids,
            "reason": sel.reason,
            "source": sel.source_type,
            # [wall superheat K, heat flux W/m2]
            "pts": [[round(a, 3), round(b, 1)] for a, b in pts],
        })
    out.sort(key=lambda s: (s["record"], s["figure"], s["series"]))

    # Every catalogued paper appears in the legend, plotted or not, with the
    # reason it is absent. A legend that lists only what happens to have data
    # answers "which papers are in this comparison?" with silence.
    plotted = {s["record"] for s in out}
    with_points = {p.stem.replace(".points", "") for p in POINTS.glob("*.points.json")}
    papers = []
    for rec_path in sorted(list(RECORDS.glob("*.json"))
                           + list((ROOT / "catalog" / "pointers").glob("*.json"))):
        rid = rec_path.stem
        pending = ROOT / "pipeline" / "figures" / rid / "needs_calibration.json"
        n_pending = 0
        if pending.exists():
            try:
                n_pending = len(json.loads(pending.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                n_pending = 0
        if rid in plotted:
            status, note = "plotted", ""
        elif rid in with_points:
            status = "other_axes"
            note = rejected_reason.get(rid, "digitized, but no wall-superheat/heat-flux axes")
        elif n_pending:
            status, note = "needs_calibration", f"{n_pending} figure(s) await axis calibration"
        else:
            status, note = "no_points", "no digitized points yet"
        papers.append({"id": rid, "status": status, "note": note})

    reference = None
    if out:
        hi = max(p[0] for s in out for p in s["pts"])
        grid = [0.5 + i * (max(20.0, hi) - 0.5) / 79 for i in range(80)]
        q, props = rohsenow(grid, fluid="water")
        reference = {
            "pts": [[round(t, 3), round(v, 1)] for t, v in zip(grid, q)],
            "source": props["source"],
            "label": "Rohsenow, water on copper (c_sf 0.013)",
        }
    return {"series": out, "reference": reference, "papers": papers}


def build(mode: str = "standalone") -> Path:
    data = compile_catalog()
    DIST.mkdir(parents=True, exist_ok=True)

    # Keep the dashboard self-contained: every catalog paper ships beside the
    # generated page, so a citation can open inside the app at its cited page.
    paper_dist = DIST / "papers"
    paper_dist.mkdir(exist_ok=True)
    copied = 0
    for name in sorted({r["pdf"] for r in data["records"] if r.get("pdf")}):
        source = PAPERS / name
        if not source.exists():
            continue
        dest = paper_dist / name
        # Same size as what's already there -- almost certainly last run's
        # copy of this same source file. Skipping avoids re-touching a file
        # a PDF viewer or file-indexer may currently have open (a locked
        # destination is a transient Windows nuisance, not a build error).
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            copied += 1
            continue
        try:
            shutil.copy2(source, dest)
            copied += 1
        except PermissionError as e:
            print(f"warning: could not copy {name} into {paper_dist} "
                  f"({e}) -- it may be open in another program; the dashboard "
                  f"will link to it, but that link will 404 until it's copied",
                  file=sys.stderr)

    if mode == "server":
        (DIST / "data").mkdir(exist_ok=True)
        (DIST / "data" / "catalog.json").write_text(json.dumps(data), encoding="utf-8")
        payload, loader = "null", "fetch('data/catalog.json').then(r=>r.json()).then(d=>{DATA=d;boot();});"
    else:
        payload, loader = json.dumps(data, ensure_ascii=False), "boot();"

    out = DIST / "index.html"
    out.write_text(TEMPLATE.replace("__DATA__", payload).replace("__LOADER__", loader),
                   encoding="utf-8")
    n_ev = sum(len(r["evidence"]) for r in data["records"])
    print(f"built {out}  ({len(data['records'])} records, {n_ev} evidence spans, "
          f"{copied} papers, {len(data['review'])} in review, "
          f"{out.stat().st_size/1024:.0f} KB)")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MHT-DataHub</title>
<style>
:root{
  color-scheme: light;
  --sidebar-w:258px;
  --bg:#fbfbfa;        --panel:#ffffff;      --raise:#f6f6f4;
  --ink:#16161a;       --ink2:#5b5b63;       --ink3:#8e8e97;
  --line:#e6e5e1;      --line2:#d9d8d3;
  --accent:#2a78d6;    --accent-soft:#eaf1fc;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --s4:#eda100; --s5:#e87ba4; --s6:#008300;
  --s7:#4a3aa7; --s8:#e34948;
  --s9:#0f8fa8; --s10:#a05a18; --s11:#9c27b0; --s12:#6b8e00;
  --warn:#8a5a00;      --warn-bg:#fdf4e3;
  --shadow: 0 1px 2px rgb(16 16 20 / .04), 0 6px 18px -10px rgb(16 16 20 / .18);
  --r:11px;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --bg:#0e0e10;      --panel:#17171a;      --raise:#1f1f23;
    --ink:#f2f2f4;     --ink2:#a9a9b2;       --ink3:#77777f;
    --line:#26262b;    --line2:#33333a;
    --accent:#5c9bea;  --accent-soft:#182739;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70;
    --s4:#c98500; --s5:#d55181; --s6:#008300;
  --s7:#9085e9; --s8:#e66767;
  --s9:#1b9fb8; --s10:#c47331; --s11:#c05bd0; --s12:#7fa300;
    --s7:#9085e9; --s8:#e66767;
  --s9:#1b9fb8; --s10:#c47331; --s11:#c05bd0; --s12:#7fa300;
    --s9:#1b9fb8; --s10:#c47331; --s11:#c05bd0; --s12:#7fa300;
    --warn:#f0c579;    --warn-bg:#2b2311;
    --shadow: 0 1px 2px rgb(0 0 0 / .4), 0 8px 24px -12px rgb(0 0 0 / .7);
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --bg:#0e0e10; --panel:#17171a; --raise:#1f1f23;
  --ink:#f2f2f4; --ink2:#a9a9b2; --ink3:#77777f;
  --line:#26262b; --line2:#33333a;
  --accent:#5c9bea; --accent-soft:#182739;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --s4:#c98500; --s5:#d55181; --s6:#008300;
  --s7:#9085e9; --s8:#e66767;
  --s9:#1b9fb8; --s10:#c47331; --s11:#c05bd0; --s12:#7fa300;
  --warn:#f0c579; --warn-bg:#2b2311;
  --shadow: 0 1px 2px rgb(0 0 0 / .4), 0 8px 24px -12px rgb(0 0 0 / .7);
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased;letter-spacing:-0.006em}
button,input{font:inherit;color:inherit}
body.resizing{cursor:col-resize;user-select:none}
body.resizing iframe{pointer-events:none}
::selection{background:var(--accent-soft)}

/* ── top bar ─────────────────────────────────────────── */
header{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:14px;
  padding:0 16px;height:52px;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:saturate(180%) blur(14px);border-bottom:1px solid var(--line)}
.brand{font-weight:600;font-size:16px;letter-spacing:-.02em;white-space:nowrap}
.brand span{color:var(--ink3);font-weight:450}
.grow{flex:1}
.srch{position:relative;min-width:180px;max-width:340px;flex:1}
.srch input{width:100%;height:32px;padding:0 10px 0 30px;border-radius:9px;
  border:1px solid var(--line);background:var(--panel);outline:none}
.srch input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.srch::before{content:"";position:absolute;left:10px;top:9px;width:13px;height:13px;
  border:1.6px solid var(--ink3);border-radius:50%;pointer-events:none}
.srch::after{content:"";position:absolute;left:20px;top:20px;width:6px;height:1.6px;
  background:var(--ink3);transform:rotate(45deg);pointer-events:none}
.tb{display:flex;gap:2px;padding:2px;background:var(--raise);border-radius:9px}
.tb button{height:26px;padding:0 10px;border:0;background:transparent;border-radius:7px;
  cursor:pointer;color:var(--ink2);font-size:14px}
.tb button[aria-pressed=true]{background:var(--panel);color:var(--ink);box-shadow:var(--shadow)}
.icon{height:30px;width:30px;display:grid;place-items:center;border:1px solid var(--line);
  background:var(--panel);border-radius:9px;cursor:pointer;color:var(--ink2)}
.icon:hover{border-color:var(--line2);color:var(--ink)}
.pill{display:inline-flex;align-items:center;gap:5px;height:26px;padding:0 9px;
  border-radius:99px;background:var(--warn-bg);color:var(--warn);font-size:13.5px;
  border:0;cursor:pointer;white-space:nowrap}

/* ── shell ───────────────────────────────────────────── */
.shell{display:grid;grid-template-columns:var(--sidebar-w) 8px minmax(0,1fr);align-items:start}
@media(max-width:860px){.shell{grid-template-columns:1fr}}
aside{position:sticky;top:52px;max-height:calc(100vh - 52px);overflow:auto;
  padding:14px 10px 40px}
.side-resizer{position:sticky;top:52px;z-index:20;width:8px;height:calc(100vh - 52px);
  padding:0;border:0;background:transparent;cursor:col-resize;touch-action:none}
.side-resizer::after{content:"";position:absolute;inset:0 auto 0 3px;width:1px;
  background:var(--line);transition:width .12s,background .12s,box-shadow .12s}
.side-resizer:hover::after,.side-resizer:focus-visible::after,.resizing .side-resizer::after{
  width:2px;background:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.side-resizer:focus-visible{outline:none}
@media(max-width:860px){aside{position:static;max-height:none;border-right:0;
  border-bottom:1px solid var(--line)}.side-resizer{display:none}}
main{padding:16px 18px 60px;min-width:0}
aside::-webkit-scrollbar{width:8px}
aside::-webkit-scrollbar-thumb{background:var(--line2);border-radius:8px}

/* ── facet tree ──────────────────────────────────────── */
.grp{margin-bottom:6px}
.grp>h3{margin:10px 0 3px;padding:0 6px;font-size:12px;font-weight:650;
  letter-spacing:.08em;text-transform:uppercase;color:var(--ink3)}
.row{display:flex;align-items:center;border-radius:8px;min-height:27px;
  transition:background .12s}
.row:hover{background:var(--raise)}
.row.on{background:var(--accent);color:#fff}
.row.on .n{color:#fff;opacity:.75}
.row.dim{opacity:.36}
.tw{width:18px;flex:none;border:0;background:none;cursor:pointer;color:inherit;
  opacity:.5;font-size:11px;line-height:1;padding:0}
.tw:hover{opacity:1}
.row .lbl{flex:1;min-width:0;display:flex;align-items:center;gap:6px;border:0;
  background:none;cursor:pointer;text-align:left;padding:3px 6px 3px 0;color:inherit;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.n{margin-left:auto;font-size:12.5px;color:var(--ink3);font-variant-numeric:tabular-nums;
  padding-right:7px}
.dot{width:5px;height:5px;border-radius:50%;flex:none}

/* ── numeric ─────────────────────────────────────────── */
.rng{padding:3px 6px 8px}
.rng label{display:flex;justify-content:space-between;gap:8px;font-size:13px;
  color:var(--ink2);margin-bottom:3px}
.rng label b{font-weight:500;color:var(--ink3);font-variant-numeric:tabular-nums}
.rng input{width:100%;accent-color:var(--accent);height:14px}

/* ── record rows ─────────────────────────────────────── */
.list{display:flex;flex-direction:column;gap:6px}
.rec{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  overflow:hidden;transition:border-color .12s,box-shadow .12s}
.rec:hover{border-color:var(--line2)}
.rec[open]{box-shadow:var(--shadow);border-color:var(--line2)}
.rec>summary{list-style:none;cursor:pointer;padding:11px 14px;display:grid;gap:5px}
.rec>summary::-webkit-details-marker{display:none}
.tt{font-size:16px;font-weight:600;line-height:1.4;letter-spacing:-.012em}
.sub{display:flex;flex-wrap:wrap;gap:8px;font-size:13px;color:var(--ink3)}
.sub b{font-weight:500;color:var(--ink2)}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.c{font-size:12.5px;line-height:1.1;padding:5px 8px;border-radius:6px;white-space:nowrap;
  background:var(--raise);color:var(--ink2);border:1px solid transparent}
.c.key{background:var(--accent-soft);color:var(--accent)}
/* dashed = the model inferred it and it has not been confirmed.
   "AI guessed" vs "verified" has to be visible at a glance. */
.c.unc{background:transparent;border:1px dashed var(--line2);color:var(--ink3)}
.c.num{font-variant-numeric:tabular-nums}

.body{padding:0 14px 14px;display:grid;gap:14px}
.sec>h4{margin:0 0 7px;font-size:12px;font-weight:650;letter-spacing:.07em;
  text-transform:uppercase;color:var(--ink3)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 16px;font-size:14px}
.kv dt{color:var(--ink3)}
.kv dd{margin:0;font-variant-numeric:tabular-nums}
.ev{position:relative;padding:10px 36px 10px 13px;border-left:2px solid var(--line);
  border-radius:0 8px 8px 0;margin-bottom:3px;cursor:pointer;transition:background .12s}
.ev:hover,.ev:focus-visible{background:var(--raise);outline:none}
.ev::after{content:"Open paper ↗";position:absolute;right:10px;top:10px;color:var(--accent);
  font-size:12px;opacity:0;transform:translateX(-2px);transition:opacity .12s,transform .12s}
.ev:hover::after,.ev:focus-visible::after{opacity:1;transform:none}
.ev.bad{border-left-color:var(--s2)}
.ev .fp{font:12.5px ui-monospace,SFMono-Regular,monospace;color:var(--accent)}
.ev q{display:block;margin:3px 0 5px;color:var(--ink2);quotes:'“' '”'}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 10px;
  font-size:13px}
.empty{padding:56px 0;text-align:center;color:var(--ink3)}

/* ── panels & charts ─────────────────────────────────── */
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:13px 15px;margin-bottom:10px}
.panel h2{margin:0;font-size:14px;font-weight:650;letter-spacing:-.01em}
.panel .cap{margin:2px 0 10px;font-size:13px;color:var(--ink3)}
#stats,#curves{max-width:75%;zoom:.75}
.strip{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.stat{flex:1;min-width:104px;background:var(--panel);border:1px solid var(--line);
  border-radius:var(--r);padding:9px 12px}
.stat .v{font-size:22px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat .k{font-size:12.5px;color:var(--ink3);margin-top:1px}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:13px;color:var(--ink2);margin-top:8px}
.legend i{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:5px}
.coverage-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;
  padding:15px 17px 0}
.coverage-head .cap{margin:3px 0 0}
.coverage-count{flex:none;padding:5px 9px;border:1px solid var(--line);border-radius:99px;
  background:var(--raise);color:var(--ink2);font-size:12.5px;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--ink3);font-weight:650;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
td.num{font-variant-numeric:tabular-nums;text-align:right}
.scroll{overflow-x:auto}
.rv{display:grid;gap:8px}
.rvi{display:grid;gap:3px;padding:9px 11px;background:var(--raise);border-radius:9px;
  border:1px dashed var(--line2)}
.rvi .h{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.rvi code{font:12.5px ui-monospace,monospace;color:var(--ink3);background:var(--panel);
  padding:2px 5px;border-radius:5px}

/* ── in-app paper reader ─────────────────────────────── */
.paper-viewer{position:fixed;inset:0;z-index:80;display:grid;place-items:center;padding:24px;
  background:rgb(10 12 16 / .58);backdrop-filter:blur(8px)}
.paper-viewer[hidden]{display:none}
.paper-shell{width:min(1180px,96vw);height:min(900px,92vh);display:grid;
  grid-template-rows:auto minmax(0,1fr);overflow:hidden;background:var(--panel);
  border:1px solid var(--line2);border-radius:14px;box-shadow:0 28px 80px rgb(0 0 0 / .35)}
.paper-bar{display:flex;align-items:center;gap:12px;min-height:58px;padding:9px 12px 9px 16px;
  border-bottom:1px solid var(--line);background:var(--panel)}
.paper-meta{min-width:0;flex:1}
.paper-meta strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-size:15px;line-height:1.35}
.paper-meta span{display:block;color:var(--ink3);font-size:12.5px}
.paper-actions{display:flex;align-items:center;gap:6px;flex:none}
.paper-action{display:inline-flex;align-items:center;justify-content:center;min-height:34px;
  padding:0 11px;border:1px solid var(--line);border-radius:8px;background:var(--raise);
  color:var(--ink2);font-size:13px;text-decoration:none;cursor:pointer}
.paper-action:hover{border-color:var(--line2);color:var(--ink)}
.paper-frame{width:100%;height:100%;border:0;background:var(--raise)}
.page-link{border-color:color-mix(in srgb,var(--accent) 24%,var(--line));color:var(--accent)}
@media(max-width:700px){
  .paper-viewer{padding:0}.paper-shell{width:100vw;height:100vh;border:0;border-radius:0}
  .paper-action.open-new{display:none}.ev::after{display:none}.ev{padding-right:13px}
}
/* ── boiling curves ──────────────────────────────────── */
.curve-panel{position:relative}
.curve-controls{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0 10px}
.curve-controls .tb{flex:0 0 auto}
.curve-controls .note{color:var(--ink3);font-size:12.5px;margin-left:auto}
.curve-stage{position:relative;max-width:80%}
.curve-chart{width:100%;height:auto;display:block;touch-action:none}
.curve-line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.cv-series.dim{opacity:.22}
.cv-series{transition:opacity .08s}
.curve-dot{stroke:var(--panel);stroke-width:2}
.curve-dot.flat{stroke:none}
.curve-ref{fill:none;stroke:var(--ink2);stroke-width:1.6;stroke-dasharray:6 5;opacity:.75}
.curve-tag{font-size:11px;fill:var(--ink2);paint-order:stroke;stroke:var(--panel);
  stroke-width:3.5px;stroke-linejoin:round}
.curve-cross{stroke:var(--line2);stroke-width:1;stroke-dasharray:3 4}
.grid{stroke:var(--line);stroke-width:1}
.axis-box{fill:none;stroke:var(--line2);stroke-width:1.2}
.axis-label{fill:var(--ink2)}
.tick{fill:var(--ink3)}
.curve-tip{position:absolute;pointer-events:none;z-index:5;min-width:170px;max-width:280px;
  padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--panel);
  box-shadow:var(--shadow);font-size:12.5px;line-height:1.45}
.curve-tip b{display:block;font-weight:600;margin-bottom:2px}
.curve-tip .muted{color:var(--ink3)}
.curve-tip .val{font-variant-numeric:tabular-nums}
.curve-empty{color:var(--ink2);font-size:14px;margin:6px 0 0}
.curve-legend{display:flex;flex-wrap:wrap;gap:6px 10px;margin-top:10px}
.curve-legend .lg-item{display:inline-flex;align-items:center;gap:6px;padding:3px 9px 3px 5px;
  border:1px solid var(--line);border-radius:99px;font-size:12.5px;background:var(--panel);
  cursor:pointer;user-select:none;transition:background .08s,border-color .08s,color .08s}
.curve-legend .lg-item:hover{border-color:var(--accent);background:var(--accent-soft)}
.curve-legend .lg-item:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.curve-legend .lg-item.off{color:var(--ink3);border-style:dashed;background:transparent}
.curve-legend .lg-item.picked{background:var(--accent);border-color:var(--accent);color:#fff}
.curve-legend .lg-item.picked em{color:#fff}
.curve-legend .lg-item em{font-style:normal;color:var(--ink3);font-size:11.5px}
.curve-legend .lg-clear{font-weight:600;border-style:solid}
.lg-mark{width:14px;height:14px;flex:0 0 14px;overflow:visible}
</style>
</head>
<body>

<header>
  <div class="brand">MHT&#8209;DataHub <span id="hc"></span></div>
  <div class="srch"><input id="q" type="search" placeholder="Search titles, tags, quotes…"></div>
  <div class="grow"></div>
  <button class="pill" id="rvBtn" hidden></button>
  <div class="tb">
    <button id="vList" aria-pressed="true">List</button>
    <button id="vTable" aria-pressed="false">Table</button>
  </div>
  <button class="icon" id="theme" title="Toggle theme">◐</button>
  <button class="icon" id="reset" title="Clear filters">⟲</button>
</header>

<div class="shell">
  <aside id="side"></aside>
  <button class="side-resizer" id="sideResizer" type="button" role="separator"
    aria-label="Resize filter panel" aria-orientation="vertical"
    aria-valuemin="220" aria-valuemax="440" aria-valuenow="258"
    title="Drag to resize · double-click to reset"></button>
  <main>
    <div id="stats" class="strip"></div>
    <div id="review"></div>
    <div id="curves"></div>
    <div id="out"></div>
  </main>
</div>

<div class="paper-viewer" id="paperViewer" hidden role="dialog" aria-modal="true" aria-labelledby="paperTitle">
  <div class="paper-shell">
    <div class="paper-bar">
      <div class="paper-meta">
        <strong id="paperTitle">Paper</strong>
        <span id="paperLocation">Page 1</span>
      </div>
      <div class="paper-actions">
        <a class="paper-action open-new" id="paperNew" target="_blank" rel="noopener">Open separately</a>
        <button class="paper-action" id="paperClose" type="button" aria-label="Close paper">Close</button>
      </div>
    </div>
    <iframe class="paper-frame" id="paperFrame" title="Research paper"></iframe>
  </div>
</div>

<script>
let DATA = __DATA__;
const S = {facets:{}, ranges:{}, q:"", table:false, showReview:false};
const $ = s => document.querySelector(s);
const esc = s => String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const nice = s => String(s).replace(/_/g," ").replace(/ \/ /g," › ");

const SIDEBAR_KEY="mht.sidebar.width",SIDEBAR_MIN=220,SIDEBAR_MAX=440,SIDEBAR_DEFAULT=258;
const clampSidebar=v=>Math.min(SIDEBAR_MAX,Math.max(SIDEBAR_MIN,Math.round(v)));
function setSidebarWidth(value,save=false){
  const width=clampSidebar(value);
  document.documentElement.style.setProperty("--sidebar-w",`${width}px`);
  const handle=$("#sideResizer");
  if(handle) handle.setAttribute("aria-valuenow",width);
  if(save){try{localStorage.setItem(SIDEBAR_KEY,String(width));}catch{}}
  return width;
}
try{setSidebarWidth(+localStorage.getItem(SIDEBAR_KEY)||SIDEBAR_DEFAULT);}catch{}

function fmt(v){
  if(v==null) return "—";
  const a=Math.abs(v);
  return (a!==0&&(a<0.01||a>=1e5)) ? v.toExponential(2) : String(+v.toFixed(a<1?4:2));
}

/* ── filtering ───────────────────────────────────────── */
function keep(r){
  if(S.q){
    const hay=(r.title+" "+r.id+" "+r.evidence.map(e=>e.quote).join(" ")+" "+
      Object.values(r.facets).flat().join(" ")).toLowerCase();
    if(!hay.includes(S.q.toLowerCase())) return false;
  }
  for(const [k,sel] of Object.entries(S.facets)){
    if(!sel.size) continue;
    const vals=r.facets[k]||[];
    // A parent selection also matches its children ("pool_boiling" keeps
    // "pool_boiling / nucleate") — otherwise clicking a category hides
    // everything filed beneath it, which reads as data loss.
    if(![...sel].every(v => vals.some(x => x===v || x.startsWith(v+" / ")))) return false;
  }
  for(const [f,sel] of Object.entries(S.ranges)){
    const rg=r.numeric[f];
    if(!rg || rg[1]<sel[0] || rg[0]>sel[1]) return false;
  }
  return true;
}
const shown = () => DATA.records.filter(keep);

/* ── sidebar: tree ───────────────────────────────────── */
function tally(key, recs){
  const m={}; for(const r of recs) for(const v of (r.facets[key]||[])) m[v]=(m[v]||0)+1; return m;
}
function treeRow({id,label,count,depth,on,dim,unconf}){
  return `<div class="row ${on?"on":""} ${dim?"dim":""}" style="padding-left:${depth*13}px">
    <span class="tw"></span>
    <button class="lbl" data-sel="${esc(id)}">
      ${unconf?`<span class="dot" style="background:var(--s2)" title="includes unconfirmed"></span>`:""}
      <span style="overflow:hidden;text-overflow:ellipsis">${esc(label)}</span>
    </button><span class="n">${count}</span></div>`;
}

function renderSide(){
  const recs=shown();
  let h = `<div class="grp">${treeRow({
    id:"__all", label:"All datasets", count:DATA.records.length, depth:0,
    on:!Object.values(S.facets).some(s=>s.size), dim:false })}</div>`;

  for(const F of DATA.facets){
    const all=tally(F.key, DATA.records), live=tally(F.key, recs);
    const keys=Object.keys(all);
    if(!keys.length) continue;
    const sel=S.facets[F.key]||new Set();
    const unconfAny=new Set(DATA.records.flatMap(r=>r.unconfirmed||[]));

    let rows="";
    if(F.tree){
      // Sub-categories (tier 2) are still in every record's own data -- the
      // Table view's detail columns and search both read them -- but the
      // sidebar only ever filters by the main category, and the tier-2 rows
      // used to add a whole extra disclosure level for what was usually a
      // single-item list under it. A main category's own count already
      // includes every record filed under one of its sub-categories (each
      // pick contributes both strings), so collapsing to main-category-only
      // rows here changes nothing about which records a click matches.
      const parents=keys.filter(k=>!k.includes(" / "))
        .sort((a,b)=>(all[b]-all[a])||a.localeCompare(b));
      for(const p of parents){
        rows+=treeRow({id:F.key+"::"+p,label:nice(p),count:live[p]||0,depth:0,
          on:sel.has(p),dim:!(live[p]||0)&&!sel.has(p),unconf:unconfAny.has(p)});
      }
    } else {
      for(const k of keys.sort((a,b)=>(all[b]-all[a])||a.localeCompare(b)))
        rows+=treeRow({id:F.key+"::"+k,label:nice(k),count:live[k]||0,depth:0,
          on:sel.has(k),dim:!(live[k]||0)&&!sel.has(k)});
    }
    h+=`<div class="grp"><h3>${esc(F.label)}</h3>${rows}</div>`;
  }

  // numeric envelope
  let rng="";
  for(const m of DATA.numeric_meta){
    const all=DATA.records.map(r=>r.numeric[m.field]).filter(Boolean);
    if(!all.length) continue;
    const lo=Math.min(...all.map(a=>a[0])), hi=Math.max(...all.map(a=>a[1]));
    if(!(hi>lo)) continue;
    const cur=S.ranges[m.field]||[lo,hi];
    rng+=`<div class="rng"><label><span>${esc(m.label)}</span>
      <b>${fmt(cur[0])}–${fmt(cur[1])} ${esc(m.unit)}</b></label>
      <input type="range" data-lo="${esc(m.field)}" min="${lo}" max="${hi}" step="${(hi-lo)/200}" value="${cur[0]}">
      <input type="range" data-hi="${esc(m.field)}" min="${lo}" max="${hi}" step="${(hi-lo)/200}" value="${cur[1]}">
    </div>`;
  }
  if(rng) h+=`<div class="grp"><h3>Operating envelope</h3>${rng}
    <p style="font-size:11px;color:var(--ink3);padding:0 6px">Records missing a field drop
    out once you move its slider.</p></div>`;

  const side=$("#side"); side.innerHTML=h;

  side.querySelectorAll("[data-sel]").forEach(b=>b.onclick=()=>{
    const id=b.dataset.sel;
    if(id==="__all"){ S.facets={}; return render(); }
    const [k,v]=id.split("::");
    const set=S.facets[k]||(S.facets[k]=new Set());
    if(set.has(v)) set.delete(v); else set.add(v);
    render();
  });
  side.querySelectorAll("input[type=range]").forEach(sl=>sl.oninput=()=>{
    const f=sl.dataset.lo||sl.dataset.hi;
    const all=DATA.records.map(r=>r.numeric[f]).filter(Boolean);
    const lo=Math.min(...all.map(a=>a[0])), hi=Math.max(...all.map(a=>a[1]));
    const cur=S.ranges[f]||[lo,hi];
    if(sl.dataset.lo) cur[0]=Math.min(+sl.value,cur[1]); else cur[1]=Math.max(+sl.value,cur[0]);
    S.ranges[f]=cur; render();
  });
}

/* ── stats & charts ──────────────────────────────────── */
function renderStats(recs){
  const ev=recs.reduce((a,r)=>a+r.evidence.length,0);
  const ok=recs.reduce((a,r)=>a+r.evidence.filter(e=>e.resolved).length,0);
  const unc=recs.reduce((a,r)=>a+(r.unconfirmed||[]).length,0);
  const pg=new Set(recs.flatMap(r=>r.cited_pages.map(p=>r.id+":"+p))).size;
  $("#stats").innerHTML=[
    [recs.length,"datasets"],[`${ok}/${ev}`,"sources located"],
    [pg,"cited pages"],[unc,"unconfirmed"],
  ].map(([v,k])=>`<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
}

// Coverage · boiling curve plane widget removed (was: envelope(recs)) — it
// only ever plotted the handful of records with both a wall-superheat and
// heat-flux range recorded as text (dT_wall + q_flux), so it read as sparse
// once other papers were curated. #charts is now left unused/empty.





// Round tick values, not evenly-sliced ones: an axis reading 1.55 / 5.44 / 9.33
// makes a reader do arithmetic to place a point.
function niceTicks(lo,hi,target=5){
  if(!isFinite(lo)||!isFinite(hi)||hi<=lo) return [lo];
  const raw=(hi-lo)/target, mag=10**Math.floor(Math.log10(raw)), n=raw/mag;
  const step=(n<1.5?1:n<3?2:n<7?5:10)*mag;
  const first=Math.ceil(lo/step)*step, out=[];
  for(let v=first;v<=hi+step*1e-9;v+=step) out.push(+v.toFixed(10));
  return out.length?out:[lo,hi];
}
function decadeTicks(lo,hi){
  const out=[];
  for(let e=Math.floor(Math.log10(lo));e<=Math.ceil(Math.log10(hi));e++){
    const v=10**e; if(v>=lo*0.999&&v<=hi*1.001) out.push(v);
  }
  return out.length>1?out:niceTicks(lo,hi);
}

/* ── boiling curves ──────────────────────────────────── */
const CV={scope:"all", ylog:false, xwall:false, hover:null, pick:null};

// Colour follows the paper, not its rank in the current filter: assigning by
// position in the filtered list would repaint every surviving curve whenever a
// facet is toggled.
// Twelve hues, so every paper in this catalog that plots gets its own. The set
// is validated for both modes on the adjacent pairlist (worst CVD ΔE 9.1
// light / 8.4 dark, worst normal-vision ΔE 19.6 / 19.3); three of the light
// steps fall under 3:1 on white, which is why every curve also carries a
// direct label and its own marker shape.
const CURVE_COLORS=["var(--s1)","var(--s2)","var(--s3)","var(--s4)",
                    "var(--s5)","var(--s6)","var(--s7)","var(--s8)",
                    "var(--s9)","var(--s10)","var(--s11)","var(--s12)"];
// Shape is a second, independent channel. Six validated hues cannot name
// fifteen papers, and inventing a seventh hue is how palettes go colour-blind
// hostile — so hue cycles every six while the marker shape advances, and every
// paper gets a unique (hue, shape) pair. Shape also survives printing, and it
// is what carries identity for the three light-mode hues that sit under 3:1.
const CURVE_SHAPES=["circle","square","triangle","diamond","down","cross"];
// Index within the papers that actually have curves — not within the whole
// catalog. Ranking by catalog position would hand two plotted papers the same
// hue while spending distinct hues on papers that draw nothing. The list is
// derived from the full series set, never from the filtered view, so toggling a
// facet cannot repaint a curve.
function plottedPapers(){
  return [...new Set((DATA.curves?.series||[]).map(s=>s.record))].sort();
}
function paperIndex(rec){ return plottedPapers().indexOf(rec); }
function curveColor(rec){
  const i=paperIndex(rec);
  return i>=0?CURVE_COLORS[i%CURVE_COLORS.length]:"var(--ink3)";
}
function curveShape(rec){
  // Shape advances with the paper as a second, independent channel: three
  // papers read as circle / square / triangle, not three circles that happen
  // to differ in colour. It also carries identity where colour cannot — in
  // print, under colour-blindness, and for the three light-mode hues that sit
  // below 3:1 on white. Past twelve papers the hue would repeat, and the shape
  // offset keeps the pair unique.
  const i=paperIndex(rec); if(i<0) return "circle";
  const n=CURVE_SHAPES.length;
  return CURVE_SHAPES[(i+Math.floor(i/CURVE_COLORS.length))%n];
}
function markerPath(shape,x,y,r){
  switch(shape){
    case "square":   return `<rect x="${(x-r).toFixed(1)}" y="${(y-r).toFixed(1)}"
      width="${(2*r).toFixed(1)}" height="${(2*r).toFixed(1)}" rx="1"`;
    case "triangle": return `<polygon points="${x.toFixed(1)},${(y-r*1.15).toFixed(1)}
      ${(x+r).toFixed(1)},${(y+r*0.85).toFixed(1)} ${(x-r).toFixed(1)},${(y+r*0.85).toFixed(1)}"`;
    case "down":     return `<polygon points="${x.toFixed(1)},${(y+r*1.15).toFixed(1)}
      ${(x+r).toFixed(1)},${(y-r*0.85).toFixed(1)} ${(x-r).toFixed(1)},${(y-r*0.85).toFixed(1)}"`;
    case "diamond":  return `<polygon points="${x.toFixed(1)},${(y-r*1.2).toFixed(1)}
      ${(x+r*1.2).toFixed(1)},${y.toFixed(1)} ${x.toFixed(1)},${(y+r*1.2).toFixed(1)}
      ${(x-r*1.2).toFixed(1)},${y.toFixed(1)}"`;
    case "cross":    return `<path d="M${(x-r).toFixed(1)} ${y.toFixed(1)}H${(x+r).toFixed(1)}
      M${x.toFixed(1)} ${(y-r).toFixed(1)}V${(y+r).toFixed(1)}" stroke-width="2.1"`;
    default:         return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r.toFixed(1)}"`;
  }
}
function marker(shape,x,y,r,fill,cls){
  const stroke=shape==="cross"?`stroke="${fill}" fill="none"`:`fill="${fill}"`;
  return `<g class="${cls}">${markerPath(shape,x,y,r)} ${stroke}/></g>`;
}
function shortId(rec){const p=rec.split("-");return p[0]+" "+(p[1]||"");}

// Scope-filtered only (plain vs. all series) -- ignores the legend pick, so
// the legend can always show every in-scope paper as selectable even while
// one is isolated. curveSeries() below layers the pick on top for the chart
// itself; both read from this so scope and pick can never disagree about
// which papers exist to choose from.
function curveSeriesInScope(recs){
  const live=new Set(recs.map(r=>r.id));
  return (DATA.curves?.series||[])
    .filter(s=>live.has(s.record))
    .filter(s=>CV.scope==="all"||s.plain);
}
function curveSeries(recs){
  const inScope=curveSeriesInScope(recs);
  return CV.pick ? inScope.filter(s=>s.record===CV.pick) : inScope;
}

function curveLegendSwatch(rec,on){
  return `<svg class="lg-mark" viewBox="0 0 14 14" aria-hidden="true">${
    on ? marker(curveShape(rec),7,7,4.6,curveColor(rec),"")
       : `<circle cx="7" cy="7" r="4" fill="none" stroke="var(--ink3)" stroke-width="1.4"/>`}</svg>`;
}
// Every legend item is clickable, "on" (plotted, in scope) or not: picking
// one isolates its curve(s) in the chart above; picking the same one again
// (or the "Show all" chip that appears once something is picked) clears it.
// A paper with nothing to show in this scope is still clickable -- clicking
// it isolates it to *nothing*, which is exactly what its own dashed "not
// plotted" state already promises, rather than being a dead label.
function curveLegendHTML(papers,liveSet){
  const order=[...(papers||[])].sort((a,b)=>
    (liveSet.has(b.id)?1:0)-(liveSet.has(a.id)?1:0));
  const items=order.map(p=>{
    const on=liveSet.has(p.id);
    const picked=CV.pick===p.id;
    const why=on?"":` — ${p.note||"not plotted"}`;
    const cls=["lg-item",on?"":"off",picked?"picked":""].filter(Boolean).join(" ");
    return `<span class="${cls}" data-rec="${esc(p.id)}" role="button" tabindex="0"
      aria-pressed="${picked}" title="${esc((picked?"Showing only: ":"Show only: ")+p.id+(why?why:""))}">
      ${curveLegendSwatch(p.id,on)}${esc(shortId(p.id))}${on?"":`<em>${esc(p.status.replace(/_/g," "))}</em>`}</span>`;
  }).join("");
  const clear=CV.pick?`<span class="lg-item lg-clear" data-rec="" role="button" tabindex="0"
      title="Clear selection — show every paper">&times; Show all</span>`:"";
  return clear+items;
}

// Clicking a legend name isolates its curve(s); clicking it again (or the
// "Show all" chip) restores every paper. Re-renders the whole app, not just
// the curves panel -- CV.pick is curves-only state, but render() is already
// how every other control (view, scope, facets...) applies itself, so a
// second render path here would just be one more way for the panel to drift
// out of sync with everything else.
function pickCurvePaper(rec){
  CV.pick=(rec&&rec!==CV.pick)?rec:null;
  render();
}

function renderCurves(recs){
  const host=$("#curves"); if(!host) return;
  const data=DATA.curves||{series:[],reference:null,papers:[]};
  // "No papers at all" (nothing has ever been cropped/digitized) is a
  // different situation from "papers were digitized but none of their axes
  // could be calibrated" -- the latter still has a real reason worth
  // showing per paper, so only the former gets the generic setup message.
  if(!data.papers || !data.papers.length){
    host.innerHTML=`<div class="panel curve-panel"><div class="coverage-head"><div>
      <h2>Boiling curves · digitized</h2>
      <p class="cap">No digitized points in the catalog yet. Run
      <code>python -m mhtdb.pipeline crops</code> then <code>digitize</code>.</p>
      </div></div></div>`;
    return;
  }
  // inScope ignores the legend pick -- it is what decides which papers the
  // legend can offer to isolate. series layers the pick on top and is what
  // actually gets plotted, so a pick that empties the chart (e.g. the picked
  // paper has no series in the current scope) is distinguishable from scope
  // itself matching nothing.
  const inScope=curveSeriesInScope(recs);
  const series=curveSeries(recs);
  const W=760,H=390,P={l:78,r:104,t:22,b:56};
  const head=`<div class="coverage-head"><div>
      <h2>Boiling curves · digitized</h2>
      <p class="cap">Every point recovered from a figure, in SI. Hover for the paper,
      the series as its legend named it, and how the value was obtained. Click a
      name below to isolate its curve.</p>
      </div><span class="coverage-count">${series.length} curve${series.length===1?"":"s"}</span></div>`;
  // No scope/scale/axis toggles -- this panel always shows all series on a
  // linear heat-flux axis against wall superheat ΔT (CV's own defaults).
  const controls=`<div class="curve-controls">
      <span class="note">${esc(data.reference?data.reference.source:"")}</span>
    </div>`;

  if(!inScope.length||!series.length){
    const legend=curveLegendHTML(data.papers,new Set(inScope.map(s=>s.record)));
    const msg=!inScope.length
      ? `No digitized curve matches these filters.
         ${CV.scope==="plain"?"Only series whose legend names a plain reference surface are shown — switch to <em>All series</em>.":""}`
      : `${esc(shortId(CV.pick))} has no curve in this scope.
         ${CV.scope==="plain"?"It may only have non-reference series — try <em>All series</em>, or ":"Try "}<em>Show all</em> below.`;
    host.innerHTML=`<div class="panel curve-panel">${head}${controls}
      <p class="curve-empty">${msg}</p>
      <div class="curve-legend">${legend}</div></div>`;
    wireCurves();
    return;
  }

  const xOff=CV.xwall?100:0;
  const xs=series.flatMap(s=>s.pts.map(p=>p[0]+xOff));
  const ysAll=series.flatMap(s=>s.pts.map(p=>p[1]));
  const x0=Math.min(...xs),x1=Math.max(...xs);
  const y1=Math.max(...ysAll);
  const y0=CV.ylog?Math.max(1,Math.min(...ysAll.filter(v=>v>0))):0;
  const pad=(x1-x0)*.04||1;
  const xt=niceTicks(x0-pad,x1+pad);
  const XA=Math.min(x0-pad,xt[0]),XB=Math.max(x1+pad,xt[xt.length-1]);
  const px=v=>P.l+(v-XA)/(XB-XA||1)*(W-P.l-P.r);
  const L=v=>Math.log10(Math.max(v,1e-6));
  const py=v=>CV.ylog
    ? H-P.b-(L(v)-L(y0))/((L(y1)-L(y0))||1)*(H-P.t-P.b)
    : H-P.b-(v-y0)/((y1-y0)||1)*(H-P.t-P.b);

  const xTicks=xt;
  const yTicks=CV.ylog?decadeTicks(y0,y1)
    :niceTicks(y0/1e4,y1*1.02/1e4,4).map(v=>v*1e4);

  let s=`<svg class="curve-chart" viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Digitized boiling curves: heat flux against wall superheat">`;
  s+=`<rect x="${P.l}" y="${P.t}" width="${W-P.l-P.r}" height="${H-P.t-P.b}" rx="10" fill="var(--panel)"/>`;
  for(const v of xTicks){const x=px(v);
    if(x<P.l-1||x>W-P.r+1) continue;
    s+=`<line class="grid" x1="${x}" y1="${P.t}" x2="${x}" y2="${H-P.b}"/>
        <text class="tick" x="${x}" y="${H-P.b+20}" text-anchor="middle">${fmt(v)}</text>`;}
  for(const v of yTicks){const y=py(v);
    if(y<P.t-1||y>H-P.b+1) continue;
    s+=`<line class="grid" x1="${P.l}" y1="${y}" x2="${W-P.r}" y2="${y}"/>
        <text class="tick" x="${P.l-10}" y="${y+4}" text-anchor="end">${fmt(v/1e4)}</text>`;}
  s+=`<rect class="axis-box" x="${P.l}" y="${P.t}" width="${W-P.l-P.r}" height="${H-P.t-P.b}"/>
      <text class="axis-label" x="${P.l+(W-P.l-P.r)/2}" y="${H-13}" text-anchor="middle">${
        CV.xwall?"Wall temperature (°C, water at 1 atm)":"Wall superheat ΔT (K)"}</text>
      <text class="axis-label" x="20" y="${P.t+(H-P.t-P.b)/2}" text-anchor="middle"
        transform="rotate(-90 20 ${P.t+(H-P.t-P.b)/2})">Heat flux q″ (W/cm²${CV.ylog?" · log":""})</text>`;

  if(data.reference){
    const ref=data.reference.pts.filter(p=>p[0]+xOff>=XA&&p[0]+xOff<=XB&&p[1]<=y1*1.02&&(!CV.ylog||p[1]>0));
    if(ref.length>1){
      s+=`<path class="curve-ref" d="${ref.map((p,i)=>(i?"L":"M")+px(p[0]+xOff).toFixed(1)+" "+py(p[1]).toFixed(1)).join(" ")}"/>`;
      const last=ref[ref.length-1];
      s+=`<text class="curve-tag" x="${Math.min(px(last[0]+xOff)+6,W-P.r+96)}" y="${py(last[1])-2}">Rohsenow</text>`;
    }
  }

  const total=series.reduce((a,x)=>a+x.pts.length,0);
  const dense=total>900;
  const labelled=new Map();          // paper -> the series that carries its label
  series.forEach(ser=>{
    const best=labelled.get(ser.record);
    const top=x=>x.pts[x.pts.length-1][1];
    if(!best||top(ser)>top(series.find(s2=>s2.series===best))) labelled.set(ser.record,ser.series);
  });
  series.forEach(ser=>{
    const c=curveColor(ser.record);
    s+=`<g class="cv-series" data-s="${esc(ser.series)}">`;
    // Break the line where the data jumps. A digitized series is sorted by x,
    // so two runs of the same surface — or a stray mark the grouping could not
    // separate — otherwise get joined by a vertical spike that no measurement
    // supports. Points are all drawn; only the connecting line breaks.
    const jump=(y1-y0)*0.18;
    let d="",prev=null;
    ser.pts.forEach(p=>{
      const X=px(p[0]+xOff).toFixed(1),Y=py(p[1]).toFixed(1);
      d+=((prev===null||Math.abs(p[1]-prev)>jump)?"M":"L")+X+" "+Y+" ";
      prev=p[1];
    });
    s+=`<path class="curve-line" d="${d.trim()}" stroke="${c}"/>`;
    // Every point is drawn as a plain dot; the paper's shape is stamped at a
    // dozen positions along the curve. Shaping all 70 points would be an
    // unreadable smear, and ringing them all makes the line look dashed.
    const shape=curveShape(ser.record);
    if(dense){
      // One SVG node per point is honest but unaffordable: the whole corpus is
      // ~4,000 points, and a 340 KB panel re-rendered on every control click is
      // slow enough that the click lands on a node already being replaced. Past
      // the threshold the line plus its shaped markers carry the series, and
      // the hover layer still reads from the full point list.
    } else {
      ser.pts.forEach(p=>{
        s+=`<circle class="curve-dot flat" cx="${px(p[0]+xOff).toFixed(1)}"
          cy="${py(p[1]).toFixed(1)}" r="1.9" fill="${c}"/>`;});
    }
    const every=Math.max(1,Math.round(ser.pts.length/(dense?14:10)));
    ser.pts.forEach((p,i)=>{
      if(i%every&&i!==ser.pts.length-1) return;
      s+=marker(shape,px(p[0]+xOff),py(p[1]),3.6,c,"curve-dot");});

    // Direct label at the curve's end: the relief the light-mode palette needs,
    // and it keeps identity off colour alone. One per paper — two curves from
    // the same paper share a hue and would otherwise stamp the same words twice
    // on top of each other.
    if(ser.series===labelled.get(ser.record)){
      const end=ser.pts[ser.pts.length-1];
      s+=`<text class="curve-tag" x="${Math.min(px(end[0]+xOff)+7,W-6)}" y="${py(end[1])+4}"
        >${esc(shortId(ser.record))}</text>`;
    }
    s+=`</g>`;
  });

  s+=`<g id="cvOverlay" hidden>
        <line id="cvCross" class="curve-cross" y1="${P.t}" y2="${H-P.b}"/>
        <circle id="cvRing" r="6.5" fill="none" stroke-width="2"/>
      </g>`;
  s+=`<rect id="cvHit" x="${P.l}" y="${P.t}" width="${W-P.l-P.r}" height="${H-P.t-P.b}"
        fill="transparent" style="cursor:crosshair"/>`;

  const live=new Set(inScope.map(x=>x.record));
  const lg=curveLegendHTML(data.papers,live)
    +`<span class="lg-item"><svg class="lg-mark" viewBox="0 0 14 14" aria-hidden="true">
        <line x1="1" y1="7" x2="13" y2="7" stroke="var(--ink2)" stroke-width="1.8" stroke-dasharray="4 3"/>
      </svg>Rohsenow reference</span>`;

  host.innerHTML=`<div class="panel curve-panel">${head}${controls}
    <div class="curve-stage">${s}</svg><div class="curve-tip" id="cvTip" hidden></div></div>
    <div class="curve-legend">${lg}</div></div>`;
  wireCurves(series,px,py,xOff,W,H,P);
}

function curveTipHTML(h){
  return `<b>${esc(h.label||"series")}</b>
    <span class="muted">${esc(h.record)} · ${esc(h.figure)}</span><br>
    <span class="val">${fmt(h.dt)} K · ${fmt(h.q/1e4)} W/cm²</span><br>
    <span class="muted">${esc(h.source.replace(/_/g," "))}</span>`;
}

function wireCurves(series,px,py,xOff,W,H,P){
  const hit=$("#cvHit"); if(!hit||!series) return;
  const svg=hit.ownerSVGElement, tip=$("#cvTip"), ov=$("#cvOverlay"),
        cross=$("#cvCross"), ring=$("#cvRing");
  const groups=[...svg.querySelectorAll(".cv-series")];

  // Hover paints the overlay; it does not re-render the panel. Rebuilding a
  // 150 KB SVG on every pointermove is wasteful, and it detaches the very
  // nodes the pointer is over — the tooltip and the focused series survive as
  // attribute updates instead.
  const clear=()=>{
    ov.setAttribute("hidden","");tip.setAttribute("hidden","");
    groups.forEach(g=>g.classList.remove("dim"));
  };
  hit.onpointermove=ev=>{
    const r=svg.getBoundingClientRect();
    const mx=(ev.clientX-r.left)/r.width*W, my=(ev.clientY-r.top)/r.height*H;
    let best=null,bd=Infinity;
    series.forEach(sr=>sr.pts.forEach(p=>{
      const X=px(p[0]+xOff),Y=py(p[1]),d=(X-mx)**2+(Y-my)**2;
      if(d<bd){bd=d;best={x:X,y:Y,dt:p[0],q:p[1],record:sr.record,figure:sr.figure,
        series:sr.series,label:sr.label,source:sr.source};}
    }));
    if(!best||bd>42**2){clear();return;}
    ov.removeAttribute("hidden");
    cross.setAttribute("x1",best.x);cross.setAttribute("x2",best.x);
    ring.setAttribute("cx",best.x);ring.setAttribute("cy",best.y);
    ring.setAttribute("stroke",curveColor(best.record));
    groups.forEach(g=>g.classList.toggle("dim",g.dataset.s!==best.series));
    tip.innerHTML=curveTipHTML(best);
    const flip=best.x/W>0.62;
    tip.style.left=flip?"auto":(best.x/W*100)+"%";
    tip.style.right=flip?(100-best.x/W*100)+"%":"auto";
    tip.style.top=(best.y/H*100)+"%";
    tip.style.transform=`translate(${flip?"-10px":"10px"},-50%)`;
    tip.removeAttribute("hidden");
  };
  hit.onpointerleave=clear;
}

/* ── review ──────────────────────────────────────────── */
function renderReview(){
  const q=DATA.review||[];
  const btn=$("#rvBtn");
  btn.hidden=!q.length;
  btn.textContent=`${q.length} to review`;
  if(!S.showReview||!q.length){ $("#review").innerHTML=""; return; }
  $("#review").innerHTML=`<div class="panel"><h2>Review queue</h2>
    <p class="cap">Unconfirmed or new-vocabulary items. None of this blocks use —
    the values are already in the catalog, just marked unconfirmed.
    Decide from the CLI; rejections are remembered.</p>
    <div class="rv">${q.map(i=>`<div class="rvi">
      <div class="h"><b>${esc(i.proposed)}</b>
        <span style="color:var(--ink3);font-size:11.5px">${esc(i.kind.replace(/_/g," "))}</span></div>
      <div style="font-size:12px;color:var(--ink2)">${esc(i.reason)}</div>
      <div style="font-size:11.5px;color:var(--ink3)">${esc(i.record_id)} · ${esc(i.field)}</div>
      ${(i.evidence||[]).filter(e=>e.resolved).slice(0,1).map(e=>
        `<q style="font-size:12px;color:var(--ink2)">${esc(e.quote.slice(0,150))}</q>
         <span class="c num">p.${(e.pages||[]).join(", p.")}</span>`).join("")}
      <code>mhtdb review --accept '${esc(i.key)}'</code>
    </div>`).join("")}</div></div>`;
}

/* ── records ─────────────────────────────────────────── */
function recRow(r){
  const unc=new Set(r.unconfirmed||[]);
  const isUnc = v => unc.has(v) || [...unc].some(u => u===v || u.endsWith(" / "+v));
  const chip=(v,cls="")=>`<span class="c ${cls} ${isUnc(v)?"unc":""}"
     ${isUnc(v)?'title="model-inferred, not yet confirmed — see the review queue"':""}>${esc(nice(v))}</span>`;
  // Show the deepest term per facet — a parent whose child is also present is
  // redundant on a summary row. Proposed terms (⊕) always show: they are the
  // ones most in need of a human glance.
  const deepest = key => {
    const vs=r.facets[key]||[];
    return vs.filter(v => v.startsWith("⊕") || !vs.some(x => x!==v && x.startsWith(v+" / ")));
  };
  const leaf = v => v.startsWith("⊕") ? v : (v.includes(" / ") ? v.split(" / ")[1] : v);
  const head=[
    ...deepest("phenomenon").map(v=>chip(v,"key")),
    ...deepest("fluid").map(v=>chip(leaf(v))),
    ...deepest("surface_enhancement").map(v=>chip(leaf(v))),
    ...deepest("application").map(v=>chip(leaf(v))),
    ...(r.facets.derived_tags||[]).slice(0,3).map(v=>chip(v)),
  ].join("");

  const bad=r.evidence.filter(e=>!e.resolved).length;
  const cond=Object.entries(r.raw_conditions).map(([k,v])=>
    `<dt>${esc(k)}</dt><dd>${v.min===v.max?fmt(v.min):fmt(v.min)+" – "+fmt(v.max)} ${esc(v.unit||"")}</dd>`).join("");
  const der=Object.entries(r.derived).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${fmt(v)}</dd>`).join("");
  const ev=r.evidence.map(e=>`<div class="ev ${e.resolved?"":"bad"}" tabindex="0" role="link"
      aria-label="Open ${esc(r.title)} at page ${(e.pages||[])[0]||1}"
      data-paper="${esc(r.pdf)}" data-paper-title="${esc(r.title)}" data-paper-page="${(e.pages||[])[0]||1}">
      <div class="fp">${esc(e.field)}</div><q>${esc(e.quote)}</q>
      <div class="chips">
        ${e.pages.map(p=>`<span class="c num page-link" data-paper-page="${p}">p.${p}</span>`).join("")}
        ${e.sections.slice(0,2).map(s=>`<span class="c">§ ${esc(String(s).slice(0,32))}</span>`).join("")}
        ${e.resolved?`<span class="c">${esc(e.match)}</span>`
          :`<span class="c" style="color:var(--s2)">not found in PDF</span>`}
      </div></div>`).join("");

  return `<details class="rec"><summary>
      <div class="tt">${esc(r.title)}</div>
      <div class="sub"><span>${esc(r.pdf)}</span><span>${r.n_pages} pp</span>
        <span><b>${r.evidence.filter(e=>e.resolved).length}</b> sources</span>
        ${bad?`<span style="color:var(--s2)">${bad} unresolved</span>`:""}
        ${r.unconfirmed.length?`<span style="color:var(--warn)">${r.unconfirmed.length} unconfirmed</span>`:""}
        <span>${esc(r.extractor)}</span></div>
      <div class="chips">${head}</div>
    </summary>
    <div class="body">
      <div class="chips">
        ${r.points_summary?`<span class="c key">${r.points_summary.n_points} digitized points</span>`
          :`<span class="c">no digitized points</span>`}
        <span class="c num">pages ${r.cited_pages.join(", ")||"—"}</span>
        <span class="c">${esc(r.orientation)}</span><span class="c">${esc(r.heating_mode)}</span>
        ${r.material?`<span class="c">${esc(r.material)}</span>`:""}
        <span class="c">${r.n_figures} figures</span>
      </div>
      ${cond?`<div class="sec"><h4>Reported conditions · as printed</h4><dl class="kv">${cond}</dl></div>`:""}
      ${der?`<div class="sec"><h4>Derived · SI, computed</h4><dl class="kv">${der}</dl></div>`:""}
      ${r.warnings.length?`<div class="warn"><b>${r.warnings.length} plausibility warning(s)</b><br>${r.warnings.map(esc).join("<br>")}</div>`:""}
      <div class="sec"><h4>Sources</h4>${ev||"<p class='cap'>None.</p>"}</div>
    </div></details>`;
}

function renderTable(recs){
  const cols=DATA.numeric_meta.filter(m=>recs.some(r=>r.numeric[m.field]));
  return `<div class="panel scroll"><table><thead><tr>
    <th>Dataset</th><th>Phenomenon</th><th>Fluid</th><th>Surface</th><th>Src</th>
    ${cols.map(c=>`<th>${esc(c.label)}<br><span style="font-weight:400;text-transform:none">${esc(c.unit)}</span></th>`).join("")}
  </tr></thead><tbody>${recs.map(r=>`<tr>
    <td>${esc(r.title.slice(0,60))}</td>
    <td>${esc((r.facets.phenomenon||[]).filter(v=>v.includes(" / ")).map(nice).join(", ")||(r.facets.phenomenon||[]).map(nice).join(", "))}</td>
    <td>${esc((r.facets.fluid||[]).filter(v=>v.includes(" / ")).map(v=>v.split(" / ")[1]).join(", "))}</td>
    <td>${esc((r.facets.surface_enhancement||[]).filter(v=>v.includes(" / ")).map(v=>v.split(" / ")[1]).join(", "))}</td>
    <td class="num">${r.evidence.filter(e=>e.resolved).length}</td>
    ${cols.map(c=>{const v=r.numeric[c.field];
      return `<td class="num">${v?(v[0]===v[1]?fmt(v[0]):fmt(v[0])+"–"+fmt(v[1])):"—"}</td>`;}).join("")}
  </tr>`).join("")}</tbody></table></div>`;
}

function render(){
  const recs=shown();
  $("#hc").textContent=`· ${recs.length}/${DATA.records.length}`;
  renderSide(); renderStats(recs); renderReview();
  renderCurves(recs);
  $("#out").innerHTML = recs.length
    ? (S.table?renderTable(recs):`<div class="list">${recs.map(recRow).join("")}</div>`)
    : `<div class="empty">No dataset matches these filters.</div>`;
}

let paperReturnFocus=null;
function openPaper(pdf,page,title){
  if(!pdf) return;
  const safeName=String(pdf).split(/[\\/]/).pop();
  const targetPage=Math.max(1,parseInt(page,10)||1);
  const src=`papers/${encodeURIComponent(safeName)}#page=${targetPage}&zoom=page-width`;
  paperReturnFocus=document.activeElement;
  $("#paperTitle").textContent=title||safeName;
  $("#paperLocation").textContent=`Page ${targetPage} · ${safeName}`;
  $("#paperFrame").src=src;
  $("#paperNew").href=src;
  $("#paperViewer").hidden=false;
  document.body.style.overflow="hidden";
  $("#paperClose").focus();
}
function closePaper(){
  const viewer=$("#paperViewer");
  if(viewer.hidden) return;
  viewer.hidden=true;
  $("#paperFrame").src="about:blank";
  document.body.style.overflow="";
  paperReturnFocus?.focus?.();
}

function boot(){
  $("#q").oninput=e=>{S.q=e.target.value;render();};
  const setView=t=>{S.table=t;$("#vList").setAttribute("aria-pressed",!t);
    $("#vTable").setAttribute("aria-pressed",t);render();};
  $("#vList").onclick=()=>setView(false);
  $("#vTable").onclick=()=>setView(true);
  $("#rvBtn").onclick=()=>{S.showReview=!S.showReview;renderReview();};
  $("#reset").onclick=()=>{S.facets={};S.ranges={};S.q="";$("#q").value="";render();};
  $("#theme").onclick=()=>{
    const cur=document.documentElement.getAttribute("data-theme");
    const dark=matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.setAttribute("data-theme",cur?(cur==="dark"?"light":"dark"):(dark?"light":"dark"));
  };
  const resizer=$("#sideResizer");
  let resizeStart=null;
  resizer.onpointerdown=e=>{
    if(e.button!==0) return;
    const shellLeft=$(".shell").getBoundingClientRect().left;
    resizeStart={pointerId:e.pointerId,shellLeft};
    resizer.setPointerCapture(e.pointerId);
    document.body.classList.add("resizing");
    e.preventDefault();
  };
  resizer.onpointermove=e=>{
    if(!resizeStart||e.pointerId!==resizeStart.pointerId) return;
    setSidebarWidth(e.clientX-resizeStart.shellLeft);
  };
  const finishResize=e=>{
    if(!resizeStart||e.pointerId!==resizeStart.pointerId) return;
    resizeStart=null;
    document.body.classList.remove("resizing");
    setSidebarWidth(parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-w")),true);
  };
  resizer.onpointerup=finishResize;
  resizer.onpointercancel=finishResize;
  resizer.ondblclick=()=>setSidebarWidth(SIDEBAR_DEFAULT,true);
  resizer.onkeydown=e=>{
    const current=+resizer.getAttribute("aria-valuenow")||SIDEBAR_DEFAULT;
    const step=e.shiftKey?32:12;
    let next=null;
    if(e.key==="ArrowLeft") next=current-step;
    if(e.key==="ArrowRight") next=current+step;
    if(e.key==="Home") next=SIDEBAR_MIN;
    if(e.key==="End") next=SIDEBAR_MAX;
    if(next!=null){e.preventDefault();setSidebarWidth(next,true);}
  };
  $("#paperClose").onclick=closePaper;
  $("#paperViewer").onclick=e=>{if(e.target.id==="paperViewer") closePaper();};
  document.addEventListener("click",e=>{
    const pick=e.target.closest(".lg-item[data-rec]");
    if(pick){pickCurvePaper(pick.dataset.rec);return;}
    const link=e.target.closest("[data-paper]");
    if(!link) return;
    const pageTarget=e.target.closest("[data-paper-page]")||link;
    openPaper(link.dataset.paper,pageTarget.dataset.paperPage,link.dataset.paperTitle);
  });
  document.addEventListener("keydown",e=>{
    if(e.key==="Escape") return closePaper();
    if((e.key==="Enter"||e.key===" ")&&e.target.matches(".lg-item[data-rec]")){
      e.preventDefault();
      pickCurvePaper(e.target.dataset.rec);
      return;
    }
    if((e.key==="Enter"||e.key===" ")&&e.target.matches(".ev[data-paper]")){
      e.preventDefault();
      openPaper(e.target.dataset.paper,e.target.dataset.paperPage,e.target.dataset.paperTitle);
    }
  });
  render();
}
__LOADER__
</script>
</body>
</html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["standalone", "server"], default="standalone")
    build(ap.parse_args().mode)
