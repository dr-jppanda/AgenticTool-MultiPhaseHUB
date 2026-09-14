"""The review queue.

Every value the model returns lands in the catalog unconditionally. This
module partitions each record into what is stated plainly and what a human
should glance at — an unstated application, a proposed new vocabulary term,
a pick with no located evidence, a numeric value the plausibility gate threw
out — and collects those into a review queue.

Two principles, borrowed from lumina (see docs/lumina-comparison.md):

  * **Review never blocks use.** A gated field is still in the record, still
    searchable, just marked unconfirmed. Nothing is withheld pending approval.

  * **Rejections are remembered.** A rejected suggestion is never proposed
    again. Without that, the same term returns on the next paper and the queue
    never empties.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parent.parent
REVIEW_DIR = _ROOT / "catalog" / "review"
QUEUE_PATH = REVIEW_DIR / "queue.json"
REJECTIONS_PATH = REVIEW_DIR / "rejections.json"
PROPOSALS_PATH = _ROOT / "taxonomy" / "proposals" / "pending.json"


@dataclass
class ReviewItem:
    record_id: str
    field: str                 # dotted path, e.g. "application.targets[0]"
    kind: str                  # unstated_application | new_term | weak_evidence | quarantined_value
    proposed: str              # the value awaiting a decision
    reason: str                # one line: why this is here
    evidence: list[dict]       # located quotes, so the reviewer can judge in place
    created: str = ""

    def key(self) -> str:
        """Identity for dedup and rejection memory — value, not record."""
        return f"{self.kind}::{self.proposed.strip().lower()}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key()
        return d


# --------------------------------------------------------------- persistence


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def load_rejections() -> dict[str, dict]:
    return _load(REJECTIONS_PATH, {})


def save_rejections(rej: dict[str, dict]) -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    REJECTIONS_PATH.write_text(json.dumps(rej, indent=2, ensure_ascii=False), encoding="utf-8")


def load_queue() -> list[dict]:
    return _load(QUEUE_PATH, [])


def save_queue(items: Iterable[dict]) -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(list(items), indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------- gating


def _evidence_ok(ev: list[dict] | None) -> bool:
    return bool(ev) and any(e.get("resolved") for e in ev)


def gate_record(record: dict, rejections: dict[str, dict] | None = None) -> list[ReviewItem]:
    """Mark the record's fields confirmed/unconfirmed; return items needing review.

    Mutates `record` in place, adding `confirmed: bool` and `gate_reason` to the
    facet picks it inspects, plus a `gating` summary block.
    """
    rejections = rejections if rejections is not None else load_rejections()
    rid = record.get("record_id", "?")
    items: list[ReviewItem] = []
    today = str(date.today())

    def flag(field, kind, proposed, reason, evidence) -> bool:
        """Record a review item unless this exact suggestion was rejected before."""
        item = ReviewItem(rid, field, kind, str(proposed), reason,
                          evidence or [], today)
        if item.key() in rejections:
            return False
        items.append(item)
        return True

    tax = record.get("taxonomy", {}) or {}

    # -- taxonomy facets ------------------------------------------------
    for facet in ("phenomenon", "configuration", "method", "fluid", "surface_enhancement"):
        node = tax.get(facet)
        if node is None:
            continue
        picks = node if isinstance(node, list) else [node]
        for i, pick in enumerate(picks):
            if not isinstance(pick, dict):
                continue
            path = f"taxonomy.{facet}" + (f"[{i}]" if isinstance(node, list) else "")
            label = "/".join(x for x in (pick.get("tier1"), pick.get("tier2")) if x)
            ev = pick.get("evidence") or []

            if pick.get("propose_new"):
                pick["confirmed"] = False
                pick["gate_reason"] = "new vocabulary term"
                flag(path, "new_term", pick["propose_new"],
                     f"proposes a term not in the {facet} vocabulary", ev)
            elif not _evidence_ok(ev):
                pick["confirmed"] = False
                pick["gate_reason"] = "no located evidence"
                flag(path, "weak_evidence", label or facet,
                     f"{facet} assigned with no quote that resolves to the paper", ev)
            else:
                pick["confirmed"] = True
                pick.pop("gate_reason", None)

    # -- application target: the known hallucination hotspot ------------
    for i, t in enumerate((record.get("application", {}) or {}).get("targets", []) or []):
        if not isinstance(t, dict):
            continue
        label = "/".join(x for x in (t.get("tier1"), t.get("tier2")) if x)
        ev = t.get("evidence") or []

        # `fundamental` is the honest default, never gated — gating it would
        # push reviewers toward inventing an application, the exact failure the
        # field is prone to.
        if t.get("tier1") == "fundamental":
            t["confirmed"] = True
            t.pop("gate_reason", None)
            continue

        if not t.get("stated"):
            t["confirmed"] = False
            t["gate_reason"] = "inferred, not stated by the paper"
            flag(f"application.targets[{i}]", "unstated_application", label,
                 "application inferred rather than stated by the paper", ev)
        else:
            t["confirmed"] = True
            t.pop("gate_reason", None)

    # -- numerics the plausibility gate threw out -----------------------
    for field, rejected in (record.get("quarantined_values") or {}).items():
        for r in rejected:
            flag(f"conditions.{field}", "quarantined_value",
                 f"{field} = {r.get('raw')} {r.get('unit')}",
                 "outside the physically plausible range; excluded from SI and tags", [])

    record["gating"] = {
        "reviewed_at": today,
        "pending": len(items),
        "unconfirmed_fields": sum(
            1 for p in _all_picks(record) if p.get("confirmed") is False
        ),
    }
    return items


def _all_picks(record: dict) -> list[dict]:
    out = []
    tax = record.get("taxonomy", {}) or {}
    for facet in ("phenomenon", "configuration", "method", "fluid", "surface_enhancement"):
        node = tax.get(facet)
        if isinstance(node, dict):
            out.append(node)
        elif isinstance(node, list):
            out += [p for p in node if isinstance(p, dict)]
    out += [t for t in (record.get("application", {}) or {}).get("targets", []) or []
            if isinstance(t, dict)]
    return out


# ------------------------------------------------------------- queue upkeep


def rebuild_queue(records: Iterable[dict]) -> list[dict]:
    """Re-gate every record and rewrite the queue, deduped by suggestion."""
    rejections = load_rejections()
    seen: dict[str, dict] = {}
    for rec in records:
        for item in gate_record(rec, rejections):
            d = item.to_dict()
            prev = seen.get(d["key"])
            if prev:
                # Same suggestion from several papers: keep one row, note the rest.
                prev.setdefault("also_in", []).append(rec.get("record_id"))
            else:
                seen[d["key"]] = d
    queue = sorted(seen.values(), key=lambda d: (d["kind"], d["record_id"], d["field"]))
    save_queue(queue)
    return queue


def reject(key: str, note: str = "") -> bool:
    """Remember a rejection so the suggestion is never raised again."""
    rej = load_rejections()
    if key in rej:
        return False
    rej[key] = {"rejected_on": str(date.today()), "note": note}
    save_rejections(rej)
    save_queue([q for q in load_queue() if q.get("key") != key])
    return True


def accept(key: str) -> dict | None:
    """Drop the item from the queue. The value is already in the record —
    accepting only means 'stop asking me', so nothing is written back."""
    queue = load_queue()
    hit = next((q for q in queue if q.get("key") == key), None)
    if hit is None:
        return None
    save_queue([q for q in queue if q.get("key") != key])
    return hit


def collect_proposals(records: Iterable[dict]) -> list[dict]:
    """Gather every `propose_new` term for taxonomy curation.

    Grouped by facet and term, with the records that asked for it — a term
    three papers wanted is a much stronger case than one paper's one-off.
    """
    by_term: dict[tuple[str, str], dict] = {}
    for rec in records:
        tax = rec.get("taxonomy", {}) or {}
        for facet in ("phenomenon", "configuration", "method", "fluid", "surface_enhancement"):
            node = tax.get(facet)
            picks = node if isinstance(node, list) else [node] if node else []
            for p in picks:
                if not isinstance(p, dict) or not p.get("propose_new"):
                    continue
                k = (facet, p["propose_new"].strip().lower())
                entry = by_term.setdefault(k, {
                    "facet": facet, "term": p["propose_new"].strip(),
                    "nearest_parent": p.get("tier1"), "records": [], "n": 0,
                })
                entry["records"].append(rec.get("record_id"))
                entry["n"] += 1
    out = sorted(by_term.values(), key=lambda e: -e["n"])
    PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
