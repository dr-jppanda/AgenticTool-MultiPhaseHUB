"""Evaluation harness — scores an extraction run against the gold set.

    python eval/run.py                     # score catalog/records against eval/gold
    python eval/run.py --tag rules-v0      # label the scorecard

SKELETON. It runs, but it means nothing until eval/gold/ has real hand-labeled
records in it. Build 30-40, stratified across tier-1 phenomena plus deliberate
edge cases: a review paper, a mixed-mode study, a microgravity study, an unusual
fluid, and a paper that states no application.

Four metric families, deliberately kept separate:

  hierarchical F1   Partial credit for right-parent/wrong-child. Flat accuracy
                    will mislead you about whether the tier structure works.
  numeric           Within-tolerance rate on range endpoints, reported apart
                    from null rate (missed) and unsupported rate (asserted
                    without valid evidence). Conflating these hides whether a
                    prompt change made the model bolder or better.
  evidence validity Does each quote actually occur in the source? Pure string
                    check, no model in the loop — catches fabrication outright.
  application       Scored on its own, because it is the known weak point.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "eval" / "gold"
PRED = ROOT / "catalog" / "records"
REPORTS = ROOT / "eval" / "reports"

NUMERIC_TOL = 0.05  # 5% relative on range endpoints


def hierarchical_f1(gold: list[str], pred: list[str]) -> tuple[float, float, float]:
    """F1 over ancestor-expanded label sets.

    'flow_boiling/saturated/annular' expands to three labels, so predicting
    'flow_boiling/saturated' scores 2/3 recall rather than zero.
    """
    def expand(paths):
        out = set()
        for p in paths:
            parts = [x.strip() for x in p.split("/") if x.strip()]
            for i in range(1, len(parts) + 1):
                out.add("/".join(parts[:i]))
        return out

    g, p = expand(gold), expand(pred)
    if not g and not p:
        return 1.0, 1.0, 1.0
    tp = len(g & p)
    prec = tp / len(p) if p else 0.0
    rec = tp / len(g) if g else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return prec, rec, f1


def _paths(record: dict, facet: str) -> list[str]:
    node = record.get("taxonomy", {}).get(facet)
    if node is None:
        return []
    picks = node if isinstance(node, list) else [node]
    out = []
    for p in picks:
        if isinstance(p, dict):
            out.append("/".join(x for x in (p.get("tier1"), p.get("tier2"), p.get("tier3")) if x))
    return out


def score_numeric(gold: dict, pred: dict) -> dict:
    """Within-tolerance / missed / hallucinated, counted separately."""
    gc, pc = gold.get("conditions", {}), pred.get("conditions", {})
    hit = miss = extra = wrong = 0
    for field, gv in gc.items():
        if not isinstance(gv, dict) or gv.get("min") is None:
            continue
        pv = pc.get(field)
        if not isinstance(pv, dict) or pv.get("min") is None:
            miss += 1
            continue
        ok = True
        for end in ("min", "max"):
            a, b = gv.get(end), pv.get(end)
            if a is None or b is None:
                ok = False
                break
            denom = max(abs(a), 1e-12)
            if abs(a - b) / denom > NUMERIC_TOL:
                ok = False
                break
        hit += ok
        wrong += not ok
    for field, pv in pc.items():
        if isinstance(pv, dict) and pv.get("min") is not None:
            gv = gc.get(field)
            if not isinstance(gv, dict) or gv.get("min") is None:
                extra += 1
    total = hit + miss + wrong
    return {
        "in_tolerance": hit, "out_of_tolerance": wrong, "missed": miss,
        "unsupported_extra": extra,
        "in_tolerance_rate": round(hit / total, 3) if total else None,
        "null_rate": round(miss / total, 3) if total else None,
    }


def score_evidence(pred: dict) -> dict:
    """Every quote must have resolved against the source document."""
    total = resolved = fuzzy = 0

    def walk(node):
        nonlocal total, resolved, fuzzy
        if isinstance(node, dict):
            if "quote" in node and "resolved" in node:
                total += 1
                if node.get("resolved"):
                    resolved += 1
                    fuzzy += node.get("match") == "fuzzy"
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(pred)
    return {
        "quotes": total,
        "resolved": resolved,
        "fabricated": total - resolved,
        "validity_rate": round(resolved / total, 3) if total else None,
        "fuzzy_share": round(fuzzy / resolved, 3) if resolved else None,
    }


def score_application(gold: dict, pred: dict) -> dict:
    g = {t["tier1"] for t in gold.get("application", {}).get("targets", [])}
    p = {t["tier1"] for t in pred.get("application", {}).get("targets", [])}
    tp = len(g & p)
    return {
        "precision": round(tp / len(p), 3) if p else None,
        "recall": round(tp / len(g), 3) if g else None,
        # Naming an application where the gold says 'fundamental' is the
        # specific failure this facet is prone to; count it on its own.
        "over_claimed": int(g == {"fundamental"} and p != {"fundamental"}),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="untagged", help="label for this scorecard")
    args = ap.parse_args()

    gold_files = sorted(GOLD.glob("*.json"))
    if not gold_files:
        print(f"No gold records in {GOLD}.")
        print("The harness is a skeleton until you hand-label ~30. See eval/GOLD.md.")
        return 1

    facets = ["phenomenon", "configuration", "fluid", "surface_enhancement", "method"]
    rows, agg = [], {f: [] for f in facets}
    for gf in gold_files:
        gold = json.loads(gf.read_text(encoding="utf-8"))
        pf = PRED / gf.name
        if not pf.exists():
            print(f"  ! no prediction for {gf.name}")
            continue
        pred = json.loads(pf.read_text(encoding="utf-8"))
        row = {"record": gf.stem, "facets": {}}
        for f in facets:
            _, _, f1 = hierarchical_f1(_paths(gold, f), _paths(pred, f))
            row["facets"][f] = round(f1, 3)
            agg[f].append(f1)
        row["numeric"] = score_numeric(gold, pred)
        row["evidence"] = score_evidence(pred)
        row["application"] = score_application(gold, pred)
        rows.append(row)

    card = {
        "tag": args.tag,
        "date": str(date.today()),
        "n_scored": len(rows),
        "hierarchical_f1": {f: round(sum(v) / len(v), 3) for f, v in agg.items() if v},
        "evidence_validity": round(
            sum(r["evidence"]["validity_rate"] or 0 for r in rows) / max(len(rows), 1), 3
        ),
        "application_over_claim_rate": round(
            sum(r["application"]["over_claimed"] for r in rows) / max(len(rows), 1), 3
        ),
        "rows": rows,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"{args.tag}_{date.today()}.json"
    out.write_text(json.dumps(card, indent=2), encoding="utf-8")

    print(f"scored {len(rows)} records  [{args.tag}]")
    for f, v in card["hierarchical_f1"].items():
        print(f"  hF1 {f:<22} {v}")
    print(f"  evidence validity      {card['evidence_validity']}")
    print(f"  application over-claim {card['application_over_claim_rate']}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
