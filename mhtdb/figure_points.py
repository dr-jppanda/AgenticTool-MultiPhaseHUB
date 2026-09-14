"""S9 — figure digitization hook. INTENTIONALLY NOT IMPLEMENTED.

You already have a pipeline that recovers numerical values from paper figures.
This module is the seam it plugs into. Nothing here digitizes anything; it
defines the contract in both directions and provides the adapter that folds
returned points into the catalog.

    ┌─ what we give you ──────────────────────────────────────────────┐
    │  FigureInput, built by s0_ingest for every detected figure:     │
    │    figure_id, label ("Figure 5"), caption, page,                │
    │    image_path (rendered crop), bbox, and the owning record_id   │
    └─────────────────────────────────────────────────────────────────┘
                                  │
                       your pipeline runs here
                                  │
    ┌─ what you give back ────────────────────────────────────────────┐
    │  A JSON file matching schema/point.schema.json: one or more     │
    │  PointSeries, each a list of (x, y) with axis names and units.  │
    └─────────────────────────────────────────────────────────────────┘

Two ways to connect:

  A. Offline (no code change needed). Run your pipeline however you like, emit
     JSON matching schema/point.schema.json, then:
         python -m mhtdb.pipeline ingest-points --record <id> --from points.json

  B. In-process. Implement the FigurePointProvider protocol and register it:
         from mhtdb.figure_points import register_provider
         register_provider(MyDigitizer())
     The pipeline will then call it automatically during `run --with-points`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol, runtime_checkable

from .docmodel import DocumentModel

_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------- inputs


@dataclass
class FigureInput:
    """One figure handed to the digitizer."""

    record_id: str
    doc_id: str
    figure_id: str            # "fig-5"
    label: str                # "Figure 5" as printed
    caption: str
    page: int
    image_path: str | None    # rendered PNG, if s0_ingest could produce one
    bbox: tuple[float, float, float, float] | None
    source_pdf: str

    def to_dict(self) -> dict:
        return asdict(self)


def figure_inputs_for(record: dict, doc: DocumentModel) -> list[FigureInput]:
    """Build the digitizer's input payload for one record."""
    return [
        FigureInput(
            record_id=record["record_id"],
            doc_id=doc.doc_id,
            figure_id=f.id,
            label=f.label,
            caption=f.caption,
            page=f.page,
            image_path=f.image_path,
            bbox=tuple(f.bbox) if f.bbox else None,
            source_pdf=doc.source,
        )
        for f in doc.figures
    ]


def export_figure_manifest(record: dict, doc: DocumentModel, out_path: str | Path) -> Path:
    """Write the figure manifest your pipeline consumes."""
    payload = {
        "record_id": record["record_id"],
        "doc_id": doc.doc_id,
        "figures": [fi.to_dict() for fi in figure_inputs_for(record, doc)],
    }
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


# ------------------------------------------------------------------ outputs


@runtime_checkable
class FigurePointProvider(Protocol):
    """Implement this to plug an in-process digitizer into the pipeline."""

    def extract(self, figures: list[FigureInput]) -> list[dict]:
        """Return PointSeries dicts conforming to schema/point.schema.json."""
        ...


_PROVIDER: FigurePointProvider | None = None


def register_provider(provider: FigurePointProvider) -> None:
    global _PROVIDER
    if not isinstance(provider, FigurePointProvider):
        raise TypeError("provider must implement extract(list[FigureInput]) -> list[dict]")
    _PROVIDER = provider


def get_provider() -> FigurePointProvider | None:
    return _PROVIDER


# ---------------------------------------------------------------- ingestion


REQUIRED_SERIES_KEYS = {"series_id", "figure_id", "x_axis", "y_axis", "points"}
REQUIRED_AXIS_KEYS = {"quantity", "unit"}


def validate_series(series: dict) -> list[str]:
    """Structural validation against schema/point.schema.json. Returns problems."""
    problems: list[str] = []
    missing = REQUIRED_SERIES_KEYS - series.keys()
    if missing:
        problems.append(f"missing keys: {sorted(missing)}")
    for axis in ("x_axis", "y_axis"):
        spec = series.get(axis)
        if not isinstance(spec, dict):
            problems.append(f"{axis} must be an object")
            continue
        if REQUIRED_AXIS_KEYS - spec.keys():
            problems.append(f"{axis} needs {sorted(REQUIRED_AXIS_KEYS)}")
    pts = series.get("points")
    if not isinstance(pts, list) or not pts:
        problems.append("points must be a non-empty list")
    else:
        for i, pt in enumerate(pts[:50]):
            if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
                problems.append(f"points[{i}] must be [x, y]")
                break
    return problems


def ingest_points(record_id: str, points_path: str | Path, catalog_dir: str | Path | None = None) -> dict:
    """Attach a digitizer's output to a catalog record.

    Points live in their own file — the record only carries a reference and a
    summary, so the catalog stays small and the dashboard stays fast.
    """
    catalog = Path(catalog_dir or _ROOT / "catalog")
    rec_path = catalog / "records" / f"{record_id}.json"
    if not rec_path.exists():
        raise FileNotFoundError(f"no record {record_id} at {rec_path}")

    payload = json.loads(Path(points_path).read_text(encoding="utf-8"))
    series_list = payload.get("series", payload if isinstance(payload, list) else [])

    all_problems: dict[str, list[str]] = {}
    for s in series_list:
        probs = validate_series(s)
        if probs:
            all_problems[s.get("series_id", "?")] = probs
    if all_problems:
        raise ValueError(f"point payload failed validation: {all_problems}")

    out_dir = catalog / "points"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{record_id}.points.json"
    out_path.write_text(json.dumps({"record_id": record_id, "series": series_list}, indent=2), encoding="utf-8")

    record = json.loads(rec_path.read_text(encoding="utf-8"))
    record["points_ref"] = f"points/{record_id}.points.json"
    record["points_summary"] = {
        "n_series": len(series_list),
        "n_points": sum(len(s["points"]) for s in series_list),
        "figures": sorted({s["figure_id"] for s in series_list}),
        "quantities": sorted({s["y_axis"]["quantity"] for s in series_list}),
    }
    rec_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
