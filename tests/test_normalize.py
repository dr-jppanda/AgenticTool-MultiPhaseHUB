"""Tests for S5's unit parsing (mhtdb.normalize.to_si).

A digitized series with points that don't parse to SI is silently dropped
from every downstream plot/CSV even though its axes were correctly read as
dT_wall/q_flux -- these guard the two real-corpus failure modes that caused
that (space-separated SI notation, and a PDF-extracted unit string with a
mangled superscript character) so a unit string this catalog has already
seen never regresses back to "not convertible to SI".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mhtdb.normalize import to_si                                # noqa: E402


def test_slash_notation_heat_flux():
    assert to_si(500.0, "W/cm2", "q_flux") == (5_000_000.0, "W/m2")
    assert to_si(500.0, "W/cm^2", "q_flux") == (5_000_000.0, "W/m2")
    assert to_si(500.0, "kW/m2", "q_flux") == (500_000.0, "W/m2")


def test_unicode_superscript_heat_flux():
    assert to_si(500.0, "W/cm²", "q_flux") == (5_000_000.0, "W/m2")
    assert to_si(500.0, "kW/m²", "q_flux") == (500_000.0, "W/m2")


def test_space_separated_si_exponent_notation():
    """"kW m-2" (no slash) is the same unit as "kW/m2" -- common in papers
    that follow the ISO/BIPM space-separated-unit convention rather than a
    fraction. hadzic-2022's digitized series used exactly this form."""
    assert to_si(500.0, "kW m-2", "q_flux") == (500_000.0, "W/m2")
    assert to_si(500.0, "W m-2", "q_flux") == (500.0, "W/m2")
    assert to_si(500.0, "W cm-2", "q_flux") == (5_000_000.0, "W/m2")


def test_caret_space_separated_si_exponent_notation():
    """"kW m^-2" -- the same space-separated convention, with an explicit
    caret before the exponent. moze-2022's digitized series used this form."""
    assert to_si(500.0, "kW m^-2", "q_flux") == (500_000.0, "W/m2")


def test_mangled_area_exponent_falls_back_to_squared():
    """A PDF font with no ToUnicode mapping for its superscript-2 glyph can
    make PyMuPDF (and, downstream, the digitizer that reads through it)
    return a replacement character instead of "2" -- duan-2020's digitized
    unit string was exactly "W/cm�". The only physically sensible
    heat-flux unit that reads as "W/cm<one stray character>" in this
    catalog's domain is W/cm2, so it's read as such rather than dropped."""
    assert to_si(500.0, "W/cm�", "q_flux") == (5_000_000.0, "W/m2")
    assert to_si(500.0, "kW/cm�", "q_flux") == (5_000_000_000.0, "W/m2")


def test_genuinely_unknown_unit_still_rejected():
    """The fallback is narrow -- it must not swallow real garbage."""
    assert to_si(500.0, "furlongs", "q_flux") == (None, None)
    assert to_si(500.0, "W/cm��", "q_flux") == (None, None)
