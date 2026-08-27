"""Round-trip tests for S0b crops and S8 digitization.

The point of a digitizer test is not that it runs — it is that the numbers
come back. So these tests *draw* a figure from known data, push it through the
same code path a paper would take, and assert the recovered values match what
was plotted. Both engines are covered: the vector path against a natively
drawn PDF, and the raster path against the same figure flattened to a bitmap.

No network, no model, no API key. Skips cleanly if PyMuPDF or matplotlib are
missing rather than failing the suite for an unrelated reason.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fitz = pytest.importorskip("fitz")
plt = pytest.importorskip("matplotlib.pyplot")
import matplotlib

matplotlib.use("Agg")

from mhtdb.digitize import (           # noqa: E402
    NeedsCalibration, digitize_figure, _calibrate, _despan, _canonical_quantity,
)
from mhtdb.figure_crops import extract_crops                    # noqa: E402
from mhtdb.figure_points import validate_series                 # noqa: E402


# The curve the test figure is drawn from: a plausible boiling curve.
TRUE_DT = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
TRUE_Q = [1.5, 4.0, 8.5, 15.0, 24.0, 34.0, 46.0, 58.0, 70.0, 82.0]


def _draw_figure(path: Path, rasterize: bool = False, caption: str = "Figure 1. Test boiling curve."):
    """Draw the known curve as a one-page PDF, vector or flattened."""
    fig, ax = plt.subplots(figsize=(5.2, 4.0))
    ax.plot(TRUE_DT, TRUE_Q, "o-", color="#c0392b", label="Bare copper")
    ax.plot([d * 1.4 for d in TRUE_DT], TRUE_Q, "s--", color="#2471a3",
            label="Sintered copper")
    ax.set_xlim(0, 30)
    ax.set_ylim(0, 100)
    # matplotlib omits the Δ glyph from the PDF text layer, so the label is
    # spelled out here; the glyph forms are covered by the unit tests below.
    ax.set_xlabel("Wall superheat [K]")
    ax.set_ylabel("q\u2033 [W/cm2]")
    ax.legend(loc="upper left", fontsize=7)
    fig.text(0.1, 0.005, caption, fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))

    if rasterize:
        png = path.with_suffix(".png")
        fig.savefig(png, dpi=200)
        plt.close(fig)
        doc = fitz.open()
        pix = fitz.Pixmap(str(png))
        page = doc.new_page(width=pix.width * 0.72, height=pix.height * 0.72)
        page.insert_image(page.rect, filename=str(png))
        doc.save(str(path))
        doc.close()
    else:
        fig.savefig(path)
        plt.close(fig)
        _onto_page(path)
    return path


def _onto_page(path: Path):
    """Place the figure on a letter-size page, so cropping has something to do."""
    src = fitz.open(path)
    out = fitz.open()
    page = out.new_page(width=612, height=792)
    box = fitz.Rect(72, 90, 540, 450)
    page.show_pdf_page(box, src, 0)
    src.close()
    out.save(str(path.with_suffix(".paged.pdf")))
    out.close()
    path.with_suffix(".paged.pdf").replace(path)


def _closest(series_points, x):
    return min(series_points, key=lambda p: abs(p[0] - x))


# --------------------------------------------------------------------- vector


def test_vector_roundtrip_recovers_plotted_values(tmp_path):
    pdf = _draw_figure(tmp_path / "vector.pdf")
    page = fitz.open(pdf)[0]
    series = digitize_figure(pdf, 1, tuple(page.rect), figure_id="fig-1")

    assert series, "no series recovered from a natively vector figure"
    for s in series:
        assert s["uncertainty"]["method"] == "vector_path_extraction"
        assert s["confidence"] >= 0.85
        assert not validate_series(s), f"schema violation: {validate_series(s)}"

    # The red curve is the one whose points sit at the plotted superheats.
    best = max(
        series,
        key=lambda s: sum(
            1 for dt in TRUE_DT if abs(_closest(s["points"], dt)[0] - dt) < 0.35
        ),
    )
    for dt, q in zip(TRUE_DT, TRUE_Q):
        got_x, got_y = _closest(best["points"], dt)
        assert abs(got_x - dt) < 0.35, f"x off at {dt}: got {got_x}"
        assert abs(got_y - q) < 1.0, f"y off at {dt}: got {got_y} want {q}"


def test_vector_axis_quantities_are_canonical(tmp_path):
    pdf = _draw_figure(tmp_path / "axes.pdf")
    page = fitz.open(pdf)[0]
    s = digitize_figure(pdf, 1, tuple(page.rect), figure_id="fig-1")[0]
    assert s["x_axis"]["quantity"] == "dT_wall"
    assert s["y_axis"]["quantity"] == "q_flux"


def test_line_hysteresis_loop_keeps_path_order(tmp_path):
    """A single stroke that folds back on itself (CHF hysteresis: heat flux
    rises with wall superheat, then the surface rewets at a lower superheat
    on the way back down) must come back as the loop it is, not as two
    branches braided together by re-sorting on x.
    """
    up_dt = [2.0, 5.0, 8.0, 11.0, 14.0, 17.0, 20.0]
    up_q = [2.0, 10.0, 24.0, 42.0, 64.0, 90.0, 120.0]
    down_dt = list(reversed(up_dt[:-1]))          # 17 -> 2: shares up's x-range
    down_q = [v * 0.55 for v in reversed(up_q[:-1])]
    dT, q = up_dt + down_dt, up_q + down_q

    fig, ax = plt.subplots(figsize=(5.2, 4.0))
    ax.plot(dT, q, "o-", color="#c0392b")
    ax.set_xlim(0, 25)
    ax.set_ylim(0, 130)
    ax.set_xlabel("Wall superheat [K]")
    ax.set_ylabel("q″ [W/cm2]")
    fig.text(0.1, 0.005, "Figure 1. Hysteresis boiling curve.", fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    pdf = tmp_path / "loop.pdf"
    fig.savefig(pdf)
    plt.close(fig)
    _onto_page(pdf)

    page = fitz.open(pdf)[0]
    series = digitize_figure(pdf, 1, tuple(page.rect), figure_id="fig-1")
    assert series, "no series recovered from a hysteresis loop"
    s = max(series, key=lambda s: len(s["points"]))
    pts = s["points"]
    assert len(pts) >= len(dT) - 2

    # The ascending and descending branches share almost the whole x-range,
    # so sorting by x interleaves two very different y values at nearly
    # every x -- a long, jagged path. The true loop order never needs a big
    # jump between consecutive points, so its total path length is much
    # shorter than the x-sorted braid's.
    def path_length(seq):
        return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(seq, seq[1:]))

    recovered_len = path_length(pts)
    x_sorted_len = path_length(sorted(pts, key=lambda p: p[0]))
    assert recovered_len < 0.9 * x_sorted_len, (
        f"recovered path ({recovered_len:.1f}) is not meaningfully shorter than "
        f"the x-sorted braid ({x_sorted_len:.1f}) -- points look re-sorted, not path-ordered"
    )


def test_two_series_stay_separate(tmp_path):
    pdf = _draw_figure(tmp_path / "two.pdf")
    page = fitz.open(pdf)[0]
    series = digitize_figure(pdf, 1, tuple(page.rect), figure_id="fig-1")
    assert len(series) >= 2, "distinct colours must not be merged into one curve"


# --------------------------------------------------------------------- raster


def test_raster_fallback_with_supplied_calibration(tmp_path):
    pdf = _draw_figure(tmp_path / "raster.pdf", rasterize=True)
    page = fitz.open(pdf)[0]
    series = digitize_figure(
        pdf, 1, tuple(page.rect), figure_id="fig-1", ocr=False,
        calibration={"x_range": [0, 30], "y_range": [0, 100],
                     "x_quantity": "dT_wall", "x_unit": "K",
                     "y_quantity": "q_flux", "y_unit": "W/cm2"},
    )
    assert series, "raster fallback recovered nothing"
    for s in series:
        assert s["uncertainty"]["method"] == "pixel_calibrated_digitization"
        assert s["confidence"] < 0.85, "traced pixels must not claim vector confidence"
        assert s["x_axis"]["quantity"] == "dT_wall"

    best = max(
        series,
        key=lambda s: sum(
            1 for dt in TRUE_DT if abs(_closest(s["points"], dt)[0] - dt) < 1.0
        ),
    )
    for dt, q in zip(TRUE_DT, TRUE_Q):
        got_x, got_y = _closest(best["points"], dt)
        assert abs(got_x - dt) < 1.0
        assert abs(got_y - q) < 4.0, f"raster y off at {dt}: {got_y} vs {q}"


def test_uncalibratable_figure_refuses_rather_than_guesses(tmp_path):
    """A figure with no readable tick values must raise, not invent an axis."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=240)
    page.draw_rect(fitz.Rect(40, 20, 280, 200), color=(0, 0, 0))
    for i in range(6):
        page.draw_circle(fitz.Point(60 + 30 * i, 180 - 22 * i), 3, color=(1, 0, 0),
                         fill=(1, 0, 0))
    pdf = tmp_path / "blank_axes.pdf"
    doc.save(str(pdf))
    doc.close()

    with pytest.raises(NeedsCalibration) as err:
        digitize_figure(pdf, 1, (0, 0, 300, 240), figure_id="fig-1", ocr=False)
    assert err.value.detail["reason"] in {"no_calibration", "no_frame"}


# ---------------------------------------------------------------- calibration


def test_calibration_survives_contaminated_ticks():
    """Consensus fitting must ignore junk labels mixed into a tick band."""
    good = [(100.0, 0.0), (150.0, 5.0), (200.0, 10.0), (250.0, 15.0), (300.0, 20.0)]
    junk = [(215.0, 1.0), (222.0, 1.0), (229.0, 1.0), (249.0, 1.0)]  # OCR'd arrowheads
    cal = _calibrate(good + junk)
    assert cal is not None
    assert cal.scale == "linear"
    assert abs(cal.to_data(100.0) - 0.0) < 0.2
    assert abs(cal.to_data(300.0) - 20.0) < 0.2


def test_log_axis_is_detected():
    ticks = [(200.0, 1.0), (160.0, 10.0), (120.0, 100.0), (80.0, 1000.0)]
    cal = _calibrate(ticks)
    assert cal is not None and cal.scale == "log"
    assert abs(cal.to_data(140.0) - 31.62) < 1.0


def test_symbol_font_glyphs_are_normalised():
    assert _despan("Tw [K]").startswith("Δ")
    assert _canonical_quantity("Tw") == "dT_wall"
    assert _canonical_quantity("qw") == "q_flux"
    # "W/cm 2" is a superscript typeset as its own span, not a stray digit.
    assert _despan("W/cm 2") == "W/cm2"


# --------------------------------------------------------------------- crops


def test_crops_are_tight_and_carry_their_caption(tmp_path):
    pdf = _draw_figure(tmp_path / "crop.pdf")
    crops = extract_crops(pdf, tmp_path / "out")
    figs = [c for c in crops if c.kind == "figure"]
    assert figs, "no figure cropped"

    c = figs[0]
    page_area = fitz.open(pdf)[0].rect.get_area()
    crop_area = (c.bbox[2] - c.bbox[0]) * (c.bbox[3] - c.bbox[1])
    assert crop_area < page_area, "a 'crop' the size of the page is not a crop"
    assert Path(c.pdf_path).exists() and Path(c.preview_path).exists()
    assert "boiling curve" in c.caption.lower()
    assert c.caption_confidence > 0


def test_crop_manifest_roundtrips(tmp_path):
    from mhtdb.figure_crops import write_manifest, load_manifest

    pdf = _draw_figure(tmp_path / "manifest.pdf")
    crops = extract_crops(pdf, tmp_path / "out")
    path = write_manifest("rec-1", crops, tmp_path / "crops.json")
    again = load_manifest(path)
    assert [c.element_id for c in again] == [c.element_id for c in crops]
    assert all(isinstance(c.bbox, tuple) for c in again)


# -------------------------------------------------------------------- curves


def test_point_rows_derive_si_columns(tmp_path):
    from mhtdb.curves import load_points, write_csv

    catalog = tmp_path / "catalog"
    (catalog / "points").mkdir(parents=True)
    (catalog / "points" / "rec-1.points.json").write_text(json.dumps({
        "record_id": "rec-1",
        "series": [{
            "series_id": "fig-1-bare", "figure_id": "fig-1", "label": "Bare copper",
            "x_axis": {"quantity": "dT_wall", "unit": "K"},
            "y_axis": {"quantity": "q_flux", "unit": "kW/m2"},
            "points": [[5.0, 100.0], [10.0, 400.0]],
            "confidence": 0.9, "uncertainty": {"method": "vector_path_extraction"},
        }],
    }), encoding="utf-8")

    rows = load_points(catalog_dir=catalog)
    assert len(rows) == 2
    assert rows[0].wall_superheat_K == 5.0
    assert rows[0].heat_flux_W_m2 == 100_000.0      # kW/m2 -> W/m2
    assert rows[0].source_type == "vector_digitized_figure"

    csv_path = write_csv(rows, tmp_path / "points.csv")
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("paper_id,figure_id,curve_id,x_value")


def test_plain_surface_selection_rejects_enhanced_curves(tmp_path):
    from mhtdb.curves import select_boiling_curves

    catalog = tmp_path / "catalog"
    (catalog / "points").mkdir(parents=True)
    (catalog / "points" / "rec-1.points.json").write_text(json.dumps({
        "record_id": "rec-1",
        "series": [
            {"series_id": "fig-1-bare", "figure_id": "fig-1", "label": "Bare copper",
             "x_axis": {"quantity": "dT_wall", "unit": "K"},
             "y_axis": {"quantity": "q_flux", "unit": "W/cm2"},
             "points": [[5, 10], [10, 30], [15, 60]], "confidence": 0.95,
             "uncertainty": {"method": "vector_path_extraction"},
             "conditions": {"figure_caption": "Fig. 1 DI water boiling curves"}},
            {"series_id": "fig-1-cnt", "figure_id": "fig-1", "label": "Sintered CNT",
             "x_axis": {"quantity": "dT_wall", "unit": "K"},
             "y_axis": {"quantity": "q_flux", "unit": "W/cm2"},
             "points": [[3, 15], [6, 45], [9, 90]], "confidence": 0.95,
             "uncertainty": {"method": "vector_path_extraction"},
             "conditions": {"figure_caption": "Fig. 1 DI water boiling curves"}},
        ],
    }), encoding="utf-8")

    chosen, rejected = select_boiling_curves(catalog_dir=catalog)
    assert [c.label for c in chosen] == ["Bare copper"]
    assert rejected and "enhanced" in rejected[0].reason


def test_wrong_fluid_is_excluded(tmp_path):
    from mhtdb.curves import select_boiling_curves

    catalog = tmp_path / "catalog"
    (catalog / "points").mkdir(parents=True)
    (catalog / "points" / "rec-1.points.json").write_text(json.dumps({
        "record_id": "rec-1",
        "series": [{
            "series_id": "fig-3-bare", "figure_id": "fig-3", "label": "Bare Copper",
            "x_axis": {"quantity": "dT_wall", "unit": "K"},
            "y_axis": {"quantity": "q_flux", "unit": "W/cm2"},
            "points": [[5, 10], [10, 30], [15, 60]], "confidence": 0.95,
            "uncertainty": {"method": "vector_path_extraction"},
            "conditions": {"figure_caption": "Figure 3. HFE-7300 pool boiling curves"},
        }],
    }), encoding="utf-8")

    chosen, rejected = select_boiling_curves(catalog_dir=catalog, fluid="water")
    assert not chosen
    assert "HFE" in rejected[0].reason


def test_rohsenow_uses_real_properties_and_scales_cubically():
    from mhtdb.curves import rohsenow

    q, props = rohsenow([5.0, 10.0], fluid="water", p_sat=101325.0)
    assert q[1] / q[0] == pytest.approx(8.0, rel=1e-6)     # q ~ dT^3
    assert 1e3 < q[0] < 1e6, f"implausible flux {q[0]:.3g} W/m2 at 5 K"
    assert props["c_sf"] == 0.013 and props["n"] == 1.0
