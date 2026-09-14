"""S8 — tight figure/table crops, one file per element.

`s0_ingest` detects a figure from its *caption line* and, when asked for an
image, renders the **whole page**. That is enough to show a reader where a
figure lives, and not nearly enough to digitize it: the digitizer needs the
plot and only the plot, at a resolution worth tracing.

This module finds the graphic itself. For every page it clusters the page's
drawable content — embedded images and vector drawing bboxes — into connected
components, matches each cluster to the caption that refers to it, and writes
the union of the two as its own single-page PDF (vector fidelity preserved,
which is what makes `digitize`'s vector path possible) plus a preview image —
PNG for line art, JPEG for scans and photographs.

Crops land beside the existing PNGs in `pipeline/figures/<record-id>/` and are
keyed by the same `fig-N` / `tab-N` ids the figure manifest and
`schema/point.schema.json` already use, so a series can be traced from
dashboard back to crop back to source page without a mapping table.

Caption matching is a heuristic and says so: every crop carries a
`caption_confidence`, and a cluster with no plausible caption is still emitted
under a sequential id rather than dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

# A caption opener at the start of a text block. Deliberately stricter than the
# line-level regex in s0_ingest: a block beginning "Fig. 4 shows that ..." is a
# cross-reference in body text, not a caption, and is rejected below.
_CAPTION = re.compile(
    r"^\s*(fig(?:ure)?|table|tab)\s*\.?\s*(\d+[a-z]?)\s*[.:—–-]?\s+(.{0,600})",
    re.I | re.S,
)
# Body-text cross references: "Fig. 5 shows", "Table 2 summarizes", "Fig. 3 and".
_CROSSREF = re.compile(
    r"^\s*(fig(?:ure)?|table|tab)\s*\.?\s*\d+[a-z]?\s+"
    r"(shows?|show|summariz|present|display|list|report|illustrat|give|depict|"
    r"provides?|compares?|plots?|and|in|of|for|is|are|was|were|can|reports?)",
    re.I,
)

# Geometry, in PDF points (72/inch).
MERGE_GAP = 14.0        # clusters closer than this merge — sub-panels (a)(b)(c)
MIN_SIDE = 45.0         # ignore clusters smaller than this on either side
MIN_AREA = 6000.0       # ...or smaller than this in area (rules, logos, glyphs)
CAPTION_GAP = 90.0      # a caption further than this from its graphic is a guess
PAD = 4.0               # breathing room around the union rect
AXIS_LABEL_MARGIN = 42.0    # bridges a tick-label column + a rotated axis title
AXIS_LABEL_MAX_CHARS = 40   # longer than any real tick label or axis title


@dataclass
class Crop:
    """One extracted visual element and where it came from."""

    element_id: str                 # "fig-4" / "tab-2" / "fig-p3-01" if uncaptioned
    kind: str                       # "figure" | "table"
    label: str                      # "Fig. 4" as printed, or "" when uncaptioned
    caption: str
    page: int                       # 1-indexed
    bbox: tuple[float, float, float, float]
    pdf_path: str | None = None
    preview_path: str | None = None   # .png for line art, .jpg for scans
    n_panels: int = 1               # graphic sub-clusters merged into this crop
    has_vector: bool = False        # vector paths inside the graphic area
    has_raster: bool = False        # embedded image inside the graphic area
    caption_confidence: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bbox"] = list(self.bbox)
        return d


# --------------------------------------------------------------- clustering


@dataclass
class _Cluster:
    rect: "fitz.Rect"
    n: int = 1
    vector: bool = False
    raster: bool = False
    parts: list = field(default_factory=list)


def _page_furniture(rect, page_rect) -> bool:
    """Header/footer rules, page borders, column separators — never a figure."""
    w, h = rect.width, rect.height
    if h < 3 and w > page_rect.width * 0.5:      # horizontal rule
        return True
    if w < 3 and h > page_rect.height * 0.5:     # vertical rule / column line
        return True
    if rect.y1 < page_rect.height * 0.08 or rect.y0 > page_rect.height * 0.94:
        return True                              # running head / folio
    return False


def _graphic_boxes(page) -> list[tuple["fitz.Rect", bool, bool]]:
    """Every drawable thing on the page: (rect, is_vector, is_raster)."""
    out: list[tuple[fitz.Rect, bool, bool]] = []
    pr = page.rect

    for b in page.get_text("dict")["blocks"]:
        if b["type"] == 1:  # image block
            r = fitz.Rect(b["bbox"])
            if r.width >= 20 and r.height >= 20:
                out.append((r, False, True))

    for d in page.get_drawings():
        r = fitz.Rect(d["rect"])
        if r.is_empty or r.is_infinite:
            continue
        if _page_furniture(r, pr):
            continue
        out.append((r, True, False))

    return out


def _cluster(boxes, gap: float = MERGE_GAP) -> list[_Cluster]:
    """Union-find over bbox proximity. Sub-panels of one figure end up together."""
    clusters: list[_Cluster] = []
    for r, vec, ras in boxes:
        clusters.append(_Cluster(rect=fitz.Rect(r), vector=vec, raster=ras, parts=[fitz.Rect(r)]))

    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            if clusters[i] is None:
                continue
            for j in range(i + 1, len(clusters)):
                if clusters[j] is None:
                    continue
                a, b = clusters[i].rect, clusters[j].rect
                grown = fitz.Rect(a.x0 - gap, a.y0 - gap, a.x1 + gap, a.y1 + gap)
                if grown.intersects(b):
                    clusters[i].rect = a | b
                    clusters[i].n += clusters[j].n
                    clusters[i].vector |= clusters[j].vector
                    clusters[i].raster |= clusters[j].raster
                    clusters[i].parts += clusters[j].parts
                    clusters[j] = None
                    merged = True
    return [c for c in clusters if c is not None]


def _significant(c: _Cluster) -> bool:
    r = c.rect
    if r.width < MIN_SIDE or r.height < MIN_SIDE:
        return False
    return r.width * r.height >= MIN_AREA


def _is_table_furniture(c: _Cluster) -> bool:
    """A cluster that is mostly thin horizontal rules is a table's ruling.

    Journal tables are drawn as a stack of hairlines with text between them.
    Left alone, that stack clusters into something figure-shaped and steals the
    nearest figure caption — which is how "Fig. 3" ends up cropping Table 1.
    """
    if c.raster:
        return False
    rules = sum(1 for p in c.parts if p.height < 3.5 and p.width > 40)
    return len(c.parts) >= 4 and rules >= 0.6 * len(c.parts)


def _substantial_unlabelled(c: _Cluster) -> bool:
    """Bar for keeping a cluster that matched no caption.

    Uncaptioned plots are common and worth digitizing; running heads, logo
    bands and single stray boxes are not. Demand either a real embedded image
    or a genuinely drawn figure (many paths), not one rectangle.
    """
    if c.raster and c.rect.width * c.rect.height >= 8000:
        return True
    return len(c.parts) >= 8


def _looks_like_axis_text(text: str, r: "fitz.Rect") -> bool:
    """Is this text block a tick label or axis title, not a caption or body text?

    Axis labels are never part of a vector drawing or an embedded image --
    they're page text -- so `_graphic_boxes` never sees them, and a cluster's
    rect is always cropped exactly at the plot's own drawn lines. That is
    precisely where the axis numbers a digitizer needs to calibrate against
    live, since they're printed just outside the plot border. Short length
    plus a shape that's either a single compact line (a tick number) or
    tall-and-narrow (a rotated axis title) tells them apart from a caption or
    a paragraph, which are long, and from a section heading, which is wide.
    """
    text = text.strip()
    if not text or len(text) > AXIS_LABEL_MAX_CHARS:
        return False
    if _CAPTION.match(text) or _CROSSREF.match(text):
        return False
    rotated = r.height > 3 * max(r.width, 1.0)
    if rotated:
        return True
    # A non-rotated axis element is a tick number or a short axis title --
    # never more than a handful of words. A section heading ("3.6. Boiling
    # heat transfer coefficient") is just as short and single-line, but
    # reads as a sentence fragment; capping the word count separates them
    # without hand-listing every possible axis-title wording.
    return r.height < 20 and r.width < 150 and len(text.split()) <= 4


def _expand_for_axis_labels(rect: "fitz.Rect", page, exclude: list["fitz.Rect"]) -> "fitz.Rect":
    """Grow a figure's rect to swallow nearby axis tick labels and titles.

    Only text just outside one edge of the *original* rect, overlapping its
    span along the other axis, and axis-label-shaped (see
    `_looks_like_axis_text`) qualifies. Checked against the original
    boundary rather than re-checked after each addition on purpose: a tick
    label and the axis title beside it are typically both within margin of
    the plot's own drawn edge already, so one pass catches them together,
    while re-evaluating against a rect that keeps growing would let a chain
    of short blocks walk the crop straight into a neighbouring column.
    """
    fixed = fitz.Rect(rect)
    grown = fitz.Rect(rect)
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        br = fitz.Rect(b["bbox"])
        text = "".join(sp["text"] for ln in b["lines"] for sp in ln["spans"])
        if not _looks_like_axis_text(text, br):
            continue
        if any(not (br & ex).is_empty for ex in exclude):
            continue
        near_left = 0 <= fixed.x0 - br.x1 <= AXIS_LABEL_MARGIN
        near_right = 0 <= br.x0 - fixed.x1 <= AXIS_LABEL_MARGIN
        near_top = 0 <= fixed.y0 - br.y1 <= AXIS_LABEL_MARGIN
        near_bottom = 0 <= br.y0 - fixed.y1 <= AXIS_LABEL_MARGIN
        v_overlap = br.y1 >= fixed.y0 - 6 and br.y0 <= fixed.y1 + 6
        h_overlap = br.x1 >= fixed.x0 - 6 and br.x0 <= fixed.x1 + 6
        if ((near_left or near_right) and v_overlap) or ((near_top or near_bottom) and h_overlap):
            grown |= br
    return grown


# ------------------------------------------------------------------ captions


@dataclass
class _Caption:
    kind: str
    number: str
    label: str
    text: str
    rect: "fitz.Rect"
    used: bool = False


def _captions(page) -> list[_Caption]:
    caps: list[_Caption] = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        text = " ".join(s["text"] for l in b["lines"] for s in l["spans"]).strip()
        if not text:
            continue
        m = _CAPTION.match(text)
        if not m or _CROSSREF.match(text):
            continue
        word, num, rest = m.group(1), m.group(2), m.group(3).strip()
        kind = "table" if word.lower().startswith("tab") else "figure"
        caps.append(
            _Caption(
                kind=kind,
                number=num,
                label=f"{word} {num}",
                text=re.sub(r"\s+", " ", rest)[:400],
                rect=fitz.Rect(b["bbox"]),
            )
        )
    return caps


def _score(cluster: _Cluster, cap: _Caption) -> float:
    """How well one caption explains one graphic cluster. 0 means "not a match".

    A figure caption sits below its graphic and shares its horizontal extent.
    Distance is the vertical gap; horizontal overlap breaks ties and kills
    matches from the other column. Captions frequently overlap the graphic's
    bbox slightly (stray glyphs inflate the cluster), so a modest negative gap
    is tolerated rather than rejected.
    """
    r = cluster.rect
    if cap.kind != "figure":
        return 0.0
    overlap = min(r.x1, cap.rect.x1) - max(r.x0, cap.rect.x0)
    narrow = min(r.width, cap.rect.width)
    if narrow <= 0 or overlap <= 0.25 * narrow:
        return 0.0

    slack = -min(40.0, 0.2 * r.height)
    below = cap.rect.y0 - r.y1
    above = r.y0 - cap.rect.y1
    if below >= slack:
        gap, orientation = max(0.0, below), 1.0
    elif above >= slack:
        gap, orientation = max(0.0, above), 0.6   # caption above: legal, rarer
    else:
        return 0.0
    if gap > CAPTION_GAP:
        return 0.0

    score = (1.0 - gap / CAPTION_GAP) * orientation
    score *= 0.6 + 0.4 * (overlap / narrow)
    return min(1.0, score)


def _assign(clusters: list[_Cluster], caps: list[_Caption]) -> dict[int, tuple[_Caption, float]]:
    """Globally greedy caption assignment: best pair first, then the next best.

    Per-cluster greedy matching lets the first cluster on the page consume a
    caption that belongs to a later one; scoring every pair and taking them in
    descending order avoids that without needing a full Hungarian solve.
    """
    pairs = [
        (_score(cl, cap), i, j)
        for i, cl in enumerate(clusters)
        for j, cap in enumerate(caps)
    ]
    pairs = [p for p in pairs if p[0] > 0]
    pairs.sort(key=lambda p: -p[0])

    out: dict[int, tuple[_Caption, float]] = {}
    taken_caps: set[int] = set()
    for score, i, j in pairs:
        if i in out or j in taken_caps:
            continue
        out[i] = (caps[j], round(score, 2))
        taken_caps.add(j)
        caps[j].used = True
    return out


def _inside_any(rect, others, frac: float = 0.6) -> bool:
    """True when `frac` of rect's area falls inside any of `others`."""
    area = rect.width * rect.height
    if area <= 0:
        return False
    for o in others:
        inter = rect & o
        if not inter.is_empty and (inter.width * inter.height) >= frac * area:
            return True
    return False


def _table_rect(cap: _Caption, page, clusters: list[_Cluster]) -> "fitz.Rect":
    """A table's body is text, not graphics: take the block run under the caption.

    Stop at the next caption, at a large vertical gap, or at the column edge.
    Rule lines that fall inside are folded in via the cluster list.
    """
    r = fitz.Rect(cap.rect)
    prev_y1 = cap.rect.y1
    for b in sorted(page.get_text("dict")["blocks"], key=lambda b: b["bbox"][1]):
        br = fitz.Rect(b["bbox"])
        if br.y0 < cap.rect.y1 - 2:
            continue
        overlap = min(r.x1, br.x1) - max(r.x0, br.x0)
        if overlap <= 0.2 * min(r.width, br.width):
            continue
        if br.y0 - prev_y1 > 26:            # column/paragraph break
            break
        if b["type"] == 0:
            text = " ".join(s["text"] for l in b["lines"] for s in l["spans"]).strip()
            if _CAPTION.match(text) and not _CROSSREF.match(text):
                break                        # next element's caption
        r |= br
        prev_y1 = br.y1
    for c in clusters:                       # fold in rule lines drawn over it
        if r.intersects(c.rect) and c.rect.height < r.height:
            r |= c.rect
    return r


# ------------------------------------------------------------------- writing


MAX_PREVIEW_PX = 2200       # long edge of the preview image
MAX_VECTOR_PDF = 500_000    # above this, a photo-heavy crop is flattened
JPEG_QUALITY = 82


def _write_crop(src, page, rect, out_dir: Path, stem: str, dpi: int) -> tuple[str, str]:
    """One clipped single-page PDF plus a preview image.

    The PDF keeps the original drawing commands, which is what makes
    `digitize`'s vector path possible — but `show_pdf_page` clips *visually*,
    so a crop taken from a scanned page still embeds that page's full bitmap.
    On a 300-page scanned report that turns 30 crops into 77 MB. A crop with no
    vector content that lands over the size cap is therefore rewritten as just
    its own pixels: nothing is lost, because there were no paths to preserve.

    Format follows content for the same reason. Line art is PNG — crisp and
    small. A scan or a photograph is JPEG, which is ~8x smaller on exactly the
    material where lossless compression buys nothing but noise.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{stem}.pdf"
    vector = _has_vector(page, rect)

    dst = fitz.open()
    newp = dst.new_page(width=rect.width, height=rect.height)
    newp.show_pdf_page(newp.rect, src, page.number, clip=rect)
    dst.save(str(pdf_path), garbage=4, deflate=True)
    dst.close()

    preview_dpi = int(min(dpi, 72.0 * MAX_PREVIEW_PX / max(rect.width, rect.height)))
    preview = out_dir / f"{stem}{'.png' if vector else '.jpg'}"
    try:
        pix = page.get_pixmap(clip=rect, dpi=preview_dpi)
        if vector:
            pix.save(str(preview))
        else:
            preview.write_bytes(pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY))
    except Exception:
        preview = None

    # Keep drawing commands only where they are worth keeping. A crop that is
    # mostly photograph — SEM panels, apparatus shots — carries no curve to
    # extract, so an oversized one is flattened even though a stray rule or
    # scale bar makes it technically "vector".
    if pdf_path.stat().st_size > MAX_VECTOR_PDF and (
        not vector or _image_coverage(page, rect) > 0.6
    ):
        _rasterize_crop(page, rect, pdf_path, dpi)

    return str(pdf_path), (str(preview) if preview else None)


def _has_vector(page, rect) -> bool:
    """Are there real drawing paths inside this region, or only images?"""
    for it in page.get_drawings():
        r = fitz.Rect(it["rect"])
        if rect.intersects(r) and min(r.width, r.height) > 1.5:
            return True
    return False


def _image_coverage(page, rect) -> float:
    """Fraction of the crop covered by embedded raster images."""
    area = rect.get_area()
    if area <= 0:
        return 0.0
    covered = 0.0
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 1:
            continue
        inter = fitz.Rect(b["bbox"]) & rect
        if not inter.is_empty:
            covered += inter.get_area()
    return min(1.0, covered / area)


def _rasterize_crop(page, rect, pdf_path: Path, dpi: int) -> None:
    """Replace a crop PDF with one holding only the cropped pixels, as JPEG."""
    try:
        pix = page.get_pixmap(clip=rect, dpi=dpi)
        doc = fitz.open()
        pg = doc.new_page(width=rect.width, height=rect.height)
        pg.insert_image(pg.rect, stream=pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY))
        doc.save(str(pdf_path), garbage=4, deflate=True)
        doc.close()
    except Exception:
        pass          # keep the oversized-but-correct original


def extract_crops(
    pdf_path: str | Path,
    out_dir: str | Path,
    dpi: int = 300,
    tables: bool = True,
) -> list[Crop]:
    """Crop every figure and table in one PDF into its own file.

    Returns the crop manifest. Ordering is page-then-position, and ids prefer
    the printed number (`fig-4`) so they line up with `s0_ingest`'s figures;
    an uncaptioned graphic gets a positional id (`fig-p3-01`) rather than being
    dropped, because an unlabelled plot is still digitizable.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for figure crops: pip install pymupdf")

    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    src = fitz.open(pdf_path)
    stem = pdf_path.stem
    crops: list[Crop] = []
    seen: set[str] = set()

    for page in src:
        pno = page.number + 1
        caps = _captions(page)
        raw = [
            c for c in _cluster(_graphic_boxes(page))
            if _significant(c) and not _is_table_furniture(c)
        ]
        # Resolve table bodies first so their ruling can't masquerade as a
        # figure: a cluster sitting inside a table is that table's ruling.
        table_rects = [
            _table_rect(c, page, raw) for c in caps if c.kind == "table"
        ] if tables else []
        clusters = [c for c in raw if not _inside_any(c.rect, table_rects)]
        clusters.sort(key=lambda c: (c.rect.y0, c.rect.x0))
        assigned = _assign(clusters, caps)

        for idx, cl in enumerate(clusters, 1):
            cap, conf = assigned.get(idx - 1, (None, 0.0))
            if cap is None and not _substantial_unlabelled(cl):
                continue
            if cap:
                eid = f"fig-{cap.number}"
                label, caption = cap.label, cap.text
                rect = cl.rect | cap.rect
                rect = _expand_for_axis_labels(rect, page, exclude=[cap.rect])
            else:
                eid = f"fig-p{pno}-{idx:02d}"
                label, caption = "", ""
                rect = _expand_for_axis_labels(fitz.Rect(cl.rect), page, exclude=[])

            if eid in seen:                  # same number twice (continued panels)
                eid = f"{eid}-p{pno}-{idx:02d}"
            seen.add(eid)

            rect = fitz.Rect(rect.x0 - PAD, rect.y0 - PAD, rect.x1 + PAD, rect.y1 + PAD)
            rect &= page.rect
            pdf_out, png_out = _write_crop(src, page, rect, out_dir, eid, dpi)
            crops.append(
                Crop(
                    element_id=eid, kind="figure", label=label, caption=caption,
                    page=pno, bbox=tuple(rect), pdf_path=pdf_out,
                    preview_path=png_out,
                    n_panels=len(cl.parts), has_vector=cl.vector, has_raster=cl.raster,
                    caption_confidence=conf,
                )
            )

        if not tables:
            continue
        for cap in caps:
            if cap.kind != "table" or cap.used:
                continue
            cap.used = True
            eid = f"tab-{cap.number}"
            if eid in seen:
                eid = f"{eid}-p{pno}"
            seen.add(eid)
            rect = _table_rect(cap, page, clusters)
            rect = fitz.Rect(rect.x0 - PAD, rect.y0 - PAD, rect.x1 + PAD, rect.y1 + PAD)
            rect &= page.rect
            if rect.height < 24:             # caption with no body found
                continue
            pdf_out, png_out = _write_crop(src, page, rect, out_dir, eid, dpi)
            crops.append(
                Crop(
                    element_id=eid, kind="table", label=cap.label, caption=cap.text,
                    page=pno, bbox=tuple(rect), pdf_path=pdf_out,
                    preview_path=png_out, caption_confidence=1.0,
                )
            )

    src.close()
    return crops


def write_manifest(record_id: str, crops: list[Crop], out_path: str | Path) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"record_id": record_id, "n_crops": len(crops),
             "crops": [c.to_dict() for c in crops]},
            indent=2,
        ),
        encoding="utf-8",
    )
    return p


def load_manifest(path: str | Path) -> list[Crop]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for c in d["crops"]:
        c = {**c, "bbox": tuple(c["bbox"])}
        if "png_path" in c:                      # manifests written before the
            c["preview_path"] = c.pop("png_path")  # png/jpg split
        out.append(Crop(**c))
    return out
