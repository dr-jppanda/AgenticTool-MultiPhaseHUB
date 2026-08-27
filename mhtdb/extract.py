"""S1-S4 and S6 — the LLM stages.

Design rules enforced here:

* The paper text is the stable prefix; the per-pass instruction is the varying
  suffix. Passes 2-4 read the cache pass 1 wrote, so a four-pass extraction
  costs roughly one paper's input tokens plus change. Both backends honour this.
* The model returns quotes, never page numbers. Provenance is resolved
  downstream by docmodel.locate().
* Facet values are constrained to the controlled vocabulary, injected into the
  prompt from taxonomy/v1/facets.yaml so prompt and vocabulary cannot drift.
* Every call is content-addressed and cached on disk, so re-running after a
  prompt edit only re-bills the passes whose prompt actually changed.

The model is reached through `backends.py`, which serves the Anthropic API,
Claude Code, or Codex CLI. Nothing in this module knows which.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from .backends import DEFAULT_MODEL, Backend, detect_backend
from .docmodel import DocumentModel
from .schemas import Application, Conditions, Taxonomy, Triage, Verification

MODEL = DEFAULT_MODEL
PROMPT_VERSION = "p1"

T = TypeVar("T", bound=BaseModel)

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _ROOT / "pipeline" / "cache"


def load_facets(version: str = "v1") -> dict:
    return yaml.safe_load((_ROOT / "taxonomy" / version / "facets.yaml").read_text(encoding="utf-8"))


def _vocab_block(facets: dict) -> str:
    """Render the controlled vocabulary for injection into the system prompt."""
    lines: list[str] = []
    for name, spec in facets["facets"].items():
        lines.append(f"\n## {name}  ({spec['kind']}, multi={spec.get('multi', False)})")
        if spec.get("description"):
            lines.append(spec["description"].strip())
        if spec["kind"] == "flat":
            lines.append("terms: " + ", ".join(spec["terms"]))
        else:
            for t1, t2 in spec["tiers"].items():
                if isinstance(t2, dict):
                    for k, v in t2.items():
                        leaves = f" -> [{', '.join(v)}]" if v else ""
                        lines.append(f"  {t1} / {k}{leaves}")
                elif t2:
                    lines.append(f"  {t1} -> [{', '.join(map(str, t2))}]")
                else:
                    lines.append(f"  {t1}")
    return "\n".join(lines)


SYSTEM_PREAMBLE = """You extract structured metadata from multiphase heat transfer papers \
for a searchable research catalog.

Two rules govern everything you do here.

1. EVIDENCE OR NOTHING. Every value you report must be supported by a verbatim \
quote copied character-for-character from the paper. A quote that does not \
appear literally in the text will be detected and the field discarded. If the \
paper does not state something, leave the field null. A null is correct and \
costs nothing; a plausible guess is a defect.

2. NEVER COMPUTE, NEVER CONVERT, NEVER INVENT VOCABULARY. Report numbers in the \
paper's own units exactly as printed — downstream code handles SI conversion and \
dimensionless groups. Choose facet values only from the controlled vocabulary \
below; when nothing fits, use the propose_new field rather than inventing a term.

The full text of the paper follows. It is the only source you may draw on.
"""


def _system_parts(doc: DocumentModel, facets: dict) -> list[str]:
    """Stable prefix, cached once and reused by every pass on this paper.

    Returned as parts so the API backend can make them separate cacheable
    blocks; the Claude Code backend joins them into one system-prompt file.
    """
    return [
        SYSTEM_PREAMBLE,
        "# CONTROLLED VOCABULARY\n" + _vocab_block(facets),
        f"# PAPER FULL TEXT\nsource: {Path(doc.source).name}\n\n{doc.text}",
    ]


def _cache_key(doc: DocumentModel, pass_name: str, instruction: str,
               schema: type[BaseModel], backend_name: str,
               model: str = MODEL) -> Path:
    h = hashlib.sha256(
        "|".join([
            doc.doc_id, pass_name, PROMPT_VERSION, model, backend_name, instruction,
            json.dumps(schema.model_json_schema(), sort_keys=True),
        ]).encode()
    ).hexdigest()[:20]
    return _CACHE / f"{doc.doc_id}_{pass_name}_{h}.json"


def _run_pass(
    doc: DocumentModel,
    facets: dict,
    pass_name: str,
    instruction: str,
    schema: type[T],
    effort: str = "high",
    use_cache: bool = True,
    backend: Backend | None = None,
) -> T:
    backend = backend or detect_backend()
    key = _cache_key(
        doc, pass_name, instruction, schema, backend.name,
        getattr(backend, "model", MODEL),
    )
    if use_cache and key.exists():
        print(f"  [{pass_name}] disk-cached")
        return schema.model_validate_json(key.read_text(encoding="utf-8"))

    result = backend.complete(_system_parts(doc, facets), instruction, schema, effort)

    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(result.parsed.model_dump_json(indent=2), encoding="utf-8")
    print(f"  [{pass_name}] {backend.name}: {result.usage.line()}")
    return result.parsed


# ------------------------------------------------------------------ the passes

S1 = """Classify this paper.

Decide whether it reports its own quantitative multiphase heat transfer data.
A review or compilation that only tabulates other groups' results is
`review_compilation` and must have contains_dataset=false — those become
pointers, not dataset records."""

S2 = """Extract the numeric operating envelope.

For each field, give the range actually covered by the reported measurements —
not the equipment's rated capability and not a single illustrative value from
one figure. Where the paper reports a single value, set min and max equal.

Keep the paper's units verbatim ('W/cm2', 'psia', 'kg/m2s', 'mm'). Do not
convert anything.

For pool boiling, use D_h for the characteristic heater dimension and leave G
null — there is no mass flux.

If a value appears only in a figure and never in the text or a table, leave it
null. A separate figure-digitization pipeline handles those."""

S3 = """Assign the taxonomy facets.

Use only terms from the controlled vocabulary. For each pick, give the deepest
tier the paper actually supports — do not guess a tier-3 leaf when the paper
only establishes tier 2.

Reminders that catch most errors here:
- CHF, HTC and pressure drop are measured_quantity values, never phenomenon.
  A critical-heat-flux study is pool_boiling or flow_boiling with 'chf' in
  measured_quantity.
- An untreated surface is surface_enhancement plain/plain, not an empty list.
- List every working fluid studied, not just the headline one."""

S4 = """Identify the application target(s).

This field is the one most often gotten wrong, in a specific direction: a model
asked to name an application will always name one. Resist that.

Return tier1='fundamental' — a single entry, nothing else — whenever the paper
does not actually point at an application. That is the correct answer for most
fundamental studies and it is not a failure to find something.

Only report a specific application when the paper's own text motivates it. Set
stated=true only when the paper names the application explicitly; if you
inferred it from the fluid, geometry, or scale, set stated=false, mark
confidence honestly, and quote the text that led you there."""

S6 = """Below is a record extracted from this paper by an earlier pass. Audit it.

For each populated field, decide whether the paper actually supports the value.
Report `contradicted` when the paper states something different — quote it.
Report `unsupported` when nothing in the paper establishes the value.

You are looking for errors, not re-doing the extraction. Do not propose new
values. Fields that are correct need only a one-line 'supported' verdict."""


def triage(doc, facets=None, **kw) -> Triage:
    return _run_pass(doc, facets or load_facets(), "s1_triage", S1, Triage, effort="medium", **kw)


def extract_conditions(doc, facets=None, **kw) -> Conditions:
    return _run_pass(doc, facets or load_facets(), "s2_conditions", S2, Conditions, **kw)


def extract_taxonomy(doc, facets=None, **kw) -> Taxonomy:
    return _run_pass(doc, facets or load_facets(), "s3_taxonomy", S3, Taxonomy, **kw)


def extract_application(doc, facets=None, **kw) -> Application:
    return _run_pass(doc, facets or load_facets(), "s4_application", S4, Application, **kw)


def verify(doc, record: dict, facets=None, **kw) -> Verification:
    instruction = S6 + "\n\n```json\n" + json.dumps(_strip_evidence(record), indent=2)[:20000] + "\n```"
    return _run_pass(doc, facets or load_facets(), "s6_verify", instruction, Verification, **kw)


def run_all(doc: DocumentModel, backend: Backend | None = None,
            facets: dict | None = None) -> dict:
    """S1-S4 against one backend instance, so all four passes share its cache."""
    backend = backend or detect_backend()
    facets = facets or load_facets()

    tri = triage(doc, facets, backend=backend)
    print(f"  S1 -> {tri.paper_type}, dataset={tri.contains_dataset}, {tri.primary_phenomenon}")

    body: dict[str, Any] = {
        "extractor": f"{getattr(backend, 'model', MODEL)}/{PROMPT_VERSION}@{backend.name}",
        "triage": tri.model_dump(),
        "conditions": extract_conditions(doc, facets, backend=backend).model_dump(),
        "taxonomy": extract_taxonomy(doc, facets, backend=backend).model_dump(),
        "application": extract_application(doc, facets, backend=backend).model_dump(),
    }
    if tri.title:
        body["llm_title"] = tri.title
    if not tri.contains_dataset:
        body["routed_to"] = "pointers"
    return body


def _strip_evidence(obj: Any) -> Any:
    """Drop evidence blocks before the verify pass so it re-reads the paper."""
    if isinstance(obj, dict):
        return {k: _strip_evidence(v) for k, v in obj.items() if k != "evidence"}
    if isinstance(obj, list):
        return [_strip_evidence(v) for v in obj]
    return obj
