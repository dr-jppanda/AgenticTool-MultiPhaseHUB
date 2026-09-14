"""Pipeline driver: S0 -> S7, plus the point-ingestion entry point.

    python -m mhtdb.pipeline run papers/*.pdf            # LLM extraction
    python -m mhtdb.pipeline run --rules papers/*.pdf    # offline, no API key
    python -m mhtdb.pipeline ingest-points --record ID --from points.json
    python -m mhtdb.pipeline figures --record ID --out manifest.json
    python -m mhtdb.pipeline crops --all              # S8: one file per figure
    python -m mhtdb.pipeline digitize --record ID     # S9: figures -> points
    python -m mhtdb.pipeline curves --out out/        # points -> CSV + plot
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .docmodel import DocumentModel
from .normalize import normalize_record

_ROOT = Path(__file__).resolve().parent.parent
CATALOG = _ROOT / "catalog"
DOCCACHE = _ROOT / "pipeline" / "docmodels"
FIGURES = _ROOT / "pipeline" / "figures"
CALIB = _ROOT / "pipeline" / "calibrations"
PAPERS = _ROOT / "papers"


# ------------------------------------------------------- evidence resolution


def resolve_all_evidence(obj: Any, doc: DocumentModel, stats: dict) -> Any:
    """Walk the record, replacing every {"quote": ...} with a located entry.

    This is the mechanical half of S6: a quote that does not occur in the
    document is marked unresolved, which is the signal to discard or review the
    field. Pages and sections come from here and nowhere else.
    """
    if isinstance(obj, dict):
        if set(obj.keys()) == {"quote"} or (
            "quote" in obj and not {"pages", "sections"} & obj.keys()
        ):
            stats["total"] += 1
            loci = doc.locate(obj["quote"])
            if not loci:
                stats["unresolved"] += 1
                return {"quote": obj["quote"], "resolved": False, "pages": [], "sections": []}
            best = loci[0]
            stats["resolved"] += 1
            stats[best.match] = stats.get(best.match, 0) + 1
            entry = {"quote": obj["quote"], "resolved": True, **best.to_dict()}
            if len(loci) > 1:
                entry["also_at"] = [l.to_dict() for l in loci[1:]]
            return entry
        return {k: resolve_all_evidence(v, doc, stats) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_all_evidence(v, doc, stats) for v in obj]
    return obj


def collect_pages_sections(obj: Any, pages: set, sections: dict) -> None:
    """Aggregate every page/section the record cites, for the dashboard header."""
    if isinstance(obj, dict):
        if obj.get("resolved"):
            pages.update(obj.get("pages", []))
            for s in obj.get("sections", []):
                sections[s["id"]] = s
        for v in obj.values():
            collect_pages_sections(v, pages, sections)
    elif isinstance(obj, list):
        for v in obj:
            collect_pages_sections(v, pages, sections)


# --------------------------------------------------------------- record build


def _record_id(pdf: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", pdf.stem.lower()).strip("-")[:60]


_TITLE_NOISE = re.compile(
    r"^(abstract|doi|http|www|proceedings|copyright|©|received|accepted|"
    r"published|journal|vol\.?\s*\d|issn|acknowledg|keywords|contents lists|"
    r"available at|elsevier|springer|asme|ieee|downloaded|publications?$|"
    r"international journal|journal of|transactions of|preprint|"
    r"purdue|university|department|school of)",
    re.I,
)
_TITLE_TOPIC = re.compile(
    r"boiling|condens|evaporat|heat transfer|two-phase|two phase|flow|"
    r"nucleat|wetting|wettab|thermal|coolant|quench|chf|critical heat",
    re.I,
)


def _guess_title(doc: DocumentModel) -> str:
    """Best title-like line on page 1, scored rather than first-match.

    Heuristic; the LLM path takes the title from S1 instead, which is why
    Triage carries a `title` field.
    """
    page1_end = doc.pages[0].end if doc.pages else 4000
    best, best_score = None, 0.0

    for idx, line in enumerate(doc.text[:page1_end].split("\n")[:40]):
        s = " ".join(line.split())
        words = s.split()
        if not (20 <= len(s) <= 250) or len(words) < 4 or _TITLE_NOISE.match(s):
            continue
        if sum(c.isalpha() or c.isspace() for c in s) / len(s) < 0.75:
            continue

        score = 1.0
        score += 2.0 if _TITLE_TOPIC.search(s) else 0.0   # on-topic beats boilerplate
        score += min(len(words), 20) / 20.0               # titles are long-ish
        score -= 1.5 * (idx / 40.0)                       # earlier is likelier
        score -= 1.0 if re.search(r"\b(19|20)\d{2}\b", s) else 0.0   # dates = header
        score -= 1.0 if re.search(r"\d{3,}", s) else 0.0             # page/vol numbers
        score -= 0.5 if "," in s and len(words) < 12 else 0.0        # author line
        if score > best_score:
            best, best_score = s, score

    return best or Path(doc.source).stem.replace("_", " ")


def build_record(pdf: Path, use_rules: bool, verify_pass: bool = False,
                 backend=None) -> dict:
    from .s0_ingest import ingest_pdf, save_docmodel

    rid = _record_id(pdf)
    cached = DOCCACHE / f"{rid}.docmodel.json"
    if cached.exists():
        from .s0_ingest import load_docmodel

        doc = load_docmodel(cached)
        print(f"[{rid}] S0 cached ({len(doc.pages)}p, {len(doc.sections)} sections, {len(doc.figures)} figures)")
    else:
        doc = ingest_pdf(pdf, figures_dir=FIGURES / rid)
        save_docmodel(doc, cached)
        print(f"[{rid}] S0 ingested ({len(doc.pages)}p, {len(doc.sections)} sections, {len(doc.figures)} figures)")

    if use_rules:
        from .extract_rules import extract_rules

        body = extract_rules(doc)
    else:
        from . import extract as ex

        body = ex.run_all(doc, backend=backend)

    stats = {"total": 0, "resolved": 0, "unresolved": 0}
    body = resolve_all_evidence(body, doc, stats)

    pages: set[int] = set()
    sections: dict[str, dict] = {}
    collect_pages_sections(body, pages, sections)

    record = {
        "record_id": rid,
        "doc_id": doc.doc_id,
        # S1 reads the title off the page; the heuristic is only a fallback.
        "title": body.get("llm_title") or _guess_title(doc),
        "source_pdf": str(pdf.name),
        "n_pages": len(doc.pages),
        "n_figures": len(doc.figures),
        "figures": [{"id": f.id, "label": f.label, "page": f.page, "caption": f.caption[:200]} for f in doc.figures],
        "cited_pages": sorted(pages),
        "cited_sections": list(sections.values()),
        "evidence_stats": stats,
        "points_ref": None,
        **body,
    }

    record = normalize_record(record)

    # Review gating: mark each pick confirmed/unconfirmed and collect
    # anything a human should glance at. Never withholds a value — see gating.py.
    from .gating import gate_record

    pending = gate_record(record)
    if pending:
        print(f"  gating: {len(pending)} item(s) queued for review")

    if verify_pass and not use_rules:
        from . import extract as ex

        v = ex.verify(doc, record, backend=backend)
        record["verification"] = v.model_dump()
        print(f"[{rid}] S6 {v.overall}")

    return record


def write_record(record: dict) -> Path:
    is_pointer = record.get("routed_to") == "pointers"
    target = CATALOG / ("pointers" if is_pointer else "records")
    other = CATALOG / ("records" if is_pointer else "pointers")

    target.mkdir(parents=True, exist_ok=True)
    p = target / f"{record['record_id']}.json"
    p.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    # S1-S4 always re-extract (see extract.py's own disk cache for what makes
    # that free), which means this record is rebuilt from scratch every time
    # `run` sees this paper again -- silently dropping points_ref/points_summary
    # if S9 had already attached them in an earlier session, even though the
    # digitized points file is still sitting right there in catalog/points/.
    # Reattaching it here (pure JSON merge, no model call) is what keeps
    # "add more papers, rerun" from quietly erasing already-digitized data.
    if not is_pointer:
        points_path = CATALOG / "points" / f"{record['record_id']}.points.json"
        if points_path.exists():
            from .figure_points import ingest_points

            ingest_points(record["record_id"], points_path, catalog_dir=CATALOG)
            print(f"  reattached existing digitized points ({points_path.relative_to(_ROOT)})")

    # Triage can reclassify a paper between runs — a review first mistaken for a
    # dataset, say. Drop the counterpart so the same paper cannot appear as both
    # a dataset record and a pointer.
    stale = other / f"{record['record_id']}.json"
    if stale.exists():
        stale.unlink()
        print(f"  removed stale {stale.relative_to(_ROOT)}")
    return p


# ------------------------------------------------------------------- commands


def cmd_run(args) -> int:
    pdfs: list[Path] = []
    for pattern in args.pdfs:
        p = Path(pattern)
        pdfs.extend(sorted(p.parent.glob(p.name)) if any(c in pattern for c in "*?") else [p])
    pdfs = [p for p in pdfs if p.suffix.lower() == ".pdf" and p.exists()]
    if not pdfs:
        print("no PDFs matched", file=sys.stderr)
        return 1

    backend = None
    if not args.rules:
        from .backends import detect_backend

        backend = detect_backend(prefer=args.backend, model=args.model)
        print(f"backend: {backend.name} ({backend.model})\n")

    ok = 0
    for pdf in pdfs:
        try:
            rec = build_record(pdf, use_rules=args.rules, verify_pass=args.verify,
                               backend=backend)
            path = write_record(rec)
            s = rec["evidence_stats"]
            print(
                f"[{rec['record_id']}] -> {path.relative_to(_ROOT)}  "
                f"evidence {s['resolved']}/{s['total']} resolved, "
                f"tags={len(rec.get('derived_tags', []))}, pages={rec['cited_pages']}"
            )
            ok += 1
        except Exception as e:
            print(f"[{pdf.name}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    _refresh_derived_views()
    print(f"\n{ok}/{len(pdfs)} records written to {CATALOG / 'records'}")

    # The dashboard is a build artifact, not a running service — new records are
    # invisible until it is regenerated. Offer to do it here so the common case
    # ("I added papers, show me") is one command.
    if getattr(args, "build", False):
        import subprocess

        # Our stdout is buffered when piped; the child's is not. Flush first or
        # the build line surfaces above output that logically precedes it.
        sys.stdout.flush()
        if subprocess.run([sys.executable, str(_ROOT / "app" / "build.py")]).returncode:
            print("dashboard build failed", file=sys.stderr)
            return 1
    else:
        print("dashboard not rebuilt — run `python app/build.py`, or pass --build")
    return 0 if ok else 1


def _all_records() -> list[dict]:
    out = []
    for d in ("records", "pointers"):
        for p in sorted((CATALOG / d).glob("*.json")):
            out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def _refresh_derived_views() -> None:
    """Rebuild the review queue and proposal list across the whole catalog.

    Corpus-wide rather than per-paper, so a suggestion raised by three papers
    collapses into one queue row instead of three.
    """
    from .gating import collect_proposals, rebuild_queue

    recs = _all_records()
    q = rebuild_queue(recs)
    props = collect_proposals(recs)
    if q:
        print(f"\nreview queue: {len(q)} item(s)   python -m mhtdb.pipeline review")
    if props:
        print(f"vocab proposals: {len(props)}     python -m mhtdb.pipeline propose")


def cmd_backends(args) -> int:
    from .backends import DEFAULT_MODEL, describe_backends, detect_backend

    print("Model backends:")
    print(describe_backends())
    try:
        b = detect_backend()
        print(f"\nauto would select: {b.name}")
    except RuntimeError as e:
        print(f"\nauto would fail:\n{e}")
    print(f"\ndefault Anthropic model: {DEFAULT_MODEL}")
    print("default Codex model: configured Codex CLI default")
    print("override with --backend / --model, or the MHTDB_BACKEND env var")
    return 0


def cmd_review(args) -> int:
    """One row, two decisions. Anything that needs expanding or navigating
    turns a review queue into an inbox nobody opens."""
    from .gating import accept, load_queue, reject

    if args.accept:
        hit = accept(args.accept)
        print(f"accepted: {hit['proposed']}" if hit else f"no queue item with key {args.accept!r}")
        return 0 if hit else 1
    if args.reject:
        ok = reject(args.reject, args.note or "")
        print("rejected and remembered — it will not be suggested again" if ok
              else "already rejected")
        return 0

    queue = load_queue()
    if not queue:
        print("Review queue is empty.")
        return 0

    print(f"{len(queue)} item(s) awaiting review. Nothing here blocks use of the catalog.\n")
    for q in queue:
        also = q.get("also_in") or []
        where = q["record_id"] + (f" (+{len(also)} more)" if also else "")
        print(f"  [{q['kind']}]  {q['proposed']}")
        print(f"      {q['reason']}")
        print(f"      in {where} · field {q['field']}")
        for e in (q.get("evidence") or [])[:1]:
            if e.get("resolved"):
                pages = ",".join(f"p.{p}" for p in e.get("pages", []))
                print(f"      “{e['quote'][:110]}”  [{pages}]")
        print(f"      accept:  python -m mhtdb.pipeline review --accept {q['key']!r}")
        print(f"      reject:  python -m mhtdb.pipeline review --reject {q['key']!r}")
        print()
    return 0


def cmd_propose(args) -> int:
    """Vocabulary terms the model asked for that the taxonomy lacks."""
    from .gating import PROPOSALS_PATH, collect_proposals

    props = collect_proposals(_all_records())
    if not props:
        print("No pending vocabulary proposals.")
        return 0
    print(f"{len(props)} proposed term(s). A term several papers want is a much "
          f"stronger case than one paper's one-off.\n")
    for p in props:
        print(f"  {p['n']}x  {p['facet']}: {p['term']}"
              f"   (nearest existing parent: {p['nearest_parent']})")
        print(f"      wanted by: {', '.join(p['records'][:4])}")
    print(f"\nTo adopt one: add it under the right tier in taxonomy/v1/facets.yaml,")
    print(f"then re-run only S3 for the affected records. Accepted tiers are never")
    print(f"reshuffled — see 'Adding more papers' in the README.")
    print(f"\nfull list: {PROPOSALS_PATH.relative_to(_ROOT)}")
    return 0


def cmd_renormalize(args) -> int:
    """Re-run S5 over the whole catalog. Deterministic — zero model calls.

    This is what makes the numbers-first design pay off: change a threshold in
    binning.yaml, run this, and every record re-tags for free.
    """
    from .gating import gate_record

    changed = 0
    for d in ("records", "pointers"):
        for p in sorted((CATALOG / d).glob("*.json")):
            rec = json.loads(p.read_text(encoding="utf-8"))
            before = (rec.get("derived_tags"), rec.get("derived"))
            rec = normalize_record(rec, version=args.taxonomy)
            gate_record(rec)
            if (rec.get("derived_tags"), rec.get("derived")) != before:
                changed += 1
                print(f"  {rec['record_id']}: {before[0]} -> {rec['derived_tags']}")
            p.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    _refresh_derived_views()
    print(f"\nrenormalized {len(_all_records())} record(s), {changed} changed. No model calls.")
    return 0


def cmd_points(args) -> int:
    from .figure_points import ingest_points

    rec = ingest_points(args.record, getattr(args, "from"))
    print(f"attached {rec['points_summary']['n_points']} points "
          f"in {rec['points_summary']['n_series']} series to {args.record}")
    return 0


def cmd_figures(args) -> int:
    from .figure_points import export_figure_manifest
    from .s0_ingest import load_docmodel

    rec = json.loads((CATALOG / "records" / f"{args.record}.json").read_text(encoding="utf-8"))
    doc = load_docmodel(DOCCACHE / f"{args.record}.docmodel.json")
    p = export_figure_manifest(rec, doc, args.out)
    print(f"wrote figure manifest ({len(doc.figures)} figures) -> {p}")
    return 0


# ------------------------------------------------------ S8 crops / S9 points


def _record_pdf(record_id: str) -> Path:
    """Locate the source PDF for a record id."""
    rec_path = CATALOG / "records" / f"{record_id}.json"
    if not rec_path.exists():
        rec_path = CATALOG / "pointers" / f"{record_id}.json"
    if rec_path.exists():
        name = json.loads(rec_path.read_text(encoding="utf-8")).get("source_pdf")
        if name and (PAPERS / name).exists():
            return PAPERS / name
    hits = [p for p in PAPERS.glob("*.pdf") if _record_id(p) == record_id]
    if not hits:
        raise FileNotFoundError(f"no PDF found for record {record_id}")
    return hits[0]


def _record_ids(args) -> list[str]:
    """Every catalogued paper, pointers included.

    A review or correlation-only paper is filed under `pointers/` rather than
    `records/`, but its figures are still figures — the 1951 HTL report is one
    of the richest sources of plain-copper boiling curves in this corpus. So
    cropping and digitizing walk both directories; only *dataset* records get
    a points_ref written back.
    """
    if getattr(args, "record", None):
        return [args.record]
    ids = {p.stem for p in (CATALOG / "records").glob("*.json")}
    ids |= {p.stem for p in (CATALOG / "pointers").glob("*.json")}
    return sorted(ids)


def cmd_crops(args) -> int:
    """S8 — crop every figure and table into its own PDF + PNG."""
    from .figure_crops import extract_crops, write_manifest

    ids = _record_ids(args)
    if not ids:
        print("no records — run `python -m mhtdb.pipeline run papers/*.pdf` first",
              file=sys.stderr)
        return 1

    total = 0
    for rid in ids:
        try:
            pdf = _record_pdf(rid)
        except FileNotFoundError as e:
            print(f"[{rid}] {e}", file=sys.stderr)
            continue
        out_dir = FIGURES / rid
        manifest = out_dir / "crops.json"
        if manifest.exists() and not args.force:
            print(f"[{rid}] crops cached ({len(json.loads(manifest.read_text(encoding='utf-8'))['crops'])})")
            continue
        crops = extract_crops(pdf, out_dir, dpi=args.dpi, tables=not args.no_tables)
        write_manifest(rid, crops, manifest)
        labelled = sum(1 for c in crops if c.caption_confidence > 0)
        print(f"[{rid}] {len(crops)} crops ({labelled} captioned, "
              f"{sum(1 for c in crops if c.kind == 'table')} tables) -> "
              f"{out_dir.relative_to(_ROOT)}")
        total += len(crops)
    print(f"\n{total} element(s) cropped")
    return 0


def _parse_calib(text: str | None) -> dict | None:
    """Parse `--calib`.

        --calib "x=0:30,y=0:1800"
        --calib "x=0:30:dT_wall:K,y=0:1800:q_flux:kW/m2"
        --calib "frame=0.10/0.05/0.95/0.80,x=0:30,y=0:1200"

    Two numbers per axis — the value at each end of the plot frame — and,
    optionally, what the axis measures. `frame=` pins the plot rectangle itself
    as fractions of the crop (x0/y0/x1/y1), for figures where panel detection
    picks the wrong plot. The quantity is worth supplying for a
    fully rasterized figure: the tracing works from tick values alone, but
    nothing downstream can use a series whose axes are unnamed.
    """
    if not text:
        return None
    out: dict = {}
    for part in text.split(","):
        part = part.strip()
        m = re.match(
            r"^([xy])\s*=\s*([-\d.eE+]+)\s*:\s*([-\d.eE+]+)"
            r"(?:\s*:\s*([A-Za-z_][\w]*))?(?:\s*:\s*(\S+))?$",
            part,
        )
        naming = re.match(r"^([xy])\s*=\s*([A-Za-z_][\w]*)(?:\s*:\s*(\S+))?$", part)
        if naming:
            # Name an axis the extractor calibrated but could not label — a
            # vector figure whose axis title is an image, say. Ranges stay as
            # resolved from the text layer.
            out[f"{naming.group(1)}_quantity"] = naming.group(2)
            if naming.group(3):
                out[f"{naming.group(1)}_unit"] = naming.group(3)
            continue
        if part.startswith("frame="):
            nums = [float(v) for v in part.split("=", 1)[1].split("/")]
            if len(nums) != 4:
                raise ValueError("frame= needs four fractions: x0/y0/x1/y1")
            out["frame"] = nums
            continue
        if not m:
            raise ValueError(
                f"cannot parse calibration {part!r}; expected x=0:30 "
                f"or x=0:30:dT_wall:K"
            )
        axis = m.group(1)
        out[f"{axis}_range"] = [float(m.group(2)), float(m.group(3))]
        if m.group(4):
            out[f"{axis}_quantity"] = m.group(4)
        if m.group(5):
            out[f"{axis}_unit"] = m.group(5)
    return out


def cmd_digitize(args) -> int:
    """S9 — read the one boiling-curve figure per paper into data points."""
    import concurrent.futures
    import time

    from .digitize import NeedsCalibration, digitize_figure, select_boiling_curve_figure
    from .figure_crops import load_manifest
    from .figure_points import ingest_points

    ids = _record_ids(args)

    def _already_resolved(rid: str) -> str | None:
        """A paper whose one boiling-curve figure was already run through the
        expensive real digitizer -- either it attached points (dataset record,
        or waiting beside a pointer paper's crops), or the digitizer itself
        already opened the figure and confirmed it isn't a data plot. Checked
        so a plain `digitize` with no `--record`/`--figure` targeting doesn't
        re-pay for the same expensive agentic call on every invocation --
        everything else in this pipeline (run, crops) is incremental by
        construction, and S9 should be too. Returns a short reason string, or
        None if the figure still needs a real attempt (never run, or the
        selection/mechanism itself failed last time -- both cheap to retry)."""
        if (CATALOG / "points" / f"{rid}.points.json").exists() or (FIGURES / rid / "points.json").exists():
            return "already digitized"
        marker = FIGURES / rid / "not_boiling_curve.json"
        if marker.exists():
            return "already checked — not a boiling-curve figure"
        return None

    # ---- Phase 1: pick the one figure per paper (cheap, sequential) --------
    # A cheap selection call per paper, not a per-figure filter: given every
    # crop's caption at once, it names the single figure that is the paper's
    # primary boiling-curve comparison -- so at most one figure per paper
    # ever reaches the expensive real digitizer below.
    work: list[tuple[str, Path, object, dict | None, int | None]] = []
    for rid in ids:
        resolved = None if (args.figure or args.force) else _already_resolved(rid)
        if resolved:
            print(f"[{rid}] {resolved} — pass --force to redo, or "
                  f"--record {rid} --figure <id> to redo just one figure")
            continue
        manifest = FIGURES / rid / "crops.json"
        if not manifest.exists():
            print(f"[{rid}] no crops yet — run `python -m mhtdb.pipeline crops --record {rid}`")
            continue
        crops = [c for c in load_manifest(manifest) if c.kind == "figure"]
        if not crops:
            continue

        saved = _load_calib(rid)
        cli_calib = _parse_calib(args.calib)
        panel = args.panel

        if args.figure:
            # A human is targeting one figure explicitly (usually to correct
            # a bad auto-selection, or supply --calib) -- skip selection.
            crop = next((c for c in crops if c.element_id == args.figure), None)
            if crop is None:
                print(f"[{rid}] no figure {args.figure!r} among its crops")
                continue
            if cli_calib:
                if args.panel:
                    entry = saved.get(args.figure) or {}
                    if "panels" not in entry:
                        entry = {"panels": {}}
                    entry["panels"][str(args.panel)] = cli_calib
                    saved[args.figure] = entry
                else:
                    saved[args.figure] = cli_calib
                _save_calib(rid, saved)
        else:
            figure_id, sel_panel, reason = select_boiling_curve_figure(crops, args.model)
            if not figure_id:
                print(f"[{rid}] no boiling-curve figure among {len(crops)} figure(s) — {reason}")
                continue
            crop = next((c for c in crops if c.element_id == figure_id), None)
            if crop is None:
                print(f"[{rid}] selection named unknown figure {figure_id!r} — skipping")
                continue
            # --panel on the command line always wins; otherwise, trust the
            # selection call's own read of which lettered sub-panel (if any)
            # is the boiling curve, so a stacked multi-panel figure doesn't
            # need a human to notice and re-run with --panel by hand.
            panel = args.panel if args.panel is not None else sel_panel
            panel_note = f", panel {panel}" if panel is not None else ""
            print(f"[{rid}] selected {figure_id}{panel_note} — {reason}")

        work.append((rid, _record_pdf(rid), crop, saved.get(crop.element_id), panel))

    if not work:
        print("\n0 point(s) digitized")
        return 0

    # ---- Phase 2: digitize each paper's one figure (expensive, concurrent) -
    # One figure per paper now, so the useful thing to run concurrently is
    # different papers, not different figures within one paper.
    total = len(work)
    print(f"\ndigitizing {total} figure(s) across {total} paper(s)"
          f"{f' ({args.jobs} at a time)' if args.jobs > 1 else ''}...")
    t0 = time.time()
    done = 0
    results: dict[str, tuple] = {}   # record_id -> (crop, series_or_None, error_or_None)

    def _digitize_one(rid, pdf, crop, calibration, panel):
        try:
            got = digitize_figure(
                pdf, crop.page, crop.bbox, figure_id=crop.element_id,
                caption=f"{crop.label} {crop.caption}".strip(),
                record_id=rid, calibration=calibration,
                panel=panel, model=args.model,
                has_vector=crop.has_vector, has_raster=crop.has_raster,
            )
            return rid, crop, got, None
        except Exception as e:            # NeedsCalibration, or a bad selection
            return rid, crop, None, e

    def _report(rid, crop, got, err) -> None:
        nonlocal done
        done += 1
        results[rid] = (crop, got, err)
        prefix = f"[{done}/{total} @ {time.time() - t0:.0f}s]"
        if err is not None:
            print(f"{prefix} [{rid}] {crop.element_id}: needs calibration ({type(err).__name__})")
        elif got:
            n = sum(len(g["points"]) for g in got)
            method = got[0].get("uncertainty", {}).get("method", "unspecified")
            print(f"{prefix} [{rid}] {crop.element_id}: {len(got)} series, {n} points ({method})")
        else:
            print(f"{prefix} [{rid}] {crop.element_id}: turned out not to be a data plot")

    # `as_completed` (not `map`) so each paper's line prints the moment it
    # finishes, instead of the whole batch going silent until the slowest
    # paper in it is done.
    jobs = max(1, args.jobs)
    if jobs > 1 and total > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(_digitize_one, *w) for w in work]
            for future in concurrent.futures.as_completed(futures):
                _report(*future.result())
    else:
        for w in work:
            _report(*_digitize_one(*w))

    print(f"{total} figure(s) done in {time.time() - t0:.0f}s")

    # ---- Phase 3: write each paper's result -------------------------------
    grand = 0
    for rid, (crop, got, err) in results.items():
        if err is not None:
            detail = err.detail if isinstance(err, NeedsCalibration) else {
                "figure_id": crop.element_id, "reason": type(err).__name__,
                "message": str(err)[:200],
            }
            path = FIGURES / rid / "needs_calibration.json"
            path.write_text(json.dumps([detail], indent=2), encoding="utf-8")
            print(f"[{rid}] not digitized -> {path.relative_to(_ROOT)}")
            continue
        if not got:
            if not args.out:
                # The model opened the figure itself and confirmed it isn't a
                # data plot -- worth remembering so a future plain `digitize`
                # doesn't pay for that same real (if quick) verification call
                # again. A `--out`-targeted one-off run leaves no mark, since
                # it isn't updating the catalog's own state anyway.
                marker = FIGURES / rid / "not_boiling_curve.json"
                marker.write_text(
                    json.dumps({"figure_id": crop.element_id}, indent=2), encoding="utf-8")
            continue
        grand += sum(len(s["points"]) for s in got)
        payload = {"record_id": rid, "extractor": "mhtdb-digitize/v1", "series": got}
        if args.out:
            Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"[{rid}] wrote {args.out}")
        else:
            tmp = FIGURES / rid / "points.json"
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            if not (CATALOG / "records" / f"{rid}.json").exists():
                # A pointer paper (review / correlation-only) has no dataset
                # record to hang points on. The series are still written; they
                # just stay beside the crops instead of entering the catalog.
                print(f"[{rid}] pointer paper — points left at "
                      f"{tmp.relative_to(_ROOT)}, not attached to a record")
                continue
            rec = ingest_points(rid, tmp)
            print(f"[{rid}] attached {rec['points_summary']['n_points']} points "
                  f"in {rec['points_summary']['n_series']} series")

    print(f"\n{grand} point(s) digitized")
    return 0


def _load_calib(record_id: str) -> dict:
    p = CALIB / f"{record_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_calib(record_id: str, data: dict) -> None:
    CALIB.mkdir(parents=True, exist_ok=True)
    (CALIB / f"{record_id}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def cmd_curves(args) -> int:
    """Compile digitized points into a CSV and a comparison plot."""
    from .curves import (load_points, write_csv, select_boiling_curves,
                         plot_boiling_curves)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    records = args.records.split(",") if args.records else None

    rows = load_points(records=records)
    if not rows:
        print("no digitized points in the catalog — run `digitize` first", file=sys.stderr)
        return 1
    csv_path = write_csv(rows, out_dir / "boiling_points.csv")
    print(f"{len(rows)} point(s) -> {csv_path}")

    chosen, rejected = select_boiling_curves(
        records=records, include_all=args.all_series, fluid=args.fluid,
    )
    if not chosen:
        print("no plain-surface boiling curves matched; nothing to plot", file=sys.stderr)
        for r in rejected[:10]:
            print(f"  skipped {r.record_id}:{r.series_id} — {r.reason}", file=sys.stderr)
        return 1

    png, props = plot_boiling_curves(chosen, out_dir / "boiling_curve_summary.png",
                                     fluid=args.fluid)
    print(f"\nplotted {len(chosen)} curve(s) -> {png}")
    for sel in chosen:
        print(f"  {sel.record_id:38s} {sel.figure_id:10s} {sel.source_type:24s} "
              f"{sel.reason}")
    if rejected:
        print(f"\n{len(rejected)} series excluded (enhanced surfaces, wrong axes, "
              f"unusable points) — see --all-series to include them")
    print(f"\nRohsenow overlay properties: {props['source']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mhtdb.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="ingest PDFs and write catalog records")
    r.add_argument("pdfs", nargs="+")
    r.add_argument("--rules", action="store_true", help="use the offline rule extractor (no model at all)")
    r.add_argument("--verify", action="store_true", help="run the S6 LLM audit pass")
    r.add_argument(
        "--backend", choices=["auto", "api", "claude-code", "codex"], default=None,
        help="model backend. auto (default): API credentials if present, else a local "
             "Claude Code install, else Codex CLI. Overrides MHTDB_BACKEND.",
    )
    r.add_argument(
        "--model", default=None,
        help="model id (default: claude-opus-5 for Anthropic; Codex CLI default for codex)",
    )
    r.add_argument("--build", action="store_true",
                   help="rebuild the dashboard when the run finishes")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("backends", help="show which model backends are available")
    b.set_defaults(func=cmd_backends)

    rv = sub.add_parser("review", help="unconfirmed items awaiting a decision")
    rv.add_argument("--accept", metavar="KEY", help="accept and stop asking")
    rv.add_argument("--reject", metavar="KEY", help="reject and never suggest again")
    rv.add_argument("--note", help="why, recorded with the rejection")
    rv.set_defaults(func=cmd_review)

    pr = sub.add_parser("propose", help="vocabulary terms the taxonomy is missing")
    pr.set_defaults(func=cmd_propose)

    rn = sub.add_parser("renormalize",
                        help="re-apply units/dimensionless/binning to the whole catalog (no model calls)")
    rn.add_argument("--taxonomy", default="v1", help="taxonomy version to apply")
    rn.set_defaults(func=cmd_renormalize)

    p = sub.add_parser("ingest-points", help="attach digitized figure points to a record")
    p.add_argument("--record", required=True)
    p.add_argument("--from", required=True, dest="from")
    p.set_defaults(func=cmd_points)

    f = sub.add_parser("figures", help="export the figure manifest for the digitizer")
    f.add_argument("--record", required=True)
    f.add_argument("--out", required=True)
    f.set_defaults(func=cmd_figures)

    c = sub.add_parser("crops", help="S8: crop each figure/table into its own PDF")
    c.add_argument("--record", help="one record id (default: every record)")
    c.add_argument("--dpi", type=int, default=300)
    c.add_argument("--force", action="store_true", help="recrop even if cached")
    c.add_argument("--no-tables", action="store_true", help="figures only")
    c.set_defaults(func=cmd_crops)

    dg = sub.add_parser("digitize", help="S9: recover data points from each paper's "
                                          "one boiling-curve figure")
    dg.add_argument("--record", help="one record id (default: every record)")
    dg.add_argument("--figure", help="digitize this one figure id instead of letting "
                                     "selection pick it -- e.g. to correct a bad pick, "
                                     "or supply --calib")
    dg.add_argument("--force", action="store_true",
                    help="redo digitization even for a paper that already has points "
                         "attached (default: skip it, same as crops/run's own caching)")
    dg.add_argument("--model", default=None,
                    help="model id for the selection and digitizing calls (default: "
                         "claude's own default)")
    dg.add_argument("--calib", help='axis values at the frame edges, e.g. "x=0:30,y=0:1800"'
                                    " (needs --figure; remembered for later runs)")
    dg.add_argument("--panel", type=int,
                    help="which panel of a multi-panel figure to digitize (1-based, "
                         "top-left first); needed when a figure stacks unrelated plots")
    dg.add_argument("--out", help="write the point payload here instead of the catalog")
    dg.add_argument("--jobs", type=int, default=4,
                    help="digitize this many papers concurrently (each is an "
                         "independent agentic call on its one selected figure); "
                         "1 to run sequentially")
    dg.set_defaults(func=cmd_digitize)

    cv = sub.add_parser("curves", help="compile digitized points into a CSV + plot")
    cv.add_argument("--out", default="out", help="output directory")
    cv.add_argument("--records", help="comma-separated record ids")
    cv.add_argument("--fluid", default="water")
    cv.add_argument("--all-series", action="store_true",
                    help="plot every curve, not just plain reference surfaces")
    cv.set_defaults(func=cmd_curves)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
