"""S5 — deterministic normalization. No LLM touches this stage.

Three jobs, in order:
  1. Convert every numeric field from the paper's units to SI.
  2. Compute dimensionless groups from SI values + CoolProp fluid properties.
  3. Apply binning rules to produce derived tags.

Because this is pure code, changing a threshold in taxonomy/v1/binning.yaml and
re-running re-tags the entire corpus for free. That is the whole reason the
extraction stage reports numbers instead of tags.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
G_EARTH = 9.80665

# --------------------------------------------------------------------- units
# Multiplicative factors to SI. Offsets handled separately for temperature.
_UNIT_FACTORS: dict[str, tuple[str, float]] = {
    # pressure -> Pa
    "pa": ("Pa", 1.0), "kpa": ("Pa", 1e3), "mpa": ("Pa", 1e6),
    "bar": ("Pa", 1e5), "mbar": ("Pa", 1e2),
    "psi": ("Pa", 6894.757), "psia": ("Pa", 6894.757), "psig": ("Pa", 6894.757),
    "atm": ("Pa", 101325.0), "torr": ("Pa", 133.322), "mmhg": ("Pa", 133.322),
    # heat flux -> W/m2
    "w/m2": ("W/m2", 1.0), "w/m^2": ("W/m2", 1.0),
    "kw/m2": ("W/m2", 1e3), "kw/m^2": ("W/m2", 1e3),
    "mw/m2": ("W/m2", 1e6), "mw/m^2": ("W/m2", 1e6),
    "w/cm2": ("W/m2", 1e4), "w/cm^2": ("W/m2", 1e4),
    "kw/cm2": ("W/m2", 1e7), "kw/cm^2": ("W/m2", 1e7),
    "btu/hr-ft2": ("W/m2", 3.15459), "btu/h/ft2": ("W/m2", 3.15459),
    # mass flux -> kg/m2/s
    "kg/m2s": ("kg/m2/s", 1.0), "kg/m2-s": ("kg/m2/s", 1.0),
    "kg/m2/s": ("kg/m2/s", 1.0), "kg/(m2s)": ("kg/m2/s", 1.0),
    "kg/m^2s": ("kg/m2/s", 1.0), "kg/m^2/s": ("kg/m2/s", 1.0),
    "g/cm2s": ("kg/m2/s", 10.0),
    "lb/ft2h": ("kg/m2/s", 1.35623e-3), "lbm/ft2-h": ("kg/m2/s", 1.35623e-3),
    # length -> m
    "m": ("m", 1.0), "cm": ("m", 1e-2), "mm": ("m", 1e-3),
    "um": ("m", 1e-6), "µm": ("m", 1e-6), "micron": ("m", 1e-6),
    "nm": ("m", 1e-9), "in": ("m", 0.0254), "inch": ("m", 0.0254), "ft": ("m", 0.3048),
    # dimensionless / misc
    "-": ("-", 1.0), "": ("-", 1.0), "%": ("-", 0.01),
    "deg": ("deg", 1.0), "degree": ("deg", 1.0), "degrees": ("deg", 1.0), "°": ("deg", 1.0),
    "g": ("g", 1.0), "count": ("count", 1.0),
    # temperature DIFFERENCES -> K (same magnitude in C and K)
    "k": ("K", 1.0),
}

_TEMP_ABS = {"c", "°c", "degc", "celsius"}
_TEMP_ABS_F = {"f", "°f", "degf", "fahrenheit"}

# Fields whose unit is an absolute temperature vs a temperature difference.
_ABSOLUTE_TEMP_FIELDS = {"T_sat"}


_SUPERSCRIPT_MAP = str.maketrans({
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
    "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+",
})


def _clean_unit(u: str | None) -> str:
    if u is None:
        return ""
    s = u.strip().lower()
    s = s.translate(_SUPERSCRIPT_MAP)
    s = s.replace(" ", "").replace("·", "").replace("−", "-")
    s = s.replace("º", "°")            # masculine ordinal, as OCR reads degrees
    s = re.sub(r"^\[|\]$", "", s)
    # Space-separated SI exponent notation ("kW m-2", "W cm-2", "kW m^-2")
    # collapses, once spaces are stripped above, to "kwm-2"/"kwm^-2" --
    # rewrite the implicit per-area division back to the slash form
    # _UNIT_FACTORS keys use (which drop the minus sign: "kw/m2", not
    # "kw/m-2").
    s = re.sub(r"^((?:k|m)?w)m\^?-?(\d+)$", r"\1/m\2", s)
    s = re.sub(r"^((?:k|m)?w)cm\^?-?(\d+)$", r"\1/cm\2", s)
    # A figure's printed area exponent ("²") sometimes survives PDF text
    # extraction as a single unrecognisable character -- a font glyph with
    # no ToUnicode mapping decodes to U+FFFD or similar. The only physically
    # sensible reading of "w/cm<junk>" or "w/m<junk>" in this catalog is an
    # area exponent, so one stray trailing character there is read as "2"
    # rather than losing the whole unit (and the point along with it).
    s = re.sub(r"^((?:k|m)?w/c?m)[^\w/-]$", r"\g<1>2", s)
    return s


def to_si(value: float | None, unit: str | None, field: str) -> tuple[float | None, str | None]:
    """Convert one value to SI. Returns (value_si, si_unit); (None, None) if unknown."""
    if value is None:
        return None, None
    u = _clean_unit(unit)

    if u in _TEMP_ABS:
        return (value + 273.15, "K") if field in _ABSOLUTE_TEMP_FIELDS else (value, "K")
    if u in _TEMP_ABS_F:
        return ((value - 32) * 5 / 9 + 273.15, "K") if field in _ABSOLUTE_TEMP_FIELDS else (value * 5 / 9, "K")

    hit = _UNIT_FACTORS.get(u)
    if hit is None:
        return None, None
    si_unit, factor = hit
    return value * factor, si_unit


# ------------------------------------------------------------ fluid properties

_COOLPROP_NAMES = {
    "water": "Water", "steam_water": "Water",
    "R134a": "R134a", "R32": "R32", "R125": "R125", "R410A": "R410A",
    "R404A": "R404A", "R407C": "R407C", "R245fa": "R245fa",
    "R1234yf": "R1234yf", "R1234ze(E)": "R1234ze(E)",
    "R22": "R22", "R123": "R123",
    "CO2": "CarbonDioxide", "ammonia": "Ammonia", "propane": "Propane",
    "isobutane": "IsoButane",
    "nitrogen": "Nitrogen", "helium": "Helium", "hydrogen": "Hydrogen",
    "oxygen": "Oxygen", "argon": "Argon", "methane_LNG": "Methane",
    "FC-72": None, "HFE-7100": None, "Novec-649": None,  # not in CoolProp core
}

# Fallback saturated properties at ~1 atm for fluids CoolProp lacks.
_FALLBACK_PROPS = {
    "FC-72": {"rho_l": 1621.0, "rho_v": 13.4, "sigma": 0.0085, "h_fg": 88000.0, "mu_l": 4.5e-4, "p_crit": 1.83e6},
    "FC-77": {"rho_l": 1602.0, "rho_v": 11.0, "sigma": 0.0080, "h_fg": 89000.0, "mu_l": 8.0e-4, "p_crit": 1.62e6},
    "HFE-7100": {"rho_l": 1418.0, "rho_v": 9.9, "sigma": 0.0102, "h_fg": 111600.0, "mu_l": 3.7e-4, "p_crit": 2.23e6},
    "Novec-649": {"rho_l": 1543.0, "rho_v": 13.1, "sigma": 0.0108, "h_fg": 88000.0, "mu_l": 4.0e-4, "p_crit": 1.87e6},
}


def fluid_properties(fluid: str | None, p_sat: float | None = None, T_sat: float | None = None) -> dict:
    """Saturated liquid/vapor properties in SI. Empty dict when unavailable."""
    if not fluid:
        return {}
    if fluid in _FALLBACK_PROPS:
        return dict(_FALLBACK_PROPS[fluid])

    name = _COOLPROP_NAMES.get(fluid)
    if not name:
        return {}
    try:
        from CoolProp.CoolProp import PropsSI
    except ImportError:
        return {}

    try:
        p_crit = PropsSI("Pcrit", name)
        if p_sat is None and T_sat is None:
            p_sat = 101325.0
        if p_sat is None:
            p_sat = PropsSI("P", "T", T_sat, "Q", 0, name)
        p_sat = min(max(p_sat, 1.0), p_crit * 0.999)
        return {
            "rho_l": PropsSI("D", "P", p_sat, "Q", 0, name),
            "rho_v": PropsSI("D", "P", p_sat, "Q", 1, name),
            "sigma": PropsSI("SURFACE_TENSION", "P", p_sat, "Q", 0, name),
            "h_fg": PropsSI("H", "P", p_sat, "Q", 1, name) - PropsSI("H", "P", p_sat, "Q", 0, name),
            "mu_l": PropsSI("V", "P", p_sat, "Q", 0, name),
            "p_crit": p_crit,
            "T_sat_calc": PropsSI("T", "P", p_sat, "Q", 0, name),
        }
    except Exception:
        return {}


# ------------------------------------------------------------ dimensionless


def dimensionless(si: dict, props: dict) -> dict:
    """Compute the groups declared in binning.yaml -> derived."""
    out: dict[str, float] = {}
    rho_l, rho_v = props.get("rho_l"), props.get("rho_v")
    sigma, h_fg, mu_l = props.get("sigma"), props.get("h_fg"), props.get("mu_l")

    def mid(field: str) -> float | None:
        lo, hi = si.get(f"{field}_min"), si.get(f"{field}_max")
        vals = [v for v in (lo, hi) if v is not None]
        return sum(vals) / len(vals) if vals else None

    D_h, G, q, p = mid("D_h"), mid("G"), mid("q_flux"), mid("p_sat")

    if sigma and rho_l and rho_v is not None and rho_l > rho_v:
        L_cap = math.sqrt(sigma / (G_EARTH * (rho_l - rho_v)))
        out["L_cap"] = L_cap
        if D_h and D_h > 0:
            out["Co"] = L_cap / D_h
            out["Bo"] = (G_EARTH * (rho_l - rho_v) * D_h**2) / sigma
    if G and D_h and rho_l and sigma:
        out["We_lo"] = (G**2 * D_h) / (rho_l * sigma)
    if G and D_h and rho_l:
        out["Fr_lo"] = G**2 / (rho_l**2 * G_EARTH * D_h)
    if G and D_h and mu_l:
        out["Re_lo"] = (G * D_h) / mu_l
    if q and G and h_fg and G > 0:
        out["Bl"] = q / (G * h_fg)
    if p and props.get("p_crit"):
        out["p_reduced"] = p / props["p_crit"]

    return {k: v for k, v in out.items() if v is not None and math.isfinite(v)}


# ----------------------------------------------------------------- binning


def load_binning(version: str = "v1") -> dict:
    return yaml.safe_load((_ROOT / "taxonomy" / version / "binning.yaml").read_text(encoding="utf-8"))


def apply_binning(si: dict, derived: dict, rules: dict | None = None) -> list[str]:
    rules = rules or load_binning()
    tags: list[str] = []
    for rule in rules["rules"]:
        field, use = rule["field"], rule.get("use", "mid")
        if field in derived:
            value = derived[field]
        else:
            lo, hi = si.get(f"{field}_min"), si.get(f"{field}_max")
            vals = [v for v in (lo, hi) if v is not None]
            if not vals:
                continue
            value = {"min": min(vals), "max": max(vals)}.get(use, sum(vals) / len(vals))
        for band in rule["bands"]:
            if "lt" in band and value < float(band["lt"]):
                tags.append(band["tag"]); break
            if "ge" in band and value >= float(band["ge"]):
                tags.append(band["tag"]); break
    return sorted(set(tags))


# ------------------------------------------------------------------- driver

_NUMERIC_FIELDS = [
    "p_sat", "T_sat", "G", "q_flux", "x_quality", "dT_sub", "dT_wall",
    "D_h", "L_heated", "aspect_ratio", "n_channels", "Ra_surface",
    "contact_angle", "gravity_level", "n_data_points",
]

# Physically plausible SI bounds. A value outside these is a unit error or a
# mis-attributed number (e.g. a nanotube coating thickness captured as D_h).
# Rejected values are quarantined with a warning rather than silently feeding
# the dimensionless groups and producing confident-looking nonsense.
_PLAUSIBLE: dict[str, tuple[float, float]] = {
    "p_sat": (1e2, 5e7),          # 1 mbar .. 500 bar
    "T_sat": (4.0, 1200.0),       # K, absolute -- LHe (4.2K) is the coldest cryogen in scope
    "G": (0.1, 1e4),              # kg/m2/s
    "q_flux": (1.0, 1e9),         # W/m2
    "x_quality": (-1.0, 1.2),
    "dT_sub": (0.0, 300.0),
    "dT_wall": (0.0, 600.0),
    "D_h": (1e-5, 1.0),           # 10 um .. 1 m
    "L_heated": (1e-4, 100.0),
    "aspect_ratio": (1e-3, 1e3),
    "n_channels": (1, 1e5),
    "Ra_surface": (1e-10, 1e-3),  # 0.1 nm .. 1 mm
    "contact_angle": (0.0, 180.0),
    "gravity_level": (0.0, 100.0),
    "n_data_points": (1, 1e7),
}


def normalize_record(record: dict, version: str = "v1") -> dict:
    """Populate record['si'], ['derived'], ['derived_tags'], ['unit_warnings']."""
    conditions = record.get("conditions", {})
    si: dict[str, float] = {}
    warnings: list[str] = []
    rejected: dict[str, list[dict]] = {}

    for field in _NUMERIC_FIELDS:
        spec = conditions.get(field) or {}
        unit = spec.get("unit")
        for end in ("min", "max"):
            raw = spec.get(end)
            if raw is None:
                continue
            val, si_unit = to_si(raw, unit, field)
            if val is None:
                warnings.append(f"{field}.{end}: unrecognized unit {unit!r}")
                continue
            lo, hi = _PLAUSIBLE.get(field, (float("-inf"), float("inf")))
            if not (lo <= val <= hi):
                warnings.append(
                    f"{field}.{end}: {raw} {unit} -> {val:.4g} {si_unit} is outside the "
                    f"plausible range [{lo:g}, {hi:g}]; value quarantined"
                )
                rejected.setdefault(field, []).append({"end": end, "raw": raw, "unit": unit, "si": val})
                continue
            si[f"{field}_{end}"] = val
            si[f"{field}_unit"] = si_unit

    fluids = [f.get("tier2") or f.get("tier1") for f in record.get("taxonomy", {}).get("fluid", [])]
    primary_fluid = fluids[0] if fluids else None
    p_mid = None
    if "p_sat_min" in si or "p_sat_max" in si:
        vals = [si.get("p_sat_min"), si.get("p_sat_max")]
        vals = [v for v in vals if v is not None]
        p_mid = sum(vals) / len(vals)

    props = fluid_properties(primary_fluid, p_sat=p_mid, T_sat=si.get("T_sat_min"))
    derived = dimensionless(si, props)
    tags = apply_binning(si, derived, load_binning(version))

    record["si"] = si
    record["fluid_properties"] = {k: v for k, v in props.items() if isinstance(v, (int, float))}
    record["derived"] = derived
    record["derived_tags"] = tags
    record["unit_warnings"] = warnings
    record["quarantined_values"] = rejected
    record["taxonomy_version"] = version
    return record
