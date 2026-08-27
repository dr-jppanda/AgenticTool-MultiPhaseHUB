"""Rule-based bootstrap extractor — the offline stand-in for S1-S4.

Purpose: get a real, evidence-grounded catalog off the ground without API
credentials, and give Phase 0 hand-labeling a starting draft rather than a blank
form. It is deliberately conservative — it reports only what a regex can defend
with a located quote, and marks everything `extractor: "rules-v0"` so it is never
confused with model output.

This is NOT a substitute for mhtdb/extract.py. It cannot read a table, resolve
"the same conditions as above", or tell a rated capability from a measured
range. Expect low recall and treat every field as a draft.
"""

from __future__ import annotations

import re
from typing import Iterable

from .docmodel import DocumentModel

EXTRACTOR_ID = "rules-v0"

# ------------------------------------------------------------------ keywords

_PHENOMENON = [
    (("pool boiling", "pool-boiling"), ("pool_boiling", None, None)),
    (("flow boiling", "flow-boiling", "convective boiling"), ("flow_boiling", None, None)),
    (("dropwise condensation",), ("condensation", "dropwise", None)),
    (("filmwise condensation", "film condensation"), ("condensation", "filmwise", None)),
    (("condensation",), ("condensation", "filmwise", None)),
    (("spray cooling",), ("evaporation", "spray_cooling", None)),
    (("jet impingement",), ("evaporation", "jet_impingement", None)),
    (("thin film evaporation", "thin-film evaporation"), ("evaporation", "thin_film", None)),
]

_FLUIDS = [
    (("deionized water", "de-ionized water", "di water", "distilled water", "water"), ("water", "water")),
    (("fc-72", "fc72"), ("dielectric", "FC-72")),
    (("fc-77",), ("dielectric", "FC-77")),
    (("hfe-7100", "hfe7100"), ("dielectric", "HFE-7100")),
    (("novec",), ("dielectric", "Novec-649")),
    (("r134a", "r-134a"), ("refrigerant_hfc", "R134a")),
    (("r245fa",), ("refrigerant_hfc", "R245fa")),
    (("r1234ze",), ("refrigerant_hfo", "R1234ze(E)")),
    (("r1234yf",), ("refrigerant_hfo", "R1234yf")),
    (("liquid nitrogen", "ln2"), ("cryogen", "nitrogen")),
    (("liquid helium",), ("cryogen", "helium")),
    (("carbon dioxide", "co2"), ("natural", "CO2")),
    (("ammonia",), ("natural", "ammonia")),
    (("ethanol",), ("mixture", "binary_organic")),
]

_ENHANCEMENT = [
    (("sintered",), ("porous_coating", "sintered")),
    (("microporous", "micro-porous"), ("porous_coating", "microporous")),
    (("metal foam", "copper foam"), ("porous_coating", "metal_foam")),
    (("nanowire",), ("nanostructured", "nanowire")),
    (("carbon nanotube", "cnt"), ("nanostructured", "cnt")),
    (("graphene",), ("nanostructured", "graphene")),
    (("cuo nanostructure", "cuo nanostructures", "cuo"), ("nanostructured", "nanoparticle_deposit")),
    (("biphilic",), ("wettability_engineered", "biphilic")),
    (("superhydrophobic",), ("wettability_engineered", "superhydrophobic")),
    (("hydrophobic",), ("wettability_engineered", "hydrophobic")),
    (("hydrophilic",), ("wettability_engineered", "hydrophilic")),
    (("sandblast", "sand-blast", "sanded", "abraded"), ("roughened", "sandblasted")),
    (("etched",), ("roughened", "etched")),
    (("laser-textured", "laser textured"), ("roughened", "laser_textured")),
    (("microfin", "micro-fin"), ("structured", "microfins")),
    (("reentrant cavit", "re-entrant cavit"), ("structured", "reentrant_cavities")),
    (("pin fin", "pin-fin"), ("structured", "pin_fins")),
    (("smooth surface", "polished", "plain surface", "bare surface"), ("plain", "plain")),
]

_MEASURED = [
    (("critical heat flux", "chf"), "chf"),
    (("heat transfer coefficient", "htc"), "htc"),
    (("boiling curve",), "boiling_curve"),
    (("wall superheat", "superheat"), "wall_superheat"),
    (("contact angle",), "contact_angle"),
    (("nucleation site density", "active nucleation site"), "nucleation_site_density"),
    (("bubble departure", "departure diameter", "bubble frequency"), "bubble_dynamics"),
    (("pressure drop",), "pressure_drop"),
    (("void fraction",), "void_fraction"),
    (("flow regime",), "flow_regime"),
    (("onset of nucleate boiling",), "onb"),
    (("rewetting",), "rewetting_temperature"),
    (("dryout",), "dryout_quality"),
]

_MODALITY = [
    (("thermocouple",), "thermocouple"),
    (("high-speed camera", "high speed camera", "high-speed video", "high-speed imaging"), "high_speed_visualization"),
    (("infrared", "ir thermograph"), "ir_thermography"),
    (("rtd", "resistance temperature detector"), "rtd"),
    (("particle image velocimetry", "piv"), "piv"),
    (("interferometr",), "optical_interferometry"),
    (("x-ray",), "x_ray"),
]

_CONFIG = [
    (("microchannel",), ("channel", "microchannel_array")),
    (("minichannel",), ("channel", "minichannel")),
    (("annulus", "annular test section"), ("channel", "annulus")),
    (("rectangular channel",), ("channel", "rectangular")),
    (("circular tube", "round tube"), ("channel", "circular_tube")),
    (("plate heat exchanger",), ("channel", "plate_heat_exchanger")),
    (("horizontal wire", "platinum wire"), ("surface", "wire")),
    (("tube bundle",), ("surface", "tube_bundle")),
    (("cylinder",), ("surface", "cylinder")),
    (("heat pipe",), ("device", "heat_pipe")),
    (("thermosyphon",), ("device", "thermosyphon")),
    (("vapor chamber",), ("device", "vapor_chamber")),
    (("cold plate",), ("device", "cold_plate")),
    (("flat plate", "horizontal surface", "heater surface", "copper block"), ("surface", "flat_plate")),
]

_APPLICATION = [
    (("data center", "datacenter"), ("electronics_thermal", "datacenter")),
    (("immersion cooling",), ("electronics_thermal", "immersion_cooling")),
    (("power electronics", "igbt"), ("electronics_thermal", "power_electronics")),
    (("electronics cooling", "chip cooling", "thermal management of electronic",
      "microprocessor", "high heat flux electronic"), ("electronics_thermal", "chip_cold_plate")),
    (("nuclear reactor", "pressurized water reactor", "light water reactor",
      "loss-of-coolant", "reactor safety"), ("nuclear", None)),
    (("refrigeration", "heat pump", "air conditioning"), ("hvacr", None)),
    (("steam generator", "power plant", "condenser tube"), ("power_generation", None)),
    (("spacecraft", "microgravity"), ("aerospace", None)),
    (("desalination",), ("process", "desalination_med_msf")),
    (("battery thermal", "battery pack"), ("battery_thermal", None)),
]

# ------------------------------------------------------------------ numerics

_NUM = r"[-+]?\d{1,6}(?:[.,]\d+)?(?:\s*[×x]\s*10\s*[-−]?\d+)?"
_RANGE = rf"({_NUM})\s*(?:[-–—]|to|up to|and)\s*({_NUM})"

_UNIT_ALT = {
    "q_flux": r"(?:W/cm(?:\^?2|²)|kW/m(?:\^?2|²)|W/m(?:\^?2|²)|MW/m(?:\^?2|²))",
    "dT_wall": r"(?:K|°C|C|℃)",
    "T_sat": r"(?:K|°C|C|℃)",
    "dT_sub": r"(?:K|°C|C|℃)",
    "contact_angle": r"(?:°|deg(?:rees?)?)",
    "Ra_surface": r"(?:nm|µm|um|μm|mm)",
    "D_h": r"(?:nm|µm|um|μm|mm|cm|m|in)",
    "p_sat": r"(?:kPa|MPa|bar|atm|psia?|Pa)",
    "G": r"(?:kg/m(?:\^?2|²)\s*[-·/]?\s*s|kg/m2s)",
    "x_quality": r"",
}

_CUES = {
    "q_flux": r"(?:heat\s+flux|q\s*[\"″']{1,2})",
    "dT_wall": r"(?:wall\s+superheat|superheat|excess\s+temperature|\bΔ\s*T\s*sat\b)",
    "T_sat": r"(?:saturation\s+temperature|T\s*sat|bulk\s+temperature)",
    "dT_sub": r"(?:subcool(?:ing|ed)|ΔT\s*sub)",
    "contact_angle": r"(?:contact\s+angle)",
    "Ra_surface": r"(?:roughness|\bRa\b|\bRq\b|arithmetic\s+mean)",
    "D_h": r"(?:hydraulic\s+diameter|tube\s+diameter|inner\s+diameter|channel\s+width|heater\s+(?:size|diameter|area)|D\s*h\b)",
    "p_sat": r"(?:pressure|atmospheric)",
    "G": r"(?:mass\s+flux|mass\s+velocity|mass\s+flow\s+rate)",
}

_SENT_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")


def _sentences(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for chunk in _SENT_SPLIT.split(text):
        out.append((pos, chunk))
        pos += len(chunk) + 1
    return out


def _parse_num(s: str) -> float | None:
    s = s.strip().replace(",", "")
    m = re.match(rf"^([-+]?\d+(?:\.\d+)?)\s*[×x]\s*10\s*[-−]?(\d+)$", s)
    if m:
        exp = int(m.group(2))
        if "−" in s or "-10" in s.replace(" ", "")[len(m.group(1)):]:
            exp = -exp
        return float(m.group(1)) * (10.0**exp)
    try:
        return float(s)
    except ValueError:
        return None


def _find_numeric(doc: DocumentModel, field: str) -> dict:
    """Find a range for `field` by pairing a cue phrase with a unit nearby."""
    cue = _CUES.get(field)
    unit_pat = _UNIT_ALT.get(field, "")
    if not cue or not unit_pat:
        return {}

    best: dict = {}
    for _, sent in _sentences(doc.text):
        if not re.search(cue, sent, re.I):
            continue
        window = sent[:600]

        m = re.search(rf"{_RANGE}\s*({unit_pat})", window, re.I)
        if m:
            lo, hi, unit = _parse_num(m.group(1)), _parse_num(m.group(2)), m.group(3)
        else:
            m = re.search(rf"({_NUM})\s*({unit_pat})", window, re.I)
            if not m:
                continue
            v = _parse_num(m.group(1))
            lo = hi = v
            unit = m.group(2)
        if lo is None or hi is None:
            continue
        if lo > hi:
            lo, hi = hi, lo

        span = _tidy_quote(sent)
        cand = {"min": lo, "max": hi, "unit": unit.strip(), "evidence": [{"quote": span}]}
        # Prefer a genuine range over a single value; then prefer the wider one.
        score = (1 if hi > lo else 0, hi - lo)
        if not best or score > best.pop("_score", (0, 0.0)):
            cand["_score"] = score
            best = cand
        else:
            best["_score"] = score
    best.pop("_score", None)
    return best


def _tidy_quote(sentence: str, limit: int = 280) -> str:
    q = " ".join(sentence.split())
    return q[:limit].rstrip()


def _first_hit(doc: DocumentModel, needles: Iterable[str]) -> tuple[str, str] | None:
    low = doc.text.lower()
    for n in needles:
        i = low.find(n)
        if i == -1:
            continue
        start = max(0, low.rfind(".", 0, i) + 1)
        end = low.find(".", i)
        end = end + 1 if end != -1 else min(len(doc.text), i + 240)
        return n, _tidy_quote(doc.text[start:end])
    return None


def _scan(doc: DocumentModel, table, multi: bool):
    picks = []
    for needles, value in table:
        hit = _first_hit(doc, needles)
        if hit:
            picks.append((value, hit[1]))
            if not multi:
                break
    return picks


# ------------------------------------------------------------------- driver


def extract_rules(doc: DocumentModel) -> dict:
    """Produce a draft record body from a DocumentModel. No network calls."""
    text_low = doc.text.lower()

    # -- phenomenon
    phen_picks = _scan(doc, _PHENOMENON, multi=False)
    if phen_picks:
        (t1, t2, t3), quote = phen_picks[0]
        phenomenon = {"tier1": t1, "tier2": t2, "tier3": t3, "evidence": [{"quote": quote}]}
    else:
        phenomenon = {"tier1": "pool_boiling", "tier2": None, "tier3": None, "evidence": []}

    # -- configuration
    cfg = _scan(doc, _CONFIG, multi=False)
    configuration = (
        {"tier1": cfg[0][0][0], "tier2": cfg[0][0][1], "tier3": None, "evidence": [{"quote": cfg[0][1]}]}
        if cfg else {"tier1": "surface", "tier2": "flat_plate", "tier3": None, "evidence": []}
    )

    # -- fluids (dedupe, water last so a named refrigerant wins the primary slot)
    fluids, seen = [], set()
    for (t1, t2), quote in _scan(doc, _FLUIDS, multi=True):
        if t2 in seen:
            continue
        seen.add(t2)
        fluids.append({"tier1": t1, "tier2": t2, "tier3": None, "evidence": [{"quote": quote}]})
    fluids.sort(key=lambda f: f["tier2"] == "water")

    # -- surface enhancement
    enh, seen_e = [], set()
    for (t1, t2), quote in _scan(doc, _ENHANCEMENT, multi=True):
        if t2 in seen_e:
            continue
        seen_e.add(t2)
        enh.append({"tier1": t1, "tier2": t2, "tier3": None, "evidence": [{"quote": quote}]})
    if not enh:
        enh = [{"tier1": "plain", "tier2": "plain", "tier3": None, "evidence": []}]

    measured = [v for (n, v), _ in [((n, v), None) for n, v in _MEASURED] if any(x in text_low for x in n)]
    modality = [v for (n, v) in _MODALITY if any(x in text_low for x in n)]

    # -- method
    if any(k in text_low for k in ("we simulate", "numerical simulation", "volume of fluid", "lattice boltzmann")):
        method = {"tier1": "numerical_cfd", "tier2": None, "tier3": None, "evidence": []}
    else:
        method = {"tier1": "experimental", "tier2": "steady_state", "tier3": None, "evidence": []}

    # -- application: only report what the text actually names
    apps = []
    for (t1, t2), quote in _scan(doc, _APPLICATION, multi=True):
        if any(a["tier1"] == t1 for a in apps):
            continue
        apps.append({
            "tier1": t1, "tier2": t2, "stated": True, "confidence": "medium",
            "evidence": [{"quote": quote}],
        })
    if not apps:
        apps = [{"tier1": "fundamental", "tier2": "fundamental", "stated": False,
                 "confidence": "high", "evidence": []}]

    conditions = {}
    for field in ("q_flux", "dT_wall", "T_sat", "dT_sub", "contact_angle", "Ra_surface", "D_h", "p_sat", "G"):
        found = _find_numeric(doc, field)
        if found:
            conditions[field] = found

    conditions["orientation"] = (
        "horizontal" if "horizontal" in text_low
        else "vertical_upflow" if "upward flow" in text_low or "vertical upflow" in text_low
        else "unspecified"
    )
    conditions["heating_mode"] = (
        "uniform_heat_flux" if "uniform heat flux" in text_low
        else "constant_wall_temperature" if "constant wall temperature" in text_low
        else "unspecified"
    )
    for mat in ("copper", "silicon", "stainless steel", "aluminum", "aluminium", "nickel", "titanium"):
        if mat in text_low:
            conditions["surface_material"] = mat
            break

    return {
        "extractor": EXTRACTOR_ID,
        "triage": {
            "paper_type": method["tier1"] if method["tier1"] != "experimental" else "experimental",
            "contains_dataset": True,
            "primary_phenomenon": phenomenon["tier1"],
        },
        "conditions": conditions,
        "taxonomy": {
            "phenomenon": phenomenon,
            "configuration": configuration,
            "method": method,
            "fluid": fluids,
            "surface_enhancement": enh,
            "measured_quantity": measured,
            "measurement_modality": modality,
        },
        "application": {"targets": apps},
    }
