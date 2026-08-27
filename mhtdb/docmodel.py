"""Page- and section-indexed document model, plus the quote locator.

This module is the provenance core. The extraction stages ask the model only for
verbatim quotes; `DocumentModel.locate()` maps each quote back to the pages and
sections it overlaps. A quote that cannot be located is rejected, so the same
function serves as both the provenance resolver and the hallucination check.

Nothing here calls an LLM.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Iterable

# Characters PDFs love and plain text does not.
_CHAR_FIXES = {
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "·": ".", "−": "-",
}

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")

# PDFs break words across lines with a hyphen: "formu-\nlation". The model
# quotes the reassembled word, so leaving the break in place makes every quote
# spanning one fail to match. Measured on this corpus: 352 occurrences across
# five papers, 204 in a single one.
#
# Deliberately narrow — lowercase on BOTH sides only. That keeps real hyphens
# in identifiers and compounds intact: "R-134a", "T-sat", "10-20", and
# "high-\nFlux" all survive, because joining those would corrupt the term.
_HYPHEN_BREAK = re.compile(r"(?<=[a-z])-[ \t]*\n[ \t]*(?=[a-z])")

# The ambiguity this cannot resolve from text alone: a hyphen at a line end is
# either a typesetter's syllable break ("formu-lation", join it) or a real
# compound hyphen that happened to land there ("two-phase", keep it).
#
# Joining is the safer default — a missed join breaks provenance outright,
# because the model quotes the reassembled word and the span then fails to
# locate, whereas a wrong join only produces a cosmetically fused word that
# still matches. But the compounds below are frequent enough in this literature
# that mangling them is not acceptable, so their hyphen is preserved.
_COMPOUND_HEADS = frozenset("""
two three four multi single dual non self semi sub super inter intra cross
pre post anti quasi pseudo micro nano macro meso high low mid well ill
long short large small thin thick fine coarse open closed
counter co re over under near far first second third
steady state phase flow heat mass free forced
""".split())

# Fuzzy match must clear this to count as located.
FUZZY_THRESHOLD = 0.86


def normalize_text(raw: str) -> tuple[str, list[int]]:
    """Normalize `raw` and return (normalized, offset_map).

    `offset_map[i]` is the index in `raw` that produced `normalized[i]`, so a
    span found in normalized space can always be reported against the original.
    Ligatures expand; dash and quote variants fold to ASCII. Case is preserved —
    quotes are reported verbatim.

    A whitespace run collapses to a single character: a newline if the run
    contained one, otherwise a space. Keeping line structure matters for
    heading and title detection; matching is made newline-insensitive by
    `_fold`, which substitutes newlines for spaces without changing length, so
    character spans stay aligned with `text`.

    Idempotent: normalizing already-normalized text is a no-op.
    """
    out: list[str] = []
    omap: list[int] = []
    pending: str | None = None  # None | " " | "\n"

    # Indices dropped outright (hyphen + line break inside a split word).
    # Dropping rather than substituting keeps the offset map honest: those
    # input positions simply produce no output character.
    drop: set[int] = set()
    for m in _HYPHEN_BREAK.finditer(raw):
        head = re.search(r"[a-z]+$", raw[max(0, m.start() - 24) : m.start()])
        if head and head.group(0) in _COMPOUND_HEADS:
            continue  # a real compound hyphen — keep it
        drop.update(range(m.start(), m.end()))

    for i, ch in enumerate(raw):
        if i in drop:
            continue
        rep = _CHAR_FIXES.get(ch)
        if rep is None:
            # Symbol-font PDFs emit C0 control bytes for glyphs the extractor
            # cannot map — this corpus has \x01 for '×' and \x03 for '°'. A
            # language model cannot reproduce those in a quote, so they would
            # break every span that contains one. Treat them as whitespace.
            if ch != "\n" and ch != "\t" and (ord(ch) < 0x20 or ord(ch) == 0x7F):
                rep = " "
            else:
                # NFKC catches the long tail (superscripts, full-width forms).
                rep = unicodedata.normalize("NFKC", ch)
        if rep == "" or rep.isspace():
            if "\n" in (rep or ch) or ch == "\n":
                pending = "\n"
            elif pending is None:
                pending = " "
            continue
        if pending is not None and out:
            out.append(pending)
            omap.append(i)
        pending = None
        for c in rep:
            out.append(c)
            omap.append(i)

    return "".join(out), omap


def _fold(s: str) -> str:
    """Fold used only for matching, never for display.

    Length-preserving relative to `normalize_text` output, so spans found here
    index correctly into `DocumentModel.text`.
    """
    s, _ = normalize_text(s)
    return s.replace("\n", " ").lower()


@dataclass
class Page:
    number: int  # 1-indexed, as printed in a PDF reader
    start: int   # char offset into DocumentModel.text
    end: int


@dataclass
class Section:
    id: str                 # stable slug, e.g. "sec-3-2"
    number: str | None      # "3.2" when the heading is numbered
    title: str
    level: int
    start: int
    end: int

    def label(self) -> str:
        return f"{self.number} {self.title}".strip() if self.number else self.title


@dataclass
class Figure:
    """Input slot for the external figure-digitization pipeline.

    `s0_ingest` populates these; the digitizer consumes `image_path` + `caption`
    and returns point series conforming to schema/point.schema.json.
    """
    id: str                 # "fig-5"
    label: str              # "Figure 5" as printed
    caption: str
    page: int
    bbox: tuple[float, float, float, float] | None = None
    image_path: str | None = None


@dataclass
class Table:
    id: str
    caption: str
    page: int
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Locus:
    """One resolved location of a quote."""
    char_span: tuple[int, int]
    pages: list[int]
    sections: list[dict]
    match: str          # "exact" | "fuzzy"
    score: float

    def to_dict(self) -> dict:
        return {
            "char_span": list(self.char_span),
            "pages": self.pages,
            "sections": self.sections,
            "match": self.match,
            "score": round(self.score, 4),
        }


@dataclass
class DocumentModel:
    doc_id: str
    source: str
    text: str
    pages: list[Page] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    # -- indexing helpers -------------------------------------------------

    def pages_for(self, start: int, end: int) -> list[int]:
        return [p.number for p in self.pages if p.start < end and p.end > start]

    def sections_for(self, start: int, end: int) -> list[dict]:
        hits = [s for s in self.sections if s.start < end and s.end > start]
        # Deepest-first is more useful to a reader than document order.
        hits.sort(key=lambda s: (s.start, -s.level))
        return [
            {"id": s.id, "number": s.number, "title": s.title, "level": s.level}
            for s in hits
        ]

    # -- the locator ------------------------------------------------------

    def locate(self, quote: str, max_hits: int = 4) -> list[Locus]:
        """Resolve a verbatim quote to pages and sections.

        Returns every distinct location, so a quote appearing in both an
        abstract and a results section yields two loci. An empty list means the
        quote is not in the document — the caller must reject the field.
        """
        q = quote.strip()
        if len(q) < 12:
            # Too short to attribute with confidence; treat as unlocatable
            # rather than matching a common fragment in ten places.
            return []

        hay = _fold(self.text)
        needle = _fold(q)
        if not needle:
            return []

        # 1. Exact (post-normalization) match.
        loci: list[Locus] = []
        pos = hay.find(needle)
        while pos != -1 and len(loci) < max_hits:
            span = (pos, pos + len(needle))
            loci.append(
                Locus(
                    char_span=span,
                    pages=self.pages_for(*span),
                    sections=self.sections_for(*span),
                    match="exact",
                    score=1.0,
                )
            )
            pos = hay.find(needle, pos + 1)
        if loci:
            return loci

        # 2. Fuzzy fallback, anchored on the rarest token in the quote so we
        #    only score a handful of windows instead of the whole document.
        return self._fuzzy_locate(hay, needle, max_hits)

    def _fuzzy_locate(self, hay: str, needle: str, max_hits: int) -> list[Locus]:
        tokens = _WORD.findall(needle)
        if not tokens:
            return []

        doc_counts = Counter(_WORD.findall(hay))
        # Rare-but-present tokens make the best anchors.
        anchors = sorted(
            (t for t in set(tokens) if len(t) >= 4 and doc_counts.get(t, 0) > 0),
            key=lambda t: doc_counts[t],
        )[:3]
        if not anchors:
            return []

        # Window has to stay close to the needle length. An oversized window
        # makes difflib's length-based prefilters reject every candidate before
        # they are ever scored, which silently disables the whole fallback.
        window = int(len(needle) * 1.25) + 20
        candidates: set[int] = set()
        for anchor in anchors:
            for m in re.finditer(re.escape(anchor), hay):
                # The anchor can sit anywhere in the quote, so slide the window
                # back by roughly where it appears.
                offset = needle.find(anchor)
                candidates.add(max(0, m.start() - offset - 12))
                if len(candidates) > 400:
                    break

        scored: list[tuple[float, int, int]] = []
        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(needle)
        for start in sorted(candidates):
            chunk = hay[start : start + window]
            if not chunk:
                continue
            matcher.set_seq1(chunk)
            blocks = matcher.get_matching_blocks()
            covered = sum(b.size for b in blocks)
            # Score as the fraction of the QUOTE that was matched in order.
            # Normalizing by the window instead would penalise padding, which
            # is an artefact of how the window was cut, not of match quality.
            ratio = covered / len(needle)
            if ratio >= FUZZY_THRESHOLD:
                # Tighten the span onto the matched region.
                real = [b for b in blocks if b.size]
                if real:
                    s = start + real[0].a
                    e = start + real[-1].a + real[-1].size
                else:
                    s, e = start, start + window
                scored.append((ratio, s, e))

        scored.sort(reverse=True)
        out: list[Locus] = []
        used: list[tuple[int, int]] = []
        for ratio, s, e in scored:
            if any(s < ue and e > us for us, ue in used):
                continue  # overlapping duplicate
            used.append((s, e))
            out.append(
                Locus(
                    char_span=(s, e),
                    pages=self.pages_for(s, e),
                    sections=self.sections_for(s, e),
                    match="fuzzy",
                    score=ratio,
                )
            )
            if len(out) >= max_hits:
                break
        return out

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["figures"] = [asdict(f) for f in self.figures]
        return d

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolve_evidence(doc: DocumentModel, quotes: Iterable[str]) -> list[dict]:
    """Turn a list of model-supplied quotes into evidence entries.

    Unlocatable quotes are dropped and reported by the caller — never silently
    passed through with a guessed page number.
    """
    out: list[dict] = []
    for q in quotes:
        if not q or not q.strip():
            continue
        loci = doc.locate(q)
        if not loci:
            out.append({"quote": q, "resolved": False, "pages": [], "sections": []})
            continue
        best = loci[0]
        entry = {"quote": q, "resolved": True, **best.to_dict()}
        if len(loci) > 1:
            entry["also_at"] = [l.to_dict() for l in loci[1:]]
        out.append(entry)
    return out
