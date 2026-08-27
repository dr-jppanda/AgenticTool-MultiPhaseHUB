"""S0 — PDF in, DocumentModel out.

Produces the page/section index the locator needs, and the figure crops the
external digitization pipeline consumes. PyMuPDF is the only hard dependency;
swap `_extract_sections` for GROBID or docling if heading detection proves weak
on your corpus — the rest of the pipeline only depends on the DocumentModel
shape, not on how it was built.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .docmodel import DocumentModel, Figure, Page, Section, Table, normalize_text

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

# "3.2 Experimental apparatus", "IV. Results", "2 Methods"
_NUMBERED = re.compile(
    r"^\s*((?:\d+\.)*\d+|[IVXLC]+\.)\s+([A-Z][^\n]{2,80})\s*$"
)
# Unnumbered but conventional headings.
_KNOWN_HEADINGS = {
    "abstract", "introduction", "background", "literature review",
    "experimental apparatus", "experimental setup", "experimental method",
    "materials and methods", "methodology", "test facility", "test section",
    "data reduction", "uncertainty analysis", "results",
    "results and discussion", "discussion", "conclusion", "conclusions",
    "nomenclature", "acknowledgements", "acknowledgments", "references",
    "appendix",
}

_FIG_CAPTION = re.compile(
    r"^\s*(fig(?:ure)?\.?\s*(\d+[a-z]?))\s*[.:—-]?\s*(.{0,400})", re.I | re.S
)
_TAB_CAPTION = re.compile(
    r"^\s*(tab(?:le)?\.?\s*(\d+[a-z]?))\s*[.:—-]?\s*(.{0,400})", re.I | re.S
)


def ingest_pdf(pdf_path: str | Path, figures_dir: str | Path | None = None) -> DocumentModel:
    """Build a DocumentModel from a born-digital PDF."""
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for PDF ingest: pip install pymupdf")

    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)

    parts: list[str] = []
    pages: list[Page] = []
    heading_candidates: list[tuple[int, str, int]] = []  # (offset, text, level)
    figures: list[Figure] = []
    tables: list[Table] = []

    body_size = _modal_font_size(doc)
    cursor = 0

    for pno, page in enumerate(doc, start=1):
        page_start = cursor
        blocks = page.get_text("dict")["blocks"]
        page_chunks: list[str] = []

        for block in blocks:
            if block.get("type") != 0:  # not a text block
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = "".join(s["text"] for s in spans).strip()
                if not line_text:
                    continue
                max_size = max(s["size"] for s in spans)
                bold = any("bold" in s["font"].lower() for s in spans)

                if _looks_like_heading(line_text, max_size, body_size, bold):
                    level = 1 if max_size >= body_size * 1.25 else 2
                    heading_candidates.append(
                        (page_start + sum(len(c) for c in page_chunks), line_text, level)
                    )
                page_chunks.append(line_text + "\n")

        page_text = "".join(page_chunks)
        parts.append(page_text)
        cursor += len(page_text)
        pages.append(Page(number=pno, start=page_start, end=cursor))

        figures.extend(_extract_figures(page, pno, page_text, figures_dir, pdf_path.stem))
        tables.extend(_extract_table_stubs(pno, page_text))

    raw = "".join(parts)
    text, omap = normalize_text(raw)

    # Heading offsets were computed against `raw`; project them into `text`.
    raw_to_norm: dict[int, int] = {}
    for n_idx, r_idx in enumerate(omap):
        raw_to_norm.setdefault(r_idx, n_idx)

    def project(raw_off: int) -> int:
        for probe in range(raw_off, min(raw_off + 64, len(raw))):
            if probe in raw_to_norm:
                return raw_to_norm[probe]
        return min(raw_off, len(text))

    # Page spans also need projecting.
    for p in pages:
        p.start, p.end = project(p.start), project(p.end)
    if pages:
        pages[-1].end = len(text)

    sections = _build_sections(
        [(project(off), t, lvl) for off, t, lvl in heading_candidates], len(text)
    )

    return DocumentModel(
        doc_id=DocumentModel.content_hash(text),
        source=str(pdf_path),
        text=text,
        pages=pages,
        sections=sections,
        figures=figures,
        tables=tables,
        meta={"n_pages": len(pages), "ingest": "pymupdf"},
    )


def _modal_font_size(doc) -> float:
    from collections import Counter

    sizes: Counter = Counter()
    for page in list(doc)[: min(6, doc.page_count)]:
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span["text"].strip():
                        sizes[round(span["size"], 1)] += len(span["text"])
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _looks_like_heading(text: str, size: float, body: float, bold: bool) -> bool:
    if len(text) > 90 or text.endswith((".", ",", ";")):
        return False
    if _NUMBERED.match(text):
        return True
    if text.strip().lower().rstrip(":") in _KNOWN_HEADINGS:
        return True
    return (size >= body * 1.15 or bold) and len(text.split()) <= 10 and text[:1].isupper()


def _build_sections(cands: list[tuple[int, str, int]], doc_len: int) -> list[Section]:
    cands = sorted(cands, key=lambda c: c[0])
    out: list[Section] = []
    for i, (off, raw_title, level) in enumerate(cands):
        m = _NUMBERED.match(raw_title)
        number, title = (m.group(1).rstrip("."), m.group(2).strip()) if m else (None, raw_title.strip())
        end = cands[i + 1][0] if i + 1 < len(cands) else doc_len
        slug = f"sec-{number.replace('.', '-')}" if number else f"sec-{i}-{_slug(title)}"
        out.append(
            Section(id=slug, number=number, title=title, level=level, start=off, end=end)
        )
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def _extract_figures(page, pno, page_text, figures_dir, stem) -> list[Figure]:
    """Emit one Figure per detected caption, with a rendered crop when possible.

    This is the *input* slot for the external digitization pipeline.
    """
    figs: list[Figure] = []
    for line in page_text.split("\n"):
        m = _FIG_CAPTION.match(line)
        if not m:
            continue
        label, num, caption = m.group(1).strip(), m.group(2), m.group(3).strip()
        fig_id = f"fig-{num}"
        image_path = None
        if figures_dir:
            out_dir = Path(figures_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            image_path = str(out_dir / f"{stem}_{fig_id}_p{pno}.png")
            try:
                pix = page.get_pixmap(dpi=200)
                pix.save(image_path)
            except Exception:
                image_path = None
        figs.append(
            Figure(
                id=fig_id,
                label=label,
                caption=caption,
                page=pno,
                bbox=tuple(page.rect) if hasattr(page, "rect") else None,
                image_path=image_path,
            )
        )
    return figs


def _extract_table_stubs(pno: int, page_text: str) -> list[Table]:
    out: list[Table] = []
    for line in page_text.split("\n"):
        m = _TAB_CAPTION.match(line)
        if m:
            out.append(Table(id=f"tab-{m.group(2)}", caption=m.group(3).strip(), page=pno))
    return out


def save_docmodel(doc: DocumentModel, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc.to_dict(), indent=2), encoding="utf-8")


def load_docmodel(path: str | Path) -> DocumentModel:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return DocumentModel(
        doc_id=d["doc_id"],
        source=d["source"],
        text=d["text"],
        pages=[Page(**p) for p in d["pages"]],
        sections=[Section(**s) for s in d["sections"]],
        figures=[Figure(**f) for f in d["figures"]],
        tables=[Table(**t) for t in d["tables"]],
        meta=d.get("meta", {}),
    )
