"""S8 — figure digitization. Vector paths first, raster tracing as fallback.

`figure_points.py` defines the seam; this is a provider that fills it without
a model and without a human. The order matters and is not a preference:

  1. **Vector.** A plot placed in the PDF as native vector art still contains
     the drawing commands that produced it — marker centres and polyline
     vertices with full float precision. Reading those back is not
     "digitization" in the WebPlotDigitizer sense at all; it is recovering the
     numbers the author's plotting library wrote down. Error is limited to the
     axis calibration, so confidence is high (0.85-0.98).

  2. **Raster.** A flattened or scanned figure has no such record, so pixels
     get traced against a calibrated axis and confidence drops accordingly
     (0.5-0.9). Both paths are annotated per series, and nothing is emitted
     without a calibration that actually fits.

Calibration is never guessed. Tick *values* come from the PDF's own text
layer; if a figure is fully rasterized so that even its tick labels are pixels,
this module raises `NeedsCalibration` listing the tick marks it found, and the
caller supplies the values (`--calib`) rather than the module inventing them.
That is the same trade the extraction stages make: resolve, don't generate.

Coordinates are read from the *source page* with a clip rect rather than from
the cropped PDF, because a crop written with `show_pdf_page` wraps the content
in a Form XObject whose transform PyMuPDF does not fold into `get_drawings()`.
The crop files stay the human-facing artifact; `Crop.bbox` is the digitizer's
actual input.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

from .figure_crops import Crop

# ---------------------------------------------------------------- constants

MARKER_MAX = 14.0        # a path smaller than this on both sides is a marker
GRID_FRAC = 0.88         # a line spanning this much of the frame is a gridline
TICK_BAND_X = 34.0       # how far below the frame x tick labels can sit
TICK_BAND_Y = 78.0       # how far left of the frame y tick labels can sit
MIN_TICKS = 2
MIN_R2 = 0.995
MIN_POINTS = 3

_NUM = re.compile(r"^[−–—-]?\d{1,3}(?:[  ,]\d{3})*(?:\.\d+)?$|^[−–—-]?\d*\.?\d+(?:[eE][+-]?\d+)?$")

# Axis-title text -> catalog field name. Anything unmatched passes through as
# free text: schema/point.schema.json allows quantities the taxonomy has not
# named yet, and a wrong guess here is worse than an honest label.
_QUANTITY_PATTERNS = [
    # Δ appears as U+0394, U+2206, or — in Symbol-font PDFs — as the private-use
    # glyph U+F044. Miss it and "ΔTw [K]" reads as an absolute wall temperature,
    # which silently shifts every point by 100 K downstream.
    ("dT_wall", r"(wall\s*superheat|superheat|[Δ∆δ]\s*t|delta\s*t|"
                r"t\s*w\s*[-−]\s*t\s*sat)"),
    ("q_flux", r"(heat\s*flux|q\s*[\"″'’″]|q\s*w\b|\bq\b\s*$)"),
    ("htc", r"(heat\s*transfer\s*coefficient|htc|\bh\b\s*\[)"),
    ("T_wall", r"(wall\s*temperature|t\s*wall|t\s*w\b)"),
    ("x_quality", r"(vapou?r\s*quality|quality)"),
    ("G", r"(mass\s*flux|mass\s*velocity)"),
    ("p_sat", r"(pressure)"),
    ("chf", r"(critical\s*heat\s*flux|chf)"),
]


class NeedsCalibration(RuntimeError):
    """Raised when a figure is traceable but its axis values are unreadable."""

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


# ------------------------------------------------------------------- models


@dataclass
class AxisCal:
    """value = m*coord + c, or 10**(m*coord + c) on a log axis."""

    m: float
    c: float
    scale: str            # "linear" | "log"
    r2: float
    n_ticks: int
    quantity: str = ""
    unit: str = ""
    span: float = 0.0        # coordinate range the accepted ticks cover

    def to_data(self, coord: float) -> float:
        v = self.m * coord + self.c
        return 10.0 ** v if self.scale == "log" else v


@dataclass
class Panel:
    """One plot frame inside a figure. Multi-panel figures yield several."""

    rect: "fitz.Rect"
    index: int
    x: AxisCal | None = None
    y: AxisCal | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- geometry


def _iter_segments(item):
    """Yield ((x0,y0),(x1,y1)) for every straight piece of a drawing item."""
    for seg in item["items"]:
        if seg[0] == "l":
            yield (seg[1].x, seg[1].y), (seg[2].x, seg[2].y)
        elif seg[0] == "re":
            r = seg[1]
            yield (r.x0, r.y0), (r.x1, r.y0)
            yield (r.x1, r.y0), (r.x1, r.y1)
            yield (r.x1, r.y1), (r.x0, r.y1)
            yield (r.x0, r.y1), (r.x0, r.y0)
        elif seg[0] == "c":
            pts = [p for p in seg[1:] if hasattr(p, "x")]
            for a, b in zip(pts, pts[1:]):
                yield (a.x, a.y), (b.x, b.y)
        elif seg[0] == "qu":
            q = seg[1]
            pts = [q.ul, q.ur, q.lr, q.ll]
            for a, b in zip(pts, pts[1:] + pts[:1]):
                yield (a.x, a.y), (b.x, b.y)


def _frame_candidates(page, clip) -> list["fitz.Rect"]:
    """Every rectangle in the clip that could be a plot's axis box.

    Two shapes are common. A stroked rectangle (Tecplot, Origin, MATLAB box
    axes) is found directly. Spine-only axes (matplotlib default, many Excel
    charts) are recovered by pairing a long vertical line with a long
    horizontal line that meet near a corner. Both are collected — which one is
    the real axis box is decided later by whichever calibrates, because a
    panel background rect and its axis box are indistinguishable by geometry
    alone and picking the wrong one puts the tick labels *inside* the frame.
    """
    W, H = clip.width, clip.height
    cands: list[fitz.Rect] = []
    hlines: list[tuple[float, float, float]] = []   # y, x0, x1
    vlines: list[tuple[float, float, float]] = []   # x, y0, y1

    for it in page.get_drawings():
        r = fitz.Rect(it["rect"])
        if not clip.intersects(r):
            continue
        for seg in it["items"]:
            if seg[0] == "re":
                rr = fitz.Rect(seg[1]) & clip
                if rr.width > 0.22 * W and rr.height > 0.22 * H:
                    cands.append(rr)
        for (x0, y0), (x1, y1) in _iter_segments(it):
            if abs(y1 - y0) < 0.8 and abs(x1 - x0) > 0.25 * W:
                hlines.append((y0, min(x0, x1), max(x0, x1)))
            elif abs(x1 - x0) < 0.8 and abs(y1 - y0) > 0.25 * H:
                vlines.append((x0, min(y0, y1), max(y0, y1)))

    for vx, vy0, vy1 in vlines:
        for hy, hx0, hx1 in hlines:
            if abs(vx - hx0) < 8 and abs(vy1 - hy) < 8:      # bottom-left corner
                r = fitz.Rect(vx, vy0, hx1, hy) & clip
                if r.width > 0.22 * W and r.height > 0.22 * H:
                    cands.append(r)

    uniq: list[fitz.Rect] = []
    for c in cands:
        if not any(abs(c.x0 - u.x0) < 2 and abs(c.y0 - u.y0) < 2
                   and abs(c.x1 - u.x1) < 2 and abs(c.y1 - u.y1) < 2 for u in uniq):
            uniq.append(c)
    return uniq


def _panel_groups(cands: list["fitz.Rect"]) -> list[list["fitz.Rect"]]:
    """Cluster candidate rects into panels; concentric rects share a panel."""
    groups: list[list[fitz.Rect]] = []
    for c in sorted(cands, key=lambda r: -r.get_area()):
        for g in groups:
            if (c & g[0]).get_area() > 0.5 * min(c.get_area(), g[0].get_area()):
                g.append(c)
                break
        else:
            groups.append([c])
    groups.sort(key=lambda g: (round(g[0].y0), round(g[0].x0)))
    return groups


# ------------------------------------------------------------ calibration


def _as_number(text: str) -> float | None:
    t = text.strip().replace("−", "-").replace("–", "-").replace("—", "-")
    t = t.replace(" ", "").replace(" ", "").replace(",", "")
    if not t or not _NUM.match(text.strip().replace("−", "-")):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _fit(coords: list[float], values: list[float]) -> tuple[float, float, float]:
    """Least squares value = m*coord + c. Returns (m, c, r2)."""
    n = len(coords)
    mx = sum(coords) / n
    my = sum(values) / n
    sxx = sum((x - mx) ** 2 for x in coords)
    sxy = sum((x - mx) * (y - my) for x, y in zip(coords, values))
    if sxx == 0:
        return 0.0, my, 0.0
    m = sxy / sxx
    c = my - m * mx
    ss_tot = sum((y - my) ** 2 for y in values)
    ss_res = sum((y - (m * x + c)) ** 2 for x, y in zip(coords, values))
    r2 = 1.0 if ss_tot == 0 else max(0.0, 1 - ss_res / ss_tot)
    return m, c, r2


def _calibrate(ticks: list[tuple[float, float]]) -> AxisCal | None:
    """Fit an axis from (coordinate, printed value) pairs, linear or log.

    The tick set is dirty by construction: a panel label, an inset's numbers,
    the other axis' zero, and — when the labels come from OCR — arrowheads read
    as "1" all land in the same band as the real labels. So the fit is
    consensus-based rather than least-squares-with-pruning: every pair of ticks
    proposes a mapping, and the mapping that the most ticks agree with wins.
    Pruning the worst residual instead lets a cluster of identical junk values
    outvote a correct but sparser tick set.
    """
    if len(ticks) < MIN_TICKS:
        return None
    ticks = _dedupe_ticks(ticks)
    best: AxisCal | None = None

    for scale in ("linear", "log"):
        pool = [(c, v) for c, v in ticks if scale == "linear" or v > 0]
        if len(pool) < MIN_TICKS:
            continue
        vals = [v for _, v in pool]
        if scale == "log":
            if len(pool) < 3 or max(vals) / min(vals) < 20:
                continue
            pool = [(c, math.log10(v)) for c, v in pool]
            vals = [v for _, v in pool]
        spread = max(vals) - min(vals)
        if spread <= 0:
            continue
        tol = 0.02 * spread

        winner: list[tuple[float, float]] = []
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                (ci, vi), (cj, vj) = pool[i], pool[j]
                if abs(cj - ci) < 1e-6 or vi == vj:
                    continue
                m = (vj - vi) / (cj - ci)
                c = vi - m * ci
                inliers = [(cc, vv) for cc, vv in pool if abs(vv - (m * cc + c)) <= tol]
                if len(inliers) > len(winner):
                    winner = inliers
        if len(winner) < max(MIN_TICKS, 3) and len(pool) > 2:
            continue          # 3+ agreeing ticks whenever 3+ were offered
        if len(winner) < MIN_TICKS:
            continue

        m, c, r2 = _fit([w[0] for w in winner], [w[1] for w in winner])
        if m == 0 or r2 < MIN_R2:
            continue
        coords = [w[0] for w in winner]
        cal = AxisCal(m=m, c=c, scale=scale, r2=r2, n_ticks=len(winner),
                      span=max(coords) - min(coords))
        if best is None or (cal.n_ticks, cal.r2) > (best.n_ticks, best.r2):
            best = cal

    return best


def _dedupe_ticks(ticks: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Collapse labels that share a coordinate, keeping the first seen."""
    out: list[tuple[float, float]] = []
    for coord, val in ticks:
        if not any(abs(coord - c) < 1.2 for c, _ in out):
            out.append((coord, val))
    return out


def _tick_words(page, frame, clip):
    """Numeric words positioned like tick labels on each axis."""
    xt: list[tuple[float, float]] = []
    yt: list[tuple[float, float]] = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        val = _as_number(text)
        if val is None:
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if frame.y1 - 2 <= y0 <= frame.y1 + TICK_BAND_X and frame.x0 - 12 <= cx <= frame.x1 + 12:
            xt.append((cx, val))
        elif frame.x0 - TICK_BAND_Y <= x1 <= frame.x0 + 4 and frame.y0 - 8 <= cy <= frame.y1 + 8:
            yt.append((cy, val))
    return xt, yt


# Symbol-font glyphs that survive text extraction as private-use codepoints.
_GLYPHS = {
    "\uf044": "\u0394", "\uf07f": "\u0394", "\u2206": "\u0394",   # Symbol-font delta
    "\uf0b2": "\u2033", "\uf0a2": "\u2033", "\uf022": "\u2033",   # double prime (q")
    "\uf0b0": "\u00b0", "\uf03d": "=", "\uf02d": "-",
    "\u2212": "-", "\uf06d": "\u03bc", "\uf073": "\u03c3",
    "\u00ba": "\u00b0",                                   # masculine ordinal
}


def _despan(text: str) -> str:
    """Undo the artefacts of pulling text out of a PDF: glyph codes, split
    superscripts ("W/cm 2"), and runs of whitespace."""
    for bad, good in _GLYPHS.items():
        text = text.replace(bad, good)
    text = re.sub(r"\s+", " ", text).strip()
    # "W/cm 2" / "kW/m 2 K" -> "W/cm2" / "kW/m2K": a superscript typeset as its
    # own span reads back as a stray digit.
    text = re.sub(r"(?<=[a-zA-Z]) (?=\d\b)", "", text)
    text = re.sub(r"(?<=\d) (?=[Kk]\b)", "", text)
    return text


def _canonical_quantity(text: str) -> str:
    low = _despan(text).lower()
    for name, pat in _QUANTITY_PATTERNS:
        if re.search(pat, low):
            return name
    return _despan(text)[:40]


_UNIT = re.compile(r"[\[(]\s*([^\])]{1,24})\s*[\])]")


def _title_score(text: str) -> float:
    """How much a text block looks like an axis title rather than furniture."""
    t = _despan(text)
    if not t or _as_number(t) is not None:
        return -1.0
    if re.fullmatch(r"[(\[]?\s*[a-z]\s*[)\]]?\s*", t, re.I):     # "(a)" panel letter
        return -1.0
    if re.fullmatch(r"[\d\s.x×^+-]*10\s*\d*", t):                 # "8 x 10 4" axis multiplier
        return -1.0
    score = 0.0
    if _UNIT.search(t):
        score += 2.0
    if any(re.search(pat, t.lower()) for _, pat in _QUANTITY_PATTERNS):
        score += 2.0
    if len(t) <= 30:
        score += 0.5
    return score


def _axis_labels(page, frame, clip) -> tuple[tuple[str, str], tuple[str, str]]:
    """Read the axis titles: (x quantity, x unit), (y quantity, y unit).

    Candidates are scored, not ranked by position: the block furthest from the
    axis is as likely to be a panel letter or a "x 10^4" multiplier as it is to
    be the title, and mislabelling ΔT as T_wall is a silent 100 K error.
    """
    x_cands: list[tuple[float, float, str]] = []
    y_cands: list[tuple[float, float, str]] = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        r = fitz.Rect(b["bbox"])
        if not clip.intersects(r):
            continue
        text = " ".join(s["text"] for l in b["lines"] for s in l["spans"]).strip()
        if not text or len(text) > 60:
            continue
        score = _title_score(text)
        if score < 0:
            continue
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        if (frame.y1 - 4 < r.y0 <= frame.y1 + TICK_BAND_X + 34
                and frame.x0 - 24 <= cx <= frame.x1 + 24):
            x_cands.append((score, r.y0 - frame.y1, text))
        elif (r.x1 <= frame.x0 + 8 and r.x1 >= frame.x0 - TICK_BAND_Y - 40
                and frame.y0 - 12 <= cy <= frame.y1 + 12):
            y_cands.append((score, frame.x0 - r.x1, text))

    def pick(cands):
        if not cands:
            return "", ""
        cands.sort(key=lambda t: (-t[0], t[1]))      # best score, then nearest
        text = _despan(cands[0][2])
        unit = _UNIT.search(text)
        return (
            _canonical_quantity(_UNIT.sub("", text)),
            _despan(unit.group(1)) if unit else "",
        )

    return pick(x_cands), pick(y_cands)


# ------------------------------------------------------------ vector marks


def _legend_zones(page, frame) -> list["fitz.Rect"]:
    """Text blocks inside the plot that are legends, not data.

    An in-plot legend brings its own marker glyphs, drawn in each series' own
    style. Left in, they add phantom points at whatever data coordinates the
    legend box happens to occupy — and they sit exactly where a reader would
    never look for data.
    """
    zones: list[fitz.Rect] = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        r = fitz.Rect(b["bbox"])
        if not frame.contains(r) or len(b["lines"]) < 2:
            continue
        text = " ".join(s["text"] for l in b["lines"] for s in l["spans"]).strip()
        if not text or all(_as_number(w) is not None for w in text.split()):
            continue                       # a column of tick labels, not a legend
        zones.append(fitz.Rect(r.x0 - 46, r.y0 - 4, r.x1 + 6, r.y1 + 4))
    return zones


def _inset_box_zones(page, frame, clip, min_frac: float = 0.01,
                      max_frac: float = 0.55, margin: float = 1.5) -> list["fitz.Rect"]:
    """Boxed insets drawn *inside* the plot frame: legends, results tables,
    parameter callouts — the "left-top description box" case.

    A legend is often not just floating text but a bordered box, sometimes
    holding a small table of its own (roughness values, fit parameters) next
    to the marker key. `_legend_zones` finds the text; this finds the box
    itself from its drawn border, which is what actually bounds the bullet
    glyphs, the table's numbers, and the border strokes — all of which read
    back as phantom data points if left in. Any small stroked rectangle
    fully contained in the resolved axis frame is exactly that: a construct
    with nothing to do with the axes, whether or not it happens to sit
    flush against one of the frame's own edges.
    """
    # An earlier version of this also rejected any box touching a frame edge,
    # on the theory that a real sub-panel divider looks like that. It does
    # not: legends get tucked flush against a spine constantly (this figure's
    # sits flush against the top axis), and that guard silently let the
    # commonest case straight through. `max_frac` is the actual safety net —
    # a genuine second panel sharing this frame spans a large fraction of
    # it, not a corner box — so edge contact is no longer disqualifying.
    zones: list[fitz.Rect] = []
    frame_area = max(frame.get_area(), 1.0)
    for it in page.get_drawings():
        r = fitz.Rect(it["rect"])
        if not clip.intersects(r) or not frame.contains(r):
            continue
        for seg in it["items"]:
            if seg[0] != "re":
                continue
            rr = fitz.Rect(seg[1])
            if not frame.contains(rr):
                continue
            area_frac = rr.get_area() / frame_area
            if not (min_frac <= area_frac <= max_frac):
                continue
            zones.append(fitz.Rect(rr.x0 - margin, rr.y0 - margin,
                                    rr.x1 + margin, rr.y1 + margin))
    return zones


def _drop_isolated_marks(pts: list[tuple[float, float]], frame,
                          k_factor: float = 3.0, min_pts: int = 6,
                          abs_frac: float = 0.08):
    """Drop points that sit alone, far from every other point in the group.

    A legend bullet or a figure's own marker-preview callout (common next to
    a boxless legend, or repeating a curve's marker beside its own topmost
    point) is drawn in the curve's exact colour, so style alone cannot tell
    it from data — but geometry can. Real digitized data clusters along a
    curve: every point has a near neighbour. An annotation glyph sitting off
    on its own does not. This flags a point only when its nearest neighbour
    is both several times farther than typical for the group *and* a real
    fraction of the plot away, so a curve's genuinely sparse endpoints are
    left alone — it takes an actual gap, not just being the last point.
    Returns (kept, dropped), never touching the order of what it keeps.
    """
    if len(pts) < min_pts:
        return pts, []
    norm = [(p[0] / frame.width, p[1] / frame.height) for p in pts]
    nn = []
    for i, p in enumerate(norm):
        d = min(math.hypot(p[0] - q[0], p[1] - q[1])
                for j, q in enumerate(norm) if j != i)
        nn.append(d)
    med = sorted(nn)[len(nn) // 2]
    if med <= 1e-9:
        return pts, []
    threshold = max(k_factor * med, abs_frac)
    kept = [p for p, d in zip(pts, nn) if d <= threshold]
    dropped = [p for p, d in zip(pts, nn) if d > threshold]
    if len(kept) < MIN_POINTS:
        return pts, []                      # pruning would gut the series; leave it
    return kept, dropped


def _style_key(item) -> tuple:
    def rgb(c):
        return tuple(round(v, 2) for v in c) if c else None

    fill = rgb(item.get("fill"))
    stroke = rgb(item.get("color"))
    filled = fill is not None
    # Width deliberately does not enter the key: a marker is often drawn as a
    # fill plus a same-colour outline, and splitting on width turns one curve
    # into two. Fill-vs-outline does enter it, because filled/open markers are
    # how a paper distinguishes ascending from descending runs.
    return (
        fill or stroke,
        filled,
        bool(item.get("dashes") and item["dashes"] not in ("[] 0", "[]0")),
    )


def _is_white(key) -> bool:
    col = key[0]
    return col is not None and all(v > 0.93 for v in col)


def _is_tick(r, frame, tol: float = 3.0) -> bool:
    """A tick mark: a hairline sitting on one of the frame's four edges.

    Thinness is the discriminator, not position — a datum at the top of the
    axis also touches the frame, and dropping it silently truncates the very
    end of the curve that CHF studies care about most.
    """
    if min(r.width, r.height) > 2.5:
        return False
    return (
        abs(r.x0 - frame.x0) < tol or abs(r.x1 - frame.x1) < tol
        or abs(r.y0 - frame.y0) < tol or abs(r.y1 - frame.y1) < tol
    )


def _is_open_polyline(it, tol: float = 0.05) -> bool:
    """Is this drawing item's own path unclosed?

    A real marker glyph — a diamond, square, triangle — is drawn as a
    closed polygon: walk its segments and you arrive back at the point you
    started from. An error bar's cap is drawn as a "staple" (a horizontal
    tick with a short drop at each end) using the same straight-segment
    fill primitive a marker uses, but the three strokes don't chain into a
    loop — the last segment doesn't end where the first one began. That
    open-endedness is what tells the two apart when colour and bounding
    size can't: nothing about a filled black bracket's *shape* looks like
    a datum, whatever its dimensions happen to be.
    """
    segs = list(_iter_segments(it))
    if len(segs) < 2:
        return False               # a single stroke has no "closed" to fail
    start = segs[0][0]
    end = segs[-1][1]
    return math.hypot(end[0] - start[0], end[1] - start[1]) > tol


def _collect_marks(page, frame, clip, legend_zones=()):
    """Group drawing items inside the frame by style, into markers and lines.

    Returns {style_key: {"markers": [(x, y)], "lines": [[(x, y), ...]]}}.
    """
    inner = fitz.Rect(frame.x0 - 1.5, frame.y0 - 1.5, frame.x1 + 1.5, frame.y1 + 1.5)
    out: dict[tuple, dict] = {}

    for it in page.get_drawings():
        r = fitz.Rect(it["rect"])
        if r.is_empty or not inner.contains(fitz.Point(r.x0, r.y0)) or not inner.intersects(r):
            continue
        if not inner.contains(r):
            continue
        key = _style_key(it)
        if key[0] is None or _is_white(key):
            continue
        # frame, gridlines, panel background
        if r.width > GRID_FRAC * frame.width or r.height > GRID_FRAC * frame.height:
            continue

        if any(z.intersects(r) for z in legend_zones):
            continue
        slot = out.setdefault(key, {"markers": [], "lines": []})
        if r.width <= MARKER_MAX and r.height <= MARKER_MAX:
            if _is_tick(r, frame):
                continue                      # axis tick, not a datum
            # error bars: thin, tall, unfilled — a whisker, not a datum
            if not key[1] and r.width < 2.0 and r.height > 3.5:
                continue
            # An error bar's cap is a filled "staple" — a tick with a short
            # drop at each end, built from straight strokes the same way a
            # marker is — but the strokes never close into a loop the way
            # every real marker glyph does. Whatever colour it's drawn in
            # (often plain black, independent of the curve's own colour),
            # it rides right alongside the real markers rather than sitting
            # apart from them, so nothing that checks isolation catches it;
            # shape is the only thing that does.
            if _is_open_polyline(it):
                continue
            slot["markers"].append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
            continue

        # An error bar's whisker stem is a thin near-vertical stroke, same
        # as the open-style one above, just taller than a marker's bounding
        # box — that's the only reason it lands here instead of in the
        # marker branch. Tall doesn't make it data: it's still one whisker,
        # not a segment of the curve's own connecting line, and chaining
        # dozens of these nearly-parallel stems head-to-tail (the fate of
        # anything landing in `lines`) produces a zig-zag that resembles no
        # real curve.
        if r.width < 2.0 and r.height > 3.5:
            continue

        pts: list[tuple[float, float]] = []
        for a, b in _iter_segments(it):
            if not pts:
                pts.append(a)
            pts.append(b)
        if len(pts) >= 2:
            slot["lines"].append(pts)

    return out


def _merge_duplicates(vector_series, frame):
    """Fold together style groups that are plainly the same curve.

    A marker drawn as a filled glyph plus a same-colour outline yields two
    style groups tracing identical points. Ascending and descending runs of the
    same surface also share a colour but genuinely diverge — so the test is
    whether the two point clouds actually coincide, not whether they look
    alike. Merged series say so in `notes`, per the "don't fabricate a false
    split, don't hide a real one" rule.
    """
    def _close_colour(a, b, tol: float = 0.05) -> bool:
        # A filled glyph and its own stroked outline rarely share bit-identical
        # RGB — PDF writers round or blend the two paths' colours slightly
        # differently — but they're visually the same series. Comparing with
        # a small tolerance instead of exact equality is what lets that pair
        # merge; two genuinely different curves' colours (e.g. the blue and
        # the green surface here) sit far enough apart that this tolerance
        # never conflates them.
        if a is None or b is None:
            return a == b
        return all(abs(x - y) <= tol for x, y in zip(a, b))

    merged: list = []
    for key, pts, method, slot in vector_series:
        for i, (k2, p2, m2, s2) in enumerate(merged):
            if not _close_colour(k2[0], key[0]):   # different colour: never merge
                continue
            if _coincident(pts, p2, frame):
                keep = _union_points(p2, pts, frame)
                note = dict(s2)
                note["merged"] = note.get("merged", 0) + 1
                merged[i] = (k2, keep, m2, note)
                break
        else:
            merged.append((key, pts, method, dict(slot)))
    return merged


def _coincident(a, b, frame, tol_frac: float = 0.01, need: float = 0.9) -> bool:
    """Is the smaller point cloud essentially contained in the larger one?

    Containment, not similarity. A marker's outline group is a subset of its
    fill group and merges. Ascending and descending runs of the same surface
    share a colour and overlap at low superheat but diverge above it, so the
    smaller set is *not* contained and they stay separate — which is the
    distinction that matters, because merging them would erase the hysteresis
    the paper set out to show.
    """
    if not a or not b:
        return False
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    tol_x, tol_y = tol_frac * frame.width, tol_frac * frame.height
    hits = sum(
        1 for px, py in small
        if any(abs(px - qx) <= tol_x and abs(py - qy) <= tol_y for qx, qy in large)
    )
    return hits >= need * len(small)


def _chain_lines(lines: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Concatenate one style's polyline pieces into a single ordered path.

    Each piece is already in the order PyMuPDF's content-stream walk produced
    it in, which for a stroked curve is the order the plotting library drew
    it in — i.e. the data's own order, vertex by vertex. A paper often draws
    the ascending and descending branch of a hysteresis loop (or two runs
    separated by a gap the author's software didn't bridge) as two separate
    strokes in the same colour; flattening them and then sorting by x would
    braid the two branches together wherever their superheats overlap, which
    is exactly the zig-zag that makes a re-rendered curve look noisy. Joining
    head-to-tail by nearest endpoint — reversing a piece where that keeps the
    path continuous — recovers the single walk the author's pen actually took.
    """
    pieces = [list(p) for p in lines if p]
    if not pieces:
        return []
    chain = pieces.pop(0)
    while pieces:
        tail = chain[-1]
        best_i, best_rev, best_d = 0, False, math.inf
        for i, p in enumerate(pieces):
            for rev, end in ((False, p[0]), (True, p[-1])):
                d = math.hypot(end[0] - tail[0], end[1] - tail[1])
                if d < best_d:
                    best_d, best_i, best_rev = d, i, rev
        piece = pieces.pop(best_i)
        if best_rev:
            piece = list(reversed(piece))
        chain.extend(piece)
    return chain


def _union_points(a, b, frame, tol_frac: float = 0.005):
    """Merge two coincident clouds without double-counting shared points."""
    tol_x, tol_y = tol_frac * frame.width, tol_frac * frame.height
    out = list(a)
    for px, py in b:
        if not any(abs(px - qx) <= tol_x and abs(py - qy) <= tol_y for qx, qy in out):
            out.append((px, py))
    return out


def _legend_labels(page, frames, clip, zones=()) -> dict[tuple, str]:
    """Map a style key to its legend text.

    Legend glyphs are drawn in the series' own style but live outside every
    plot frame; the text immediately to their right on the same line is the
    label the author gave that curve. Recovering it is what makes `curve_id`
    meaningful ("bare_copper") instead of "series-3".
    """
    words = [w for w in page.get_text("words") if fitz.Rect(w[:4]).intersects(clip)]
    labels: dict[tuple, tuple[float, str]] = {}

    for it in page.get_drawings():
        r = fitz.Rect(it["rect"])
        if not clip.intersects(r) or r.width > 40 or r.height > 40:
            continue
        if any(f.intersects(r) for f in frames) and not any(z.intersects(r) for z in zones):
            continue
        key = _style_key(it)
        if key[0] is None or _is_white(key):
            continue
        cy = (r.y0 + r.y1) / 2
        line = [
            w for w in words
            if w[0] > r.x1 - 2 and w[0] < r.x1 + 220 and w[1] - 3 <= cy <= w[3] + 3
        ]
        if not line:
            continue
        line.sort(key=lambda w: w[0])
        picked = [line[0]]
        for prev, w in zip(line, line[1:]):
            if w[0] - prev[2] > 7:       # a wide gap: the next legend column
                break
            picked.append(w)
        text = " ".join(w[4] for w in picked).strip()
        if len(text) < 2:
            continue
        prev = labels.get(key)
        if prev is None or len(text) > len(prev[1]):
            labels[key] = (cy, text)

    return {k: v[1] for k, v in labels.items()}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:48] or "series"


def _ocr_numbers(page, rect, dpi=400):
    """Every numeric token in a region, with its page-coordinate centre.

    Optional by design: `pytesseract` + the tesseract binary are not
    dependencies of this repo. Without them a fully rasterized figure raises
    NeedsCalibration and the caller supplies four numbers once. With them the
    same figure calibrates itself, at a confidence penalty — OCR misreads
    "8" as "0" often enough that it must never look like text-layer truth.

    The whole region is read in one pass and the tokens are bound to an axis
    box afterwards, so a slightly wrong box estimate costs a few labels
    instead of all of them.
    """
    try:
        import pytesseract
        from PIL import Image
        import io
    except ImportError:
        return []

    rect = rect & page.rect
    if rect.is_empty or rect.width < 8 or rect.height < 8:
        return []
    zoom = dpi / 72.0
    try:
        pix = page.get_pixmap(clip=rect, dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        data = pytesseract.image_to_data(
            img, output_type=pytesseract.Output.DICT,
            config="--psm 11 -c tessedit_char_whitelist=0123456789.-",
        )
    except Exception:
        return []

    out = []
    for i, text in enumerate(data["text"]):
        val = _as_number(text or "")
        if val is None or int(data["conf"][i] or -1) < 55:
            continue
        cx = rect.x0 + (data["left"][i] + data["width"][i] / 2) / zoom
        cy = rect.y0 + (data["top"][i] + data["height"][i] / 2) / zoom
        out.append((cx, cy, val))
    return out


def _bind_ticks(numbers, frame):
    """Sort OCR'd numbers into x-axis and y-axis tick labels for one frame."""
    xt, yt = [], []
    for cx, cy, val in numbers:
        if frame.y1 - 4 <= cy <= frame.y1 + TICK_BAND_X and \
                frame.x0 - 16 <= cx <= frame.x1 + 16:
            xt.append((cx, val))
        elif frame.x0 - TICK_BAND_Y <= cx <= frame.x0 + 6 and \
                frame.y0 - 10 <= cy <= frame.y1 + 10:
            yt.append((cy, val))
    return xt, yt



def _known_unit(unit: str) -> bool:
    """Does mhtdb.normalize know how to convert this unit?"""
    try:
        from .normalize import to_si
    except Exception:                                  # pragma: no cover
        return True
    for field in ("q_flux", "dT_wall", "htc", "G", "p_sat", "D_h"):
        if to_si(1.0, unit, field)[0] is not None:
            return True
    return False


def _ocr_axis_titles(page, frame, dpi=400):
    """OCR the axis-title strips of a fully rasterized plot.

    Returns ((quantity, unit) | None, ...) for x and y. The y title is printed
    rotated, so its strip is rotated back before recognition.
    """
    try:
        import pytesseract
        from PIL import Image
        import io
    except ImportError:
        return None, None

    def read(rect, rotate=0):
        rect = rect & page.rect
        if rect.is_empty or rect.width < 6 or rect.height < 6:
            return None
        try:
            pix = page.get_pixmap(clip=rect, dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            if rotate:
                img = img.rotate(rotate, expand=True)
            text = pytesseract.image_to_string(img, config="--psm 7").strip()
        except Exception:
            return None
        text = _despan(text)
        if len(text) < 2 or _as_number(text) is not None:
            return None
        unit = _UNIT.search(text)
        quantity = _canonical_quantity(_UNIT.sub("", text))
        # OCR on a rotated 8-point axis title returns confident nonsense often
        # enough that only a recognised quantity is worth keeping. An empty
        # label is honest; "er oi otro ow a" is not.
        if quantity not in {name for name, _ in _QUANTITY_PATTERNS}:
            return None
        text_unit = _despan(unit.group(1)) if unit else ""
        if text_unit and not _known_unit(text_unit):
            # OCR read "W/cm2" as "Wiem'" often enough that an unrecognised
            # unit has to be dropped: a unit nothing can convert silently
            # removes the series from every downstream SI comparison.
            text_unit = ""
        return quantity, text_unit

    x_strip = fitz.Rect(frame.x0, frame.y1 + TICK_BAND_X * 0.4,
                        frame.x1, frame.y1 + TICK_BAND_X + 22)
    y_strip = fitz.Rect(frame.x0 - TICK_BAND_Y - 22, frame.y0,
                        frame.x0 - TICK_BAND_Y * 0.35, frame.y1)
    return read(x_strip), read(y_strip, rotate=-90)


def _plausible_frame(rect, clip, xt, yt, xcal=None, ycal=None) -> bool:
    """Reject frames that calibrate beautifully against the wrong labels.

    A legend box is a rectangle with numbers under it, and outlier-rejecting
    least squares will happily fit two of them. Two properties separate a real
    axis: it occupies a serious share of the figure, and its labels run the
    length of it. Without this check kim-2016 Fig. 8 calibrates its x axis
    against the legend's roughness values and reports superheats of 0-0.8 K.
    """
    if rect.get_area() < 0.15 * clip.get_area():
        return False
    # The ticks the fit actually agreed on must run the length of the axis.
    # A consensus formed from three labels crowded into one corner fits
    # perfectly and extrapolates into nonsense across the rest of the frame.
    if xcal and xcal.span < 0.45 * rect.width:
        return False
    if ycal and ycal.span < 0.45 * rect.height:
        return False
    if len(xt) >= 2:
        span = max(c for c, _ in xt) - min(c for c, _ in xt)
        if span < 0.5 * rect.width:
            return False
    if len(yt) >= 2:
        span = max(c for c, _ in yt) - min(c for c, _ in yt)
        if span < 0.5 * rect.height:
            return False
    return True


def _best_frame(page, group, clip, calibration, index, ocr=False, dpi=400):
    ocr_numbers = None
    supplied_frame = bool((calibration or {}).get('frame'))
    """Choose, among concentric candidates, the rect whose axes actually fit.

    A calibration that fits is the only evidence that a rectangle is the axis
    box rather than the panel background, so the candidates are ranked by fit
    quality instead of by size.
    """
    best = None
    best_score = -1.0
    last_xt: list = []
    last_yt: list = []

    rects: list[fitz.Rect] = []
    for rect in group:
        rects.append(rect)
        if len(_tick_words(page, rect, clip)[0]) < MIN_TICKS:
            # The candidate is probably the whole bitmap; the axis box is drawn
            # inside it. Which strictness finds it depends on how much margin,
            # title and legend the bitmap carries, so try both and let the
            # calibration decide.
            for frac in (0.45, 0.25):
                inner = _refine_raster(page, rect, dpi, min_frac=frac)
                if inner and inner not in rects:
                    rects.append(inner)

    for rect in rects:
        xt, yt = _tick_words(page, rect, clip)
        source = "text_layer"
        if ocr and (len(xt) < MIN_TICKS or len(yt) < MIN_TICKS):
            if ocr_numbers is None:
                ocr_numbers = _ocr_numbers(page, clip, dpi)
            ox, oy = _bind_ticks(ocr_numbers, rect)
            if len(ox) >= len(xt) or len(oy) >= len(yt):
                xt, yt, source = (ox or xt), (oy or yt), "ocr"
        if calibration:
            xt = [tuple(t) for t in calibration.get("x", xt)] or xt
            yt = [tuple(t) for t in calibration.get("y", yt)] or yt
            # The friendlier form: the value at each end of the axis. Most
            # plots are drawn with the axis box *at* its limits, so two numbers
            # per axis is all a human should have to type.
            if calibration.get("x_range"):
                lo, hi = calibration["x_range"]
                xt = [(rect.x0, float(lo)), (rect.x1, float(hi))]
            if calibration.get("y_range"):
                lo, hi = calibration["y_range"]
                yt = [(rect.y1, float(lo)), (rect.y0, float(hi))]
            source = "supplied"
        if len(xt) + len(yt) > len(last_xt) + len(last_yt):
            last_xt, last_yt = xt, yt
        xcal, ycal = _calibrate(xt), _calibrate(yt)
        if not xcal or not ycal:
            continue
        if not supplied_frame and not _plausible_frame(rect, clip, xt, yt, xcal, ycal):
            continue
        score = (
            min(xcal.n_ticks, ycal.n_ticks)
            + 5.0 * (xcal.r2 + ycal.r2)
            + rect.get_area() / max(clip.get_area(), 1.0)
        )
        if score > best_score:
            best_score = score
            panel = Panel(rect=rect, index=index, x=xcal, y=ycal,
                          notes=[f"axis values from {source}"])
            best = panel

    return best, last_xt, last_yt


def _ink(page, rect, dpi):
    """Boolean "there is ink here" mask for a page region.

    Luminance, not a per-channel threshold: journal figures routinely draw
    axes in 60% grey, which a `max(channel) < 128` test reads as blank paper.
    """
    import numpy as np

    pix = page.get_pixmap(clip=rect, dpi=dpi)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    lum = (0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2])
    return lum < 190


def _longest_run(mask_1d) -> tuple[int, int, int]:
    """Longest consecutive True run: (length, start, end)."""
    best = (0, 0, 0)
    run = 0
    for i, v in enumerate(mask_1d):
        if v:
            run += 1
            if run > best[0]:
                best = (run, i - run + 1, i)
        else:
            run = 0
    return best


def _axis_box_in(dark, min_frac: float = 0.25):
    """Find the axis box inside a boolean ink mask.

    Uses each rule's *extent*, not just which row or column it occupies. A
    matplotlib- or Origin-style plot draws only the left and bottom spines, so
    there is no second long row to bound the top — but the left spine's own
    length gives the plot height, and the bottom spine's length gives the
    width. Reading only row/column indices finds one line, measures a zero-high
    box, and reports "no plot frame" on a perfectly ordinary chart.
    """
    import numpy as np

    h, w = dark.shape
    if h < 20 or w < 20:
        return None

    rows = [_longest_run(dark[y]) for y in range(h)]
    cols = [_longest_run(dark[:, x]) for x in range(w)]
    ry = int(np.argmax([r[0] for r in rows]))
    cx = int(np.argmax([c[0] for c in cols]))
    row, col = rows[ry], cols[cx]
    if row[0] < min_frac * w or col[0] < min_frac * h:
        return None

    left, right = col[1] if False else cx, row[2]
    left = min(cx, row[1])
    right = max(row[2], cx)
    top = col[1]
    bottom = max(ry, col[2]) if ry >= col[1] else col[2]
    # A closed box gives a second long row at the top and column at the right;
    # when present they agree with the extents above, so nothing more is needed.
    if bottom - top < 0.15 * h or right - left < 0.15 * w:
        return None
    return left, top, right, bottom


def _merge_rects(rects, gap: float = 8.0):
    """Union rectangles that touch or nearly touch.

    Some PDF exporters slice one figure into a stack of thin image strips —
    moze-2022 Fig. 3 arrives as 24 bands. Scanned band-by-band, no strip
    contains a whole axis and the figure looks like it has no plot in it.
    """
    out: list[fitz.Rect] = []
    for r in rects:
        r = fitz.Rect(r)
        merged = True
        while merged:
            merged = False
            for i, o in enumerate(out):
                grown = fitz.Rect(o.x0 - gap, o.y0 - gap, o.x1 + gap, o.y1 + gap)
                if grown.intersects(r):
                    r |= o
                    out.pop(i)
                    merged = True
                    break
        out.append(r)
    return out


def _split_panels(dark, min_gutter: float = 0.045):
    """Split an ink mask at blank gutters: side-by-side or stacked panels.

    Returns pixel sub-boxes (left, top, right, bottom). A figure captioned
    "(a) boiling curves and (b) heat transfer coefficients" is two plots with
    different axes; treating it as one region calibrates whichever panel drew
    the longer line and puts the other panel's points on the wrong scale.
    """
    import numpy as np

    h, w = dark.shape
    def gutters(profile, span):
        blanks, runs, start = profile == 0, [], None
        for i, b in enumerate(blanks):
            if b and start is None:
                start = i
            elif not b and start is not None:
                runs.append((start, i - 1))
                start = None
        if start is not None:
            runs.append((start, len(blanks) - 1))
        return [r for r in runs
                if r[1] - r[0] >= min_gutter * span and r[0] > 0.08 * span
                and r[1] < 0.92 * span]

    col_ink = dark.sum(axis=0)
    row_ink = dark.sum(axis=1)
    xs, ys = [0], [0]
    for a, b in gutters(col_ink, w):
        xs.append((a + b) // 2)
    for a, b in gutters(row_ink, h):
        ys.append((a + b) // 2)
    xs.append(w); ys.append(h)

    boxes = []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            x0, x1, y0, y1 = xs[i], xs[i + 1], ys[j], ys[j + 1]
            if (x1 - x0) > 0.18 * w and (y1 - y0) > 0.18 * h:
                boxes.append((x0, y0, x1, y1))
    return boxes or [(0, 0, w, h)]


def _raster_frames(page, clip, dpi) -> list[list["fitz.Rect"]]:
    """Find axis boxes burned into bitmaps, one per panel.

    Image blocks are merged, each merged region is split at its blank gutters,
    and every sub-panel is scanned for its own axis box. Multi-panel figures
    are the norm in this corpus, and a panel is the unit that can be
    calibrated — `--panel N` indexes exactly what comes back here.
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return []

    raw = [fitz.Rect(b["bbox"]) & clip for b in page.get_text("dict")["blocks"]
           if b["type"] == 1]
    raw = [r for r in raw if not r.is_empty and r.width > 12 and r.height > 12]
    regions = [r for r in _merge_rects(raw) if r.width > 40 and r.height > 40] or [clip]

    zoom = dpi / 72.0
    out: list[list[fitz.Rect]] = []
    for region in regions:
        ink = _ink(page, region, dpi)
        for (px0, py0, px1, py1) in _split_panels(ink):
            sub = ink[py0:py1, px0:px1]
            box = _axis_box_in(sub)
            if not box:
                continue
            left, top, right, bottom = box
            out.append([fitz.Rect(
                region.x0 + (px0 + left) / zoom, region.y0 + (py0 + top) / zoom,
                region.x0 + (px0 + right) / zoom, region.y0 + (py0 + bottom) / zoom,
            )])

    out.sort(key=lambda g: (round(g[0].y0), round(g[0].x0)))
    return out


def _refine_raster(page, rect, dpi, min_frac: float = 0.25):
    """Given a candidate that yielded no tick text, look for the axis box
    drawn *inside* it — the usual shape when a whole plot is one image."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return None
    box = _axis_box_in(_ink(page, rect, dpi), min_frac=min_frac)
    if not box:
        return None
    zoom = dpi / 72.0
    left, top, right, bottom = box
    inner = fitz.Rect(
        rect.x0 + left / zoom, rect.y0 + top / zoom,
        rect.x0 + right / zoom, rect.y0 + bottom / zoom,
    )
    return inner if inner.width > 20 and inner.height > 20 else None


# ------------------------------------------------------------------ raster


def _column_clusters(col_mask, gap: int = 3):
    """Contiguous row runs in one pixel column, merging pinholes."""
    import numpy as np

    rows = np.where(col_mask)[0]
    if rows.size == 0:
        return []
    runs, start, prev = [], rows[0], rows[0]
    for r in rows[1:]:
        if r - prev > gap:
            runs.append((start, prev))
            start = r
        prev = r
    runs.append((start, prev))
    return [((a + b) / 2.0, b - a + 1) for a, b in runs]


def _predict_next(history: list[tuple[int, float]], next_c: int) -> float:
    """Extrapolate the expected row at `next_c` from the last two accepted
    points, instead of assuming the curve is flat since the last column.

    A pure nearest-to-last-y rule is what snaps the trace onto the wrong
    curve wherever two series cross: at the crossing column both branches'
    clusters are equally close to the last accepted y, and a coin flip picks
    one — then keeps following it. Carrying the local slope through means the
    branch that actually continues the trend wins the tie.
    """
    if len(history) < 2:
        return history[-1][1]
    (c0, y0), (c1, y1) = history[-2], history[-1]
    if next_c == c1:
        return y1
    slope = (y1 - y0) / (c1 - c0) if c1 != c0 else 0.0
    return y1 + slope * (next_c - c1)


def _smooth(ys: list[float], window: int = 3) -> list[float]:
    """A short rolling median over the traced pixel row.

    Anti-aliasing and JPEG ringing shift the occasional column's ink centroid
    a pixel or two off the true line even when the underlying curve is
    smooth. A 3-wide median absorbs that single-column jitter — the noise
    this is aimed at — without flattening a real feature, since a genuine
    bend spans many columns, not one.
    """
    n = len(ys)
    if n < window:
        return ys
    half = window // 2
    return [sorted(ys[max(0, i - half):min(n, i + half + 1)])[
        min(half, i, n - 1 - i)] for i in range(n)]


def _trim_drawn_shaft(ordered, smoothed, min_run: int = 12, tol: float = 1.2):
    """Drop a leading/trailing run whose traced row never moves.

    A curve reconstructed from antialiased ink drifts by at least a
    fraction of a pixel column to column even where the underlying data is
    locally flat — it's read off a rendered image, not a formula. A run of
    many consecutive columns landing on the *identical* row is instead the
    signature of ink drawn as a straight primitive: a dash, an arrow shaft,
    an arrowhead. Most often that is a same-colour annotation appended past
    the real curve's last point (e.g. gesturing "continues up to here"
    toward a theory or limit line outside the traced range), which shares
    the series' colour so nothing upstream can tell it from data by style
    alone. Confined to the two ends of the walk: a dead-flat stretch in the
    interior is treated as what it more plausibly is — a genuine plateau in
    the real curve — and left alone.
    """
    n = len(ordered)
    if n < min_run * 2:
        return ordered, smoothed

    def run_len_from(start, step):
        # A running min/max, not a pairwise check against the previous point:
        # one column landing a pixel outside its neighbours (a real
        # possibility from antialiasing, or from a reference-line row
        # getting blanked out from under part of the arrow's ink) shouldn't
        # snap the run short before it's actually left the flat stretch.
        j, count = start, 1
        lo = hi = smoothed[start]
        while 0 <= j + step < n:
            v = smoothed[j + step]
            new_lo, new_hi = min(lo, v), max(hi, v)
            if new_hi - new_lo > tol:
                break
            lo, hi = new_lo, new_hi
            j += step
            count += 1
        return count

    lo, hi = 0, n
    lead = run_len_from(0, 1)
    if min_run <= lead < 0.6 * n:
        lo = lead
    trail = run_len_from(n - 1, -1)
    if min_run <= trail < 0.6 * n:
        hi = n - trail
    if hi <= lo:
        return ordered, smoothed
    return ordered[lo:hi], smoothed[lo:hi]


def _trace(mask, frame, panel, zoom):
    """Follow one curve across a colour mask, column by column.

    Taking the mean row of every masked pixel in a column is the obvious
    approach and it is wrong: an in-plot legend drawn in the same colour drags
    the trace toward it, and where two branches of a hysteresis loop share a
    column the trace lands between them. Instead each column's ink is split
    into clusters and the trace follows the cluster nearest to where the local
    slope predicts it should be — seeded from the column with the least
    ambiguity — and the accepted row sequence is lightly median-smoothed
    before being resampled down to the output points.
    """
    import numpy as np

    h, w = mask.shape
    cols = np.where(mask.any(axis=0))[0]
    if cols.size < 8:
        return []

    clusters = {int(c): _column_clusters(mask[:, c]) for c in cols}
    single = [c for c in cols if len(clusters[int(c)]) == 1]
    seed = int(single[len(single) // 2]) if single else int(cols[len(cols) // 2])
    if not clusters[seed]:
        return []

    max_jump = 0.12 * h
    picked: dict[int, float] = {seed: clusters[seed][0][0]}

    for direction in (1, -1):
        history = [(seed, clusters[seed][0][0])]
        c = seed
        while True:
            nxt = [k for k in cols if (k - c) * direction > 0]
            if not nxt:
                break
            c = int(min(nxt, key=lambda k: abs(k - c)))
            cands = clusters[c]
            if not cands:
                continue
            target = _predict_next(history, c)
            best = min(cands, key=lambda t: abs(t[0] - target))
            if abs(best[0] - target) > max_jump:
                continue           # a legend swatch or another series, not us
            picked[c] = best[0]
            history.append((c, best[0]))
            del history[:-2]

    ordered = sorted(picked)
    smoothed = _smooth([picked[c] for c in ordered])
    ordered, smoothed = _trim_drawn_shaft(ordered, smoothed)
    if len(ordered) < 8:
        return []

    step = max(1, len(ordered) // 60)
    out = []
    for i, c in enumerate(ordered):
        if i % step:
            continue
        px = frame.x0 + c / zoom
        py = frame.y0 + smoothed[i] / zoom
        out.append((panel.x.to_data(px), panel.y.to_data(py)))
    out.sort(key=lambda p: p[0])
    return out


def _max_run_lengths(mask2d, axis: int):
    """Longest run of True crossing each line perpendicular to `axis`.

    `axis=1` returns, for every row, the longest unbroken run of True along
    that row (shape `(h,)`). `axis=0` does the same per column. One
    vectorized pass along the run direction — not a python loop over every
    pixel, which would be too slow at 400 dpi.
    """
    import numpy as np

    if axis == 1:
        h, w = mask2d.shape
        run = np.zeros((h, w), dtype=np.int32)
        run[:, 0] = mask2d[:, 0]
        for x in range(1, w):
            run[:, x] = np.where(mask2d[:, x], run[:, x - 1] + 1, 0)
        return run.max(axis=1)
    h, w = mask2d.shape
    run = np.zeros((h, w), dtype=np.int32)
    run[0, :] = mask2d[0, :]
    for y in range(1, h):
        run[y, :] = np.where(mask2d[y, :], run[y - 1, :] + 1, 0)
    return run.max(axis=0)


def _straight_reference_mask(dark, sat, min_frac: float = 0.6):
    """Rows/columns that are a straight reference line, not traced data.

    At any single row, a real plotted curve's ink only ever spans a
    handful of columns — its y changes with x, so it crosses each row
    briefly on its way past. A row where ink (colour or black, checked
    together) runs almost unbroken across most of the frame's width is not
    data at any x: it's a straight theory or limit line sharing the frame
    with the real curves — a Zuber CHF ceiling, a Rohsenow correlation
    drawn flat, a unity line. Checking colour and black together, not just
    black, matters: a same-colour annotation arrow drawn flush against
    such a line (pointing "the curve continues up to here") would
    otherwise pass through untouched and drag a trace along with it. The
    same test transposed catches a vertical reference line.
    """
    import numpy as np

    ink = dark | (sat > 40)
    h, w = ink.shape
    row_runs = _max_run_lengths(ink, axis=1)
    col_runs = _max_run_lengths(ink, axis=0)
    ref_rows = row_runs >= min_frac * w
    ref_cols = col_runs >= min_frac * h
    mask = np.zeros((h, w), dtype=bool)
    mask[ref_rows, :] = True
    mask[:, ref_cols] = True
    return mask, int(ref_rows.sum()), int(ref_cols.sum())


def _raster_series(page, frame, clip, panel, dpi, style_hint=None, zones=()):
    """Trace a rasterized plot body by colour, using an already-fitted axis.

    Only reached when the figure has no usable vector data. Calibration still
    comes from the PDF text layer — this traces pixels, it does not read them.
    `zones` (in page coordinates, e.g. from `_legend_zones`/`_inset_box_zones`)
    are blanked out of every colour mask before tracing, for the same reason
    the vector path excludes them: a boxed legend or its bullet glyphs are
    ink like any other, and the pixel tracer has no style key to tell them
    from a real curve — only position. A straight full-width/full-height
    reference line (see `_straight_reference_mask`) is blanked the same way,
    whatever colour it's drawn in, before any colour group is traced.

    Returns `(series, ref_info)`: `ref_info` is `None` unless a straight
    reference line was found and excluded, in which case it's
    `{"rows": n, "cols": m}`.
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        raise RuntimeError("numpy is required for raster digitization")

    zoom = dpi / 72.0
    pix = page.get_pixmap(clip=frame, dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    rgb = img[:, :, :3].astype(np.int16)

    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    sat = mx - mn
    dark = mx < 110

    for z in zones:
        zz = z & frame
        if zz.is_empty:
            continue
        x0 = max(0, int((zz.x0 - frame.x0) * zoom))
        x1 = min(mx.shape[1], int((zz.x1 - frame.x0) * zoom) + 1)
        y0 = max(0, int((zz.y0 - frame.y0) * zoom))
        y1 = min(mx.shape[0], int((zz.y1 - frame.y0) * zoom) + 1)
        dark[y0:y1, x0:x1] = False
        sat[y0:y1, x0:x1] = 0

    ref_mask, ref_rows, ref_cols = _straight_reference_mask(dark, sat)
    ref_info = {"rows": ref_rows, "cols": ref_cols} if (ref_rows or ref_cols) else None
    if ref_info:
        dark = dark & ~ref_mask
        sat = np.where(ref_mask, 0, sat)

    series: list[dict] = []
    groups: list[tuple[str, "np.ndarray"]] = []

    # Saturated colours first: each distinct hue is a series.
    coloured = sat > 55
    if coloured.sum() > 40:
        hue_bins = {
            "red": (r > g + 45) & (r > b + 45),
            "green": (g > r + 35) & (g > b + 25),
            "blue": (b > r + 45) & (b > g + 30),
            "orange": (r > g + 30) & (g > b + 40) & (r > 150),
            "magenta": (r > g + 45) & (b > g + 45),
            "cyan": (g > r + 40) & (b > r + 40),
        }
        for name, mask in hue_bins.items():
            m = mask & coloured
            if m.sum() > 60:
                groups.append((name, m))
    if not groups:
        # Monochrome plot: everything dark that is not the frame itself.
        m = dark.copy()
        m[:3, :] = m[-3:, :] = False
        m[:, :3] = m[:, -3:] = False
        if m.sum() > 60:
            groups.append(("dark", m))

    import numpy as np  # noqa: F811  (local alias after the guard above)

    for name, mask in groups:
        pts = _trace(mask, frame, panel, zoom)
        if len(pts) >= MIN_POINTS:
            series.append({"style": name, "points": pts})

    return series, ref_info


# ------------------------------------------------------------------ driver


def digitize_figure(
    pdf_path: str | Path,
    page_no: int,
    bbox,
    figure_id: str = "fig",
    caption: str = "",
    dpi: int = 400,
    calibration: dict | None = None,
    prefer: str = "auto",
    ocr: bool = True,
) -> list[dict]:
    """Digitize one figure region. Returns PointSeries dicts (point.schema.json).

    `calibration` optionally supplies axis values the PDF text layer cannot:
    {"x": [[coord, value], ...], "y": [...]} in page points.
    `prefer` is "auto" | "vector" | "raster".
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for digitization: pip install pymupdf")

    doc = fitz.open(str(pdf_path))
    page = doc[page_no - 1]
    clip = fitz.Rect(*bbox) & page.rect

    # An explicit frame ends the argument. Panel detection is a heuristic, and
    # on a figure that stacks a boiling curve over a bar chart it can pick the
    # wrong plot with complete confidence — at which point supplied axis values
    # get applied to the wrong axes. `frame` (fractions of the crop) says
    # exactly which rectangle is the plot; nothing is inferred.
    frame_spec = (calibration or {}).get("frame")
    if frame_spec:
        fx0, fy0, fx1, fy1 = frame_spec
        groups = [[fitz.Rect(
            clip.x0 + fx0 * clip.width, clip.y0 + fy0 * clip.height,
            clip.x0 + fx1 * clip.width, clip.y0 + fy1 * clip.height,
        )]]
    else:
        groups = _panel_groups(_frame_candidates(page, clip))
    if not groups:
        groups = _raster_frames(page, clip, dpi)
    if not groups:
        doc.close()
        raise NeedsCalibration(
            f"{figure_id}: no plot frame found (not a plot, or axes not drawn)",
            {"figure_id": figure_id, "page": page_no, "reason": "no_frame"},
        )

    frames = [g[0] for g in groups]
    out: list[dict] = []
    pending: list[NeedsCalibration] = []
    for i, group in enumerate(groups, 1):
        spec = _panel_calibration(calibration, i)
        panel, xt, yt = _best_frame(page, group, clip, spec, i, ocr=ocr, dpi=dpi)
        if panel is None:
            frame = group[0]
            pending.append(NeedsCalibration(
                f"{figure_id} panel {i}: axis calibration failed "
                f"({len(xt)} x-ticks, {len(yt)} y-ticks readable in the text layer)",
                {
                    "figure_id": figure_id, "page": page_no, "panel": i,
                    "n_panels": len(groups), "reason": "no_calibration",
                    "x_ticks": xt, "y_ticks": yt,
                    "frame": [round(v, 2) for v in group[0]],
                    "hint": f"supply --calib (add --panel {i} on this "
                            f"{len(groups)}-panel figure), or install "
                            f"pytesseract+tesseract to read tick labels burned "
                            f"into the image",
                },
            ))
            continue
        frame = panel.rect
        frames[i - 1] = frame
        (xq, xu), (yq, yu) = _axis_labels(page, frame, clip)
        for axis, (q_key, u_key) in (("x", ("x_quantity", "x_unit")),
                                     ("y", ("y_quantity", "y_unit"))):
            # `spec`, not `calibration`: on a panel-scoped calibration the
            # names live inside the panel entry, and reading the outer dict
            # silently returns an unnamed axis that nothing downstream can use.
            if spec and spec.get(q_key):
                if axis == "x":
                    xq, xu = spec[q_key], spec.get(u_key, xu)
                else:
                    yq, yu = spec[q_key], spec.get(u_key, yu)
        if ocr and not (xq and yq):
            oxq, oyq = _ocr_axis_titles(page, frame, dpi)
            if not xq and oxq:
                xq, xu = oxq
            if not yq and oyq:
                yq, yu = oyq
        panel.x.quantity, panel.x.unit = xq, xu
        panel.y.quantity, panel.y.unit = yq, yu

        zones = _legend_zones(page, frame) + _inset_box_zones(page, frame, clip)
        marks = _collect_marks(page, frame, clip, zones)
        legend = _legend_labels(page, frames, clip, zones)
        suffix = f"-p{i}" if len(frames) > 1 else ""

        vector_series = []
        for key, slot in marks.items():
            # Markers keep the order `page.get_drawings()` returned them in —
            # the PDF content-stream order, which for a plotted series is the
            # order the author's library issued the glyphs in, i.e. the
            # data's own sequence. Trusting it (rather than re-sorting by x)
            # is what keeps a hysteresis loop or a non-monotonic quench curve
            # from being braided into a zig-zag by two branches that share an
            # x-range. Line vertices get the same treatment via `_chain_lines`.
            pts = list(slot["markers"])
            method = "vector_path_extraction"
            chained_from = 0
            if len(pts) < MIN_POINTS and slot["lines"]:
                chained_from = len(slot["lines"])
                pts = _chain_lines(slot["lines"])
                method = "vector_path_extraction"
            if len(pts) < MIN_POINTS:
                continue
            # A legend bullet or a marker-preview callout beside a boxless
            # legend shares the curve's exact colour, so style can't tell it
            # from data — but it sits alone, with no near neighbour the way
            # every real point on a digitized curve does.
            pts, isolated = _drop_isolated_marks(pts, frame)
            if len(pts) < MIN_POINTS:
                continue
            # An annotation drawn in a series colour — an arrow, a CHF flag, a
            # highlight box — looks like a tiny cluster of marks. A real curve
            # spans a meaningful part of at least one axis.
            spread_x = (max(p[0] for p in pts) - min(p[0] for p in pts)) / frame.width
            spread_y = (max(p[1] for p in pts) - min(p[1] for p in pts)) / frame.height
            if max(spread_x, spread_y) < 0.04:
                continue
            slot = dict(slot, chained_from=chained_from, isolated_dropped=len(isolated))
            vector_series.append((key, pts, method, slot))

        vector_series = _merge_duplicates(vector_series, frame)

        if prefer != "raster" and vector_series:
            for key, pts, method, slot in vector_series:
                label = legend.get(key, "")
                # Points stay in the order they were recovered in — PDF draw
                # order for markers, chained stroke order for line vertices —
                # rather than being re-sorted by x. A boiling curve is not
                # always single-valued in wall superheat (CHF hysteresis,
                # transition-boiling reversal, a quench transient), and
                # forcing ascending x there braids separate branches into a
                # zig-zag. `points.json` records a path, not a lookup table;
                # anything that needs monotonic x (e.g. interpolation) sorts
                # its own copy at the point of use.
                data = [
                    [round(panel.x.to_data(px), 6), round(panel.y.to_data(py), 6)]
                    for px, py in pts
                ]
                conf = 0.90
                conf += 0.04 if min(panel.x.n_ticks, panel.y.n_ticks) >= 4 else -0.05
                conf += 0.02 if min(panel.x.r2, panel.y.r2) > 0.9999 else 0.0
                if not label:
                    conf -= 0.03
                notes = [
                    f"vector paths, style rgb={key[0]} {'filled' if key[1] else 'open'}",
                    f"x fit r2={panel.x.r2:.5f} on {panel.x.n_ticks} ticks; "
                    f"y fit r2={panel.y.r2:.5f} on {panel.y.n_ticks} ticks",
                    "points kept in recovered path order, not re-sorted by x",
                ]
                if len(slot.get("markers", [])) >= MIN_POINTS and slot.get("lines"):
                    notes.append("markers used; connecting line ignored")
                if slot.get("chained_from", 0) > 1:
                    notes.append(
                        f"{slot['chained_from']} stroke(s) of this colour chained "
                        "head-to-tail by nearest endpoint"
                    )
                if slot.get("isolated_dropped"):
                    notes.append(
                        f"dropped {slot['isolated_dropped']} isolated same-colour "
                        "mark(s) with no near neighbour (legend bullet or "
                        "marker-preview callout, not a data point)"
                    )
                if slot.get("merged"):
                    notes.append(
                        f"merged {slot['merged'] + 1} indistinguishable same-colour "
                        "path groups (fill + outline of one marker set)"
                    )
                out.append(
                    _series_dict(
                        figure_id + suffix, key, label, panel, data,
                        method, min(0.98, round(conf, 2)), notes, caption,
                    )
                )
        else:
            raster_out, ref_info = _raster_series(page, frame, clip, panel, dpi, zones=zones)
            for s in raster_out:
                data = [[round(x, 6), round(y, 6)] for x, y in s["points"]]
                notes = [
                    f"raster trace at {dpi} dpi, colour group '{s['style']}'",
                    f"x fit r2={panel.x.r2:.5f}; y fit r2={panel.y.r2:.5f}",
                    "no vector paths in plot area — flattened or scanned figure",
                ]
                if ref_info:
                    parts = []
                    if ref_info["rows"]:
                        parts.append(f"{ref_info['rows']} row(s)")
                    if ref_info["cols"]:
                        parts.append(f"{ref_info['cols']} column(s)")
                    notes.append(
                        f"excluded {' and '.join(parts)} of straight full-span ink "
                        "(a theory/limit/correlation line, e.g. a CHF ceiling) from "
                        "every colour trace before tracing, so an annotation drawn "
                        "in this series' own colour along that line isn't read as "
                        "data"
                    )
                out.append(
                    _series_dict(
                        figure_id + suffix, (s["style"], False, 0.0, False),
                        s["style"], panel, data,
                        "pixel_calibrated_digitization", 0.62,
                        notes, caption,
                    )
                )

    doc.close()
    if not out and pending:
        # One panel of a multi-panel figure can calibrate while another cannot —
        # a boiling curve stacked over a CHF scatter, say. Emitting the panels
        # that worked beats discarding the figure; only a figure where nothing
        # calibrated is a failure.
        raise pending[0]
    return out


def _panel_calibration(calibration: dict | None, index: int) -> dict | None:
    """Pick the calibration that applies to one panel.

    A flat spec applies to every panel; `{"panels": {"2": {...}}}` targets one.
    Multi-panel figures need the second form — panel (a) of huang-2023 Fig. 2 is
    temperature against time and panel (b) is the boiling curve, so one axis
    range cannot serve both.
    """
    if not calibration:
        return None
    panels = calibration.get("panels")
    if panels:
        return panels.get(str(index)) or panels.get(index)
    return calibration


def _series_dict(figure_id, key, label, panel, data, method, confidence, notes,
                 caption: str = "") -> dict:
    colour = key[0]
    cid = _slug(label) if label else (
        "rgb_" + "_".join(str(int(round(v * 255))) for v in (colour or (0, 0, 0)))
    )
    if key[1] is False and label:
        pass
    return {
        "series_id": f"{figure_id}-{cid}",
        "figure_id": figure_id,
        "label": label,
        "x_axis": {
            "quantity": panel.x.quantity or "x",
            "unit": panel.x.unit,
            "scale": panel.x.scale,
        },
        "y_axis": {
            "quantity": panel.y.quantity or "y",
            "unit": panel.y.unit,
            "scale": panel.y.scale,
        },
        "points": data,
        # The caption travels with the series: it is the only place the fluid,
        # the pressure and often the surface are stated, and a point table that
        # has lost them cannot be filtered safely.
        "conditions": {"figure_caption": caption[:300]} if caption else {},
        "confidence": confidence,
        "uncertainty": {"method": method},
        "notes": "; ".join(notes),
    }


def digitize_crop(crop: Crop, source_pdf: str | Path, **kw) -> list[dict]:
    """Digitize one Crop from the figure manifest."""
    return digitize_figure(
        source_pdf, crop.page, crop.bbox, figure_id=crop.element_id,
        caption=f"{crop.label} {crop.caption}".strip(), **kw
    )


# ------------------------------------------------------- provider interface


class DeterministicDigitizer:
    """FigurePointProvider implementation. No model, no network, no cost."""

    name = "mhtdb-digitize/v1"

    def __init__(self, source_pdf: str | Path, dpi: int = 400):
        self.source_pdf = str(source_pdf)
        self.dpi = dpi

    def extract(self, figures) -> list[dict]:
        out: list[dict] = []
        for fi in figures:
            if not fi.bbox:
                continue
            try:
                out += digitize_figure(
                    self.source_pdf, fi.page, fi.bbox,
                    figure_id=fi.figure_id, dpi=self.dpi,
                )
            except (NeedsCalibration, RuntimeError):
                continue
        return out
