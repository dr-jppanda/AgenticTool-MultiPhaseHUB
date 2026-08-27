"""Point tables and comparison plots built from digitized figure data.

Two outputs, both derived from `catalog/points/*.points.json`:

  * a flat CSV — one row per point, with the paper, figure, curve, raw value
    and unit as printed, how it was obtained, and how much to trust it, plus
    SI-derived columns. This is the ML-ready form of the catalog's point tier.
  * a comparison plot — several papers' curves for one phenomenon on one set
    of axes, with a correlation overlay and a shaded literature band.

Selecting *which* curve to plot is the part that needs care. A boiling paper's
figures are dominated by enhanced surfaces; the plain reference surface is one
series among many, and picking the wrong one silently compares a nanostructured
CHF against a smooth-copper baseline. So selection matches the legend text the
digitizer recovered, and every included curve is reported with the reason it
was chosen — never quietly.

Fluid properties for the reference correlation come from CoolProp via
`normalize.fluid_properties`, not from constants pasted into the plotting code:
the same properties then back the dimensionless groups in S5, so the overlay
and the catalog cannot drift apart.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .normalize import to_si, fluid_properties

_ROOT = Path(__file__).resolve().parent.parent

# Legend text that marks the untreated reference surface. Order matters only
# for reporting; any match qualifies.
_PLAIN_PATTERNS = [
    r"\bbare\b", r"\bplain\b", r"\bsmooth\b", r"\buntreated\b", r"\breference\b",
    r"\bref\b", r"\bpolished\b", r"\bas[- ]received\b", r"\bbaseline\b",
    r"\bpure\s+copper\b", r"\bblank\b", r"\bflat\b", r"\bunmodified\b",
    r"\bnon[- ]coated\b", r"\bpresent\s+work\b", r"\bsm\b",
]
_ENHANCED_PATTERNS = [
    r"nanostructur", r"microstructur", r"sinter", r"coat", r"cnt", r"nanowire",
    r"nanotube", r"textur", r"porous", r"foam", r"etch", r"laser", r"pillar",
    r"hierarch", r"cuo\b", r"tio2", r"sam\b", r"wick", r"fin\b", r"groove",
]


@dataclass
class PointRow:
    """One row of the flat point table — the schema boiling_curve_prompt asks for."""

    paper_id: str
    figure_id: str
    curve_id: str
    x_value: float
    x_unit: str
    y_value: float
    y_unit: str
    source_type: str
    extraction_method: str
    digitization_confidence: float | None
    notes: str
    wall_superheat_K: float | None = None
    heat_flux_W_m2: float | None = None

    FIELDS = (
        "paper_id", "figure_id", "curve_id", "x_value", "x_unit",
        "y_value", "y_unit", "source_type", "extraction_method",
        "digitization_confidence", "notes", "wall_superheat_K", "heat_flux_W_m2",
    )

    def row(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}


# ------------------------------------------------------------------ loading


def _source_type(series: dict) -> str:
    method = (series.get("uncertainty") or {}).get("method", "")
    if "table" in method:
        return "reported_table"
    if "vector" in method:
        return "vector_digitized_figure"
    return "raster_digitized_figure"


def load_points(catalog_dir: str | Path | None = None, records: list[str] | None = None) -> list[PointRow]:
    """Flatten every digitized series in the catalog into point rows."""
    catalog = Path(catalog_dir or _ROOT / "catalog")
    rows: list[PointRow] = []

    for path in sorted((catalog / "points").glob("*.points.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        record_id = payload.get("record_id", path.stem.replace(".points", ""))
        if records and record_id not in records:
            continue
        for s in payload.get("series", []):
            xq, xu = s["x_axis"].get("quantity", ""), s["x_axis"].get("unit", "")
            yq, yu = s["y_axis"].get("quantity", ""), s["y_axis"].get("unit", "")
            method = (s.get("uncertainty") or {}).get("method", "unknown")
            stype = _source_type(s)
            for pt in s["points"]:
                x, y = float(pt[0]), float(pt[1])
                dT, _ = to_si(x, xu, "dT_wall") if xq == "dT_wall" else (None, None)
                q, _ = to_si(y, yu, "q_flux") if yq in ("q_flux", "chf") else (None, None)
                rows.append(
                    PointRow(
                        paper_id=record_id,
                        figure_id=s.get("figure_id", ""),
                        curve_id=s.get("series_id", "").rsplit("-", 1)[-1] or "series",
                        x_value=x, x_unit=xu, y_value=y, y_unit=yu,
                        source_type=stype, extraction_method=method,
                        digitization_confidence=(
                            None if stype == "reported_table" else s.get("confidence")
                        ),
                        notes=s.get("notes", "") or (s.get("label", "")),
                        wall_superheat_K=dT, heat_flux_W_m2=q,
                    )
                )
    return rows


def write_csv(rows: list[PointRow], out_path: str | Path) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(PointRow.FIELDS))
        w.writeheader()
        for r in rows:
            w.writerow(r.row())
    return p


# ------------------------------------------------------------- curve picking


@dataclass
class Selection:
    record_id: str
    figure_id: str
    series_id: str
    label: str
    reason: str
    confidence: float | None
    source_type: str
    points: list[tuple[float, float]]      # (superheat K, heat flux W/m2)


_OTHER_FLUIDS = {
    "hfe": "HFE", "fc-72": "FC-72", "fc-77": "FC-77", "novec": "Novec",
    "r134a": "R134a", "r245fa": "R245fa", "r1234": "R1234", "pf-5060": "PF-5060",
    "ethanol": "ethanol", "methanol": "methanol", "acetone": "acetone",
    "nanofluid": "a nanofluid", "dielectric": "a dielectric",
}


def _wrong_fluid(caption: str, fluid: str) -> str:
    """Reject a curve whose caption names a different working fluid.

    mchale-2011 reports HFE-7300 and DI water in adjacent figures with the same
    legend text; without this check the two land on one set of water axes and
    the "literature scatter" is really a fluid change.
    """
    low = caption.lower()
    if fluid.lower() != "water":
        return ""
    for key, name in _OTHER_FLUIDS.items():
        if key in low:
            return f"caption names {name}, not water"
    return ""


_COLOUR_ONLY = re.compile(r"^(red|green|blue|orange|magenta|cyan|dark|yellow|violet)$", re.I)


def _classify(label: str, notes: str = "", caption: str = "",
              siblings: int = 99) -> tuple[bool, str]:
    """Is this series the paper's plain reference surface, and on what evidence?

    A raster-traced series has no legend text to read — it is named for the
    colour it was traced from. When that is all we have, the figure caption can
    still decide it, but only when the figure holds no other candidate: a
    caption reading "boiling curves of the reference Cu plate and of surfaces
    with nanowire arrays" describes both, and picking the reference out of five
    traced colours is not something this can honestly do.
    """
    if _COLOUR_ONLY.match((label or "").strip()) and caption:
        plain = [p for p in _PLAIN_PATTERNS if re.search(p, caption.lower())]
        enhanced = [p for p in _ENHANCED_PATTERNS if re.search(p, caption.lower())]
        if plain and not enhanced and siblings <= 3:
            return True, f"caption names a {plain[0].strip(chr(92)+'b')} surface " \
                         f"and the figure has {siblings} traced series"
        return False, ("traced series, named only by colour — caption is ambiguous"
                       if plain else "traced series, named only by colour")

    text = f"{label} {notes}".lower()
    enhanced = [p for p in _ENHANCED_PATTERNS if re.search(p, text)]
    plain = [p for p in _PLAIN_PATTERNS if re.search(p, text)]
    if plain and not enhanced:
        return True, f"legend matched {plain[0]!r}"
    if plain and enhanced:
        return False, f"legend matched {plain[0]!r} but also {enhanced[0]!r} — ambiguous"
    if not label:
        return False, "unlabelled series"
    return False, f"legend looks like an enhanced surface ({enhanced[0]!r})" if enhanced \
        else "legend does not name a plain reference surface"


def select_boiling_curves(
    catalog_dir: str | Path | None = None,
    records: list[str] | None = None,
    min_confidence: float = 0.0,
    include_all: bool = False,
    fluid: str = "water",
) -> tuple[list[Selection], list[Selection]]:
    """Find each paper's plain-surface boiling curve. Returns (chosen, rejected)."""
    catalog = Path(catalog_dir or _ROOT / "catalog")
    chosen: list[Selection] = []
    rejected: list[Selection] = []

    for path in sorted((catalog / "points").glob("*.points.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        record_id = payload.get("record_id", path.stem.replace(".points", ""))
        if records and record_id not in records:
            continue
        for s in payload.get("series", []):
            xq = s["x_axis"].get("quantity", "")
            yq = s["y_axis"].get("quantity", "")
            if xq != "dT_wall" or yq not in ("q_flux", "chf"):
                continue
            pts: list[tuple[float, float]] = []
            for pt in s["points"]:
                dT, _ = to_si(float(pt[0]), s["x_axis"].get("unit"), "dT_wall")
                q, _ = to_si(float(pt[1]), s["y_axis"].get("unit"), "q_flux")
                if dT is not None and q is not None and dT > 0 and q > 0:
                    pts.append((dT, q))
            if len(pts) < 3:
                continue
            # Points stay in the order the digitizer recovered them in — the
            # PDF's own draw order, or chained stroke order — rather than
            # being re-sorted by superheat. A boiling curve is not always
            # single-valued in dT (CHF hysteresis, transition-boiling
            # reversal), so sorting by x here would braid separate branches
            # back together right before they reach the chart.
            caption = (s.get("conditions") or {}).get("figure_caption", "")
            wrong_fluid = _wrong_fluid(caption, fluid)
            # Classify on the legend text where there is one: the caption
            # describes the whole figure, so "hierarchical CuO surfaces" there
            # would condemn the plain reference curve plotted beside them. The
            # caption is consulted only for series with no legend text at all.
            siblings = sum(1 for o in payload.get("series", [])
                           if o.get("figure_id") == s.get("figure_id"))
            is_plain, reason = _classify(s.get("label", ""), caption=caption,
                                         siblings=siblings)
            if wrong_fluid:
                is_plain, reason = False, wrong_fluid
            sel = Selection(
                record_id=record_id, figure_id=s.get("figure_id", ""),
                series_id=s.get("series_id", ""), label=s.get("label", ""),
                reason=reason, confidence=s.get("confidence"),
                source_type=_source_type(s), points=pts,
            )
            conf_ok = (sel.confidence or 0) >= min_confidence
            (chosen if (is_plain or include_all) and conf_ok else rejected).append(sel)

    return chosen, rejected


# ------------------------------------------------------- reference correlation


def rohsenow(
    dT: list[float],
    fluid: str = "water",
    p_sat: float = 101325.0,
    c_sf: float = 0.013,
    n: float = 1.0,
) -> tuple[list[float], dict]:
    """Rohsenow nucleate pool-boiling correlation, evaluated from real properties.

        q" = mu_l h_fg sqrt(g (rho_l - rho_v) / sigma) [ cp_l dT / (c_sf h_fg Pr^n) ]^3

    c_sf = 0.013 and n = 1.0 are the water-on-copper pair. Both are surface
    dependent, which is exactly why the correlation is drawn as a reference
    line and not as a fit to anybody's data.
    """
    props = fluid_properties(fluid, p_sat=p_sat)
    cp_l, Pr_l = _cp_and_pr(fluid, p_sat)
    needed = ("mu_l", "h_fg", "rho_l", "rho_v", "sigma")
    if any(props.get(k) is None for k in needed) or cp_l is None or Pr_l is None:
        # Representative water/copper constants, used only if CoolProp is absent.
        props = {"mu_l": 2.82e-4, "h_fg": 2.257e6, "rho_l": 958.0,
                 "rho_v": 0.597, "sigma": 0.0589}
        cp_l, Pr_l = 4217.0, 1.75
        props["source"] = "builtin constants (CoolProp unavailable)"
    else:
        props = {k: props[k] for k in needed}
        props["source"] = f"CoolProp {fluid} @ {p_sat/1000:.1f} kPa"
    props.update(cp_l=cp_l, Pr_l=Pr_l, c_sf=c_sf, n=n)

    g = 9.80665
    pre = props["mu_l"] * props["h_fg"] * math.sqrt(
        g * (props["rho_l"] - props["rho_v"]) / props["sigma"]
    )
    q = [
        pre * (cp_l * t / (c_sf * props["h_fg"] * Pr_l ** n)) ** 3 if t > 0 else 0.0
        for t in dT
    ]
    return q, props


def _cp_and_pr(fluid: str, p_sat: float):
    try:
        from CoolProp.CoolProp import PropsSI
    except ImportError:
        return None, None
    try:
        name = {"water": "Water"}.get(fluid, fluid)
        cp = PropsSI("C", "P", p_sat, "Q", 0, name)
        mu = PropsSI("V", "P", p_sat, "Q", 0, name)
        k = PropsSI("L", "P", p_sat, "Q", 0, name)
        return cp, cp * mu / k
    except Exception:
        return None, None


# -------------------------------------------------------------------- plotting


def plot_boiling_curves(
    selections: list[Selection],
    out_path: str | Path,
    fluid: str = "water",
    p_sat: float = 101325.0,
    title: str | None = None,
    dpi: int = 200,
):
    """Literature-summary boiling curve: wall temperature vs heat flux."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    T_sat_C = 100.0 if fluid == "water" and abs(p_sat - 101325.0) < 2000 else None
    if T_sat_C is None:
        props = fluid_properties(fluid, p_sat=p_sat)
        T_sat_C = (props.get("T_sat_calc") or 373.15) - 273.15

    fig, ax = plt.subplots(figsize=(10.5, 6))
    curves = []
    for sel in selections:
        dT = np.array([p[0] for p in sel.points])
        q = np.array([p[1] for p in sel.points]) / 1e4        # W/m2 -> W/cm2
        Tw = T_sat_C + dT
        ax.plot(Tw, q, marker="o", markersize=4, linewidth=1.2,
                label=f"{sel.record_id}:{sel.series_id.rsplit('-', 1)[-1]}")
        curves.append((Tw, q))

    if len(curves) >= 2:
        lo = max(c[0].min() for c in curves)
        hi = min(c[0].max() for c in curves)
        grid = np.linspace(
            min(c[0].min() for c in curves), max(c[0].max() for c in curves), 200
        )
        stack = []
        for Tw, q in curves:
            # np.interp requires its xp strictly ascending; the plotted curve
            # itself is kept in recovered path order (which may fold back on
            # itself), so this sorts a private copy just for the band and
            # leaves the drawn line alone.
            order = np.argsort(Tw)
            vals = np.interp(grid, Tw[order], q[order], left=np.nan, right=np.nan)
            stack.append(vals)
        stack = np.array(stack)
        covered = (~np.isnan(stack)).sum(axis=0) >= 2
        if covered.any():
            lo_band = np.nanmin(stack[:, covered], axis=0)
            hi_band = np.nanmax(stack[:, covered], axis=0)
            ax.fill_between(grid[covered], lo_band, hi_band, color="0.5",
                            alpha=0.18, label="Literature range", zorder=0)

    max_dT = max((p[0] for s in selections for p in s.points), default=20.0)
    dT_grid = list(np.linspace(0.5, max(20.0, max_dT), 200))
    q_roh, props = rohsenow(dT_grid, fluid=fluid, p_sat=p_sat)
    ax.plot([T_sat_C + t for t in dT_grid], [v / 1e4 for v in q_roh],
            "k--", linewidth=1.4, label="Rohsenow demo")

    ymax = max((p[1] for s in selections for p in s.points), default=1e5) / 1e4
    ax.set_ylim(0, 1.15 * ymax)
    ax.set_xlabel(f"Wall temperature, $T_w$ (°C)")
    ax.set_ylabel("Heat flux, $q''$ (W/cm²)")
    ax.set_title(title or f"Saturated pool boiling of {fluid} — literature summary")
    ax.grid(alpha=0.3, linewidth=0.6)
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=7, frameon=False)
    fig.tight_layout()

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return p, props
