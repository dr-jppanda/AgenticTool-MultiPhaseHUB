"""Deterministic smoke tests. No API calls, no cost, runs in seconds.

    pytest -q

These cover the parts that must not silently break: the locator (which is both
the provenance resolver and the hallucination check), unit conversion and the
plausibility gate, binning, the figure-digitizer contract, and the dashboard
build. The LLM stages are not covered here — that is what eval/ is for, and it
needs a gold set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mhtdb.docmodel import DocumentModel, Page, Section, normalize_text
from mhtdb.figure_points import validate_series
from mhtdb.normalize import apply_binning, dimensionless, fluid_properties, to_si

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- normalization


def test_normalize_is_idempotent_and_length_stable():
    raw = "Effect of  surface\n\n roughness   on ﬂow\nboiling"
    once, omap = normalize_text(raw)
    twice, _ = normalize_text(once)
    assert once == twice, "normalizing normalized text must be a no-op"
    assert len(omap) == len(once), "offset map must cover every output char"
    assert "ﬂ" not in once and "flow" in once, "ligatures must expand"
    assert "\n" in once, "line structure must survive (title/heading detection needs it)"


def test_control_bytes_from_symbol_fonts_become_whitespace():
    # This corpus has PDFs encoding '×' as \x01 and '°' as \x03. A model cannot
    # reproduce those, so they must not survive into the text a quote is matched against.
    out, _ = normalize_text("10 mm \x01 10 mm \x03 3 mm")
    assert "\x01" not in out and "\x03" not in out
    assert out == "10 mm 10 mm 3 mm"


# -------------------------------------------------------------------- locator


def _doc(text: str) -> DocumentModel:
    norm, _ = normalize_text(text)
    half = len(norm) // 2
    return DocumentModel(
        doc_id="t", source="t.pdf", text=norm,
        pages=[Page(1, 0, half), Page(2, half, len(norm))],
        sections=[
            Section("sec-1", "1", "Introduction", 1, 0, half),
            Section("sec-2", "2", "Experimental setup", 1, half, len(norm)),
        ],
    )


SAMPLE = (
    "Pool boiling experiments were performed on sintered copper surfaces. "
    "The working fluid was deionized water at atmospheric pressure. "
    "Copper blocks of size 10 mm \x01 10 mm \x01 3 mm were prepared as test surfaces. "
    "Heat flux ranged from 5 to 42 W/cm2 and the measured contact angle was 64.5\x03."
)


def test_exact_quote_resolves_to_a_page_and_section():
    d = _doc(SAMPLE)
    loci = d.locate("The working fluid was deionized water at atmospheric pressure")
    assert loci, "an exact quote must resolve"
    assert loci[0].match == "exact"
    assert loci[0].pages and loci[0].sections


def test_quote_survives_control_byte_and_whitespace_noise():
    # What the model actually emits: it cannot type \x01, so it writes spaces.
    d = _doc(SAMPLE)
    assert d.locate("Copper blocks of size 10 mm  10 mm  3 mm were prepared"), (
        "control-byte glyphs must not break matching"
    )


def test_fuzzy_fallback_is_actually_reachable():
    # Regression: an oversized window once made difflib's length prefilter
    # reject every candidate, silently disabling the fuzzy path entirely.
    d = _doc(SAMPLE)
    loci = d.locate("Heat flux ranged from 5 to 42 W/cm and the measured contact angle was 64.5")
    assert loci, "near-miss quotes must still resolve via the fuzzy path"


def test_fabricated_quote_is_rejected():
    d = _doc(SAMPLE)
    assert d.locate("The apparatus was cooled with liquid xenon at 4.2 K under vacuum") == []


def test_span_crossing_a_page_boundary_reports_both_pages():
    norm, _ = normalize_text(SAMPLE)
    half = len(norm) // 2
    d = _doc(SAMPLE)
    quote = norm[half - 40 : half + 40]
    loci = d.locate(quote)
    assert loci and len(loci[0].pages) == 2, "multi-page spans must report every page"


def test_short_quotes_are_not_attributed():
    d = _doc(SAMPLE)
    assert d.locate("water") == [], "fragments are too ambiguous to attribute"


# ------------------------------------------------------------------- units


@pytest.mark.parametrize(
    "value,unit,field,expected",
    [
        (42, "W/cm2", "q_flux", 420000.0),
        (1, "bar", "p_sat", 1e5),
        (14.7, "psia", "p_sat", 101367.9),
        (10, "mm", "D_h", 0.01),
        (100, "C", "T_sat", 373.15),   # absolute temperature gets the offset
        (10, "C", "dT_wall", 10.0),    # a temperature *difference* does not
    ],
)
def test_unit_conversion(value, unit, field, expected):
    got, _ = to_si(value, unit, field)
    assert got == pytest.approx(expected, rel=1e-3)


def test_unknown_unit_reports_rather_than_guesses():
    assert to_si(1.0, "furlongs/fortnight", "G") == (None, None)


def test_plausibility_gate_quarantines_impossible_values():
    from mhtdb.normalize import normalize_record

    rec = {
        "conditions": {
            # 20 nm was really extracted from a nanotube-coating sentence.
            "D_h": {"min": 20, "max": 20, "unit": "nm"},
            "q_flux": {"min": 10, "max": 40, "unit": "W/cm2"},
        },
        "taxonomy": {"fluid": [{"tier1": "water", "tier2": "water"}]},
    }
    out = normalize_record(rec)
    assert "D_h" in out["quarantined_values"], "sub-micron D_h must not pass"
    assert "D_h_min" not in out["si"], "quarantined values must not reach SI"
    assert out["si"]["q_flux_max"] == pytest.approx(4e5), "valid values still convert"
    assert out["unit_warnings"], "the rejection must be explained, not silent"


# ------------------------------------------------------- properties & binning


def test_coolprop_properties_and_dimensionless_groups():
    props = fluid_properties("water", p_sat=101325.0)
    if not props:
        pytest.skip("CoolProp not installed")
    assert 950 < props["rho_l"] < 1000
    assert 2.2e6 < props["h_fg"] < 2.3e6

    si = {"D_h_min": 0.01, "D_h_max": 0.01, "q_flux_min": 1e5, "q_flux_max": 1e5}
    d = dimensionless(si, props)
    assert d["Co"] == pytest.approx(0.25, rel=0.15), "confinement number for a 10 mm surface"
    assert d["Bo"] > 1, "a 10 mm surface is unconfined"


def test_binning_turns_numbers_into_tags():
    tags = apply_binning(
        {"D_h_min": 5e-4, "D_h_max": 5e-4, "q_flux_min": 2e6, "q_flux_max": 2e6},
        {"Co": 1.4},
    )
    assert "scale:microchannel" in tags
    assert "confinement:confined" in tags
    assert "heatflux:very_high" in tags


def test_binning_is_pure_and_reproducible():
    args = ({"contact_angle_min": 30, "contact_angle_max": 30}, {})
    assert apply_binning(*args) == apply_binning(*args)


# ------------------------------------------------ figure-digitizer contract


def test_valid_point_series_passes():
    assert validate_series({
        "series_id": "fig-5-sintered", "figure_id": "fig-5",
        "x_axis": {"quantity": "dT_wall", "unit": "K"},
        "y_axis": {"quantity": "q_flux", "unit": "W/cm2"},
        "points": [[4.1, 3.2], [7.8, 11.5]],
    }) == []


def test_malformed_point_series_is_rejected_with_reasons():
    problems = validate_series({"series_id": "x", "figure_id": "f",
                                "x_axis": {"quantity": "dT_wall"}, "points": []})
    assert len(problems) >= 3
    assert any("y_axis" in p for p in problems)
    assert any("points" in p for p in problems)


# ------------------------------------------------------- taxonomy & catalog


def test_taxonomy_files_parse_and_agree_with_code():
    import yaml
    from mhtdb.normalize import _NUMERIC_FIELDS, _PLAUSIBLE

    facets = yaml.safe_load((ROOT / "taxonomy/v1/facets.yaml").read_text(encoding="utf-8"))
    binning = yaml.safe_load((ROOT / "taxonomy/v1/binning.yaml").read_text(encoding="utf-8"))
    assert facets["facets"] and binning["rules"]

    declared = set(facets["numeric_fields"])
    coded = set(_NUMERIC_FIELDS)
    assert coded - declared == set(), f"code has fields the taxonomy does not declare: {coded - declared}"
    assert set(_PLAUSIBLE) >= coded, "every numeric field needs a plausibility bound"


def test_every_catalog_record_has_resolved_evidence():
    records = list((ROOT / "catalog/records").glob("*.json"))
    if not records:
        pytest.skip("no records yet — run the pipeline first")
    for p in records:
        r = json.loads(p.read_text(encoding="utf-8"))
        s = r["evidence_stats"]
        assert s["total"] > 0, f"{p.name} has no evidence at all"
        assert s["unresolved"] == 0, (
            f"{p.name} has {s['unresolved']} fabricated/unlocatable quote(s)"
        )


def test_dashboard_builds_and_inlines_its_data():
    import sys

    sys.path.insert(0, str(ROOT / "app"))
    import build as app_build

    if not list((ROOT / "catalog/records").glob("*.json")):
        pytest.skip("no records yet")
    out = app_build.build("standalone")
    html = out.read_text(encoding="utf-8")
    assert "__DATA__" not in html, "payload placeholder must be substituted"
    assert '"records"' in html and "<svg" in html
    assert "<h4>Sources</h4>" in html
    assert "every value traced to the paper" not in html
    assert 'id="paperViewer"' in html and "data-paper-page" in html
    assert 'id="sideResizer"' in html and "mht.sidebar.width" in html
    # "Coverage · boiling curve plane" widget was intentionally removed
    # (only ever had 2 datasets with both dT_wall and q_flux as text); the
    # "coverage-count" class lives on and is still used by the curves panel.
    assert "coverage-count" in html

    catalog = app_build.compile_catalog()
    for record in catalog["records"]:
        if record["pdf"]:
            assert (out.parent / "papers" / record["pdf"]).exists(), (
                f"dashboard build did not include {record['pdf']}"
            )


# ----------------------------------------------- PDF hyphenation at line ends


@pytest.mark.parametrize(
    "raw,should_join",
    [
        ("the formu-\nlation of", True),    # typesetter syllable break
        ("nucle-\nation site", True),
        ("experi-\nmental setup", True),
        ("a two-\nphase flow", False),      # real compound, common in this field
        ("sub-\ncooled liquid", False),
        ("well-\nknown result", False),
        ("high-\nflux surface", False),
        ("R-\n134a", False),                # identifier: digit on the right
        ("high-\nFlux", False),             # uppercase on the right
    ],
)
def test_line_break_hyphenation(raw, should_join):
    out, omap = normalize_text(raw)
    assert len(omap) == len(out), "offset map must stay aligned after dropping chars"
    assert ("-" not in out) is should_join, out


def test_hyphenated_quote_resolves_after_dehyphenation():
    # The model quotes the reassembled word; the PDF has it split across lines.
    d = _doc("Boiling incipience depends on the nucle-\nation site density of the surface.")
    assert d.locate("depends on the nucleation site density of the surface")


# ------------------------------------------------------------- review gating


def _rec(**kw):
    base = {"record_id": "t", "taxonomy": {}, "application": {"targets": []},
            "quarantined_values": {}}
    base.update(kw)
    return base


def test_stated_application_is_confirmed():
    from mhtdb.gating import gate_record

    r = _rec(application={"targets": [{
        "tier1": "electronics_thermal", "stated": True,
        "evidence": [{"quote": "q", "resolved": True}]}]})
    assert gate_record(r, {}) == []
    assert r["application"]["targets"][0]["confirmed"] is True


def test_inferred_application_is_gated():
    from mhtdb.gating import gate_record

    r = _rec(application={"targets": [{
        "tier1": "nuclear", "stated": False,
        "evidence": [{"quote": "q", "resolved": True}]}]})
    items = gate_record(r, {})
    assert len(items) == 1 and items[0].kind == "unstated_application"
    assert r["application"]["targets"][0]["confirmed"] is False


def test_fundamental_is_never_gated():
    # Gating it would push reviewers toward inventing an application — the
    # exact failure this facet is prone to.
    from mhtdb.gating import gate_record

    r = _rec(application={"targets": [
        {"tier1": "fundamental", "stated": False, "evidence": []}]})
    assert gate_record(r, {}) == []
    assert r["application"]["targets"][0]["confirmed"] is True


def test_new_vocabulary_term_is_gated():
    from mhtdb.gating import gate_record

    r = _rec(taxonomy={"fluid": [{"tier1": "dielectric", "propose_new": "HFE-7300",
                                  "evidence": [{"quote": "q", "resolved": True}]}]})
    items = gate_record(r, {})
    assert [i.kind for i in items] == ["new_term"]


def test_pick_without_located_evidence_is_gated():
    from mhtdb.gating import gate_record

    r = _rec(taxonomy={"phenomenon": {"tier1": "pool_boiling",
                                      "evidence": [{"quote": "q", "resolved": False}]}})
    items = gate_record(r, {})
    assert [i.kind for i in items] == ["weak_evidence"]
    assert r["taxonomy"]["phenomenon"]["confirmed"] is False


def test_rejected_suggestions_never_return():
    from mhtdb.gating import ReviewItem, gate_record

    mk = lambda: _rec(taxonomy={"fluid": [{"tier1": "mixture", "propose_new": "benzene",
                                           "evidence": []}]})
    key = ReviewItem("t", "f", "new_term", "benzene", "", []).key()
    assert gate_record(mk(), {}), "sanity: it is raised when not rejected"
    assert gate_record(mk(), {key: {"rejected_on": "2026-01-01"}}) == []


def test_gating_never_removes_a_value():
    """Review must not block use — gated fields stay in the record."""
    from mhtdb.gating import gate_record

    r = _rec(application={"targets": [{"tier1": "nuclear", "stated": False,
                                       "evidence": []}]})
    gate_record(r, {})
    assert r["application"]["targets"][0]["tier1"] == "nuclear"


def test_confirming_a_field_clears_any_stale_gate_reason():
    """A field gated on one pass and confirmed on the next shouldn't keep the
    old explanation around."""
    from mhtdb.gating import gate_record

    r = _rec(application={"targets": [{"tier1": "nuclear", "stated": False,
                                       "gate_reason": "stale", "evidence": []}]})
    gate_record(r, {})
    t = r["application"]["targets"][0]
    t["stated"] = True
    gate_record(r, {})
    assert t["confirmed"] is True
    assert "gate_reason" not in t
