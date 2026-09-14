"""Tests for S9's prompt-driven digitizer.

digitize_figure() shells out to an agentic `claude` CLI call, so nothing here
makes a real model call (no network, no API key, no cost). Instead these
mock `subprocess.run` the same way test_backends.py does for CodexBackend,
and check the orchestration: the prompt contains what it should, the
subprocess is invoked correctly, and each failure mode of "what the CLI call
produced" (missing file, bad JSON, schema-invalid series) is handled without
raising anything but NeedsCalibration -- while a legitimate "not a plot"
answer (`{"series": []}`) is not treated as an error at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fitz = pytest.importorskip("fitz")

from mhtdb import backends as backends_module                     # noqa: E402
from mhtdb import digitize                                       # noqa: E402
from mhtdb.digitize import (                                     # noqa: E402
    NeedsCalibration, digitize_figure, select_boiling_curve_figure,
)


def _one_page_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((20, 20), "Figure 1. A test figure.")
    doc.save(path)
    doc.close()


VALID_SERIES = {
    "series": [
        {
            "series_id": "fig-1-bare",
            "figure_id": "fig-1",
            "label": "Bare copper",
            "x_axis": {"quantity": "dT_wall", "unit": "K", "scale": "linear"},
            "y_axis": {"quantity": "q_flux", "unit": "W/cm2", "scale": "linear"},
            "points": [[2.0, 1.5], [10.0, 24.0], [20.0, 82.0]],
            "uncertainty": {"method": "vector_path_extraction"},
            "notes": "isolated by red stroke color",
        }
    ]
}


def _fake_run_writing(payload: dict):
    def fake_run(cmd, cwd, **kwargs):
        Path(cwd, "output.json").write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    return fake_run


def test_prompt_includes_figure_identity_and_quantity_hints():
    text = digitize._prompt("fig-3", "huang-2023-pure-copper", "Fig 3. Boiling curve.",
                            calibration=None, panel=None)
    assert "fig-3" in text
    assert "huang-2023-pure-copper" in text
    assert "Boiling curve" in text
    assert "dT_wall" in text and "q_flux" in text
    assert "output.json" in text


def test_prompt_includes_human_supplied_axis_naming_hint():
    text = digitize._prompt("fig-6", "shi-2015", "caption",
                            calibration={"x_quantity": "dT_wall", "x_unit": "K",
                                         "y_quantity": "q_flux", "y_unit": "W/cm2"},
                            panel=None)
    assert "x_axis.quantity='dT_wall'" in text
    assert "y_axis.quantity='q_flux'" in text


def test_prompt_includes_frame_and_range_hints():
    text = digitize._prompt("fig-2", "rec", "caption",
                            calibration={"frame": [0.1, 0.05, 0.9, 0.8], "x_range": [0, 30]},
                            panel=None)
    assert "x0=0.1" in text and "y1=0.8" in text
    assert "runs from 0 to 30" in text


def test_prompt_includes_panel_targeting():
    text = digitize._prompt("fig-2", "rec", "caption", calibration=None, panel=2)
    assert "panel 2" in text


def test_known_vector_kind_skips_step_1_probing():
    text = digitize._prompt("fig-2", "rec", "caption", calibration=None, panel=None,
                            known_kind="vector")
    assert "vector data confirmed" in text
    assert "Skip straight to Step 2" in text
    assert "Check whether the plot area contains" not in text


def test_known_raster_kind_skips_step_1_probing():
    text = digitize._prompt("fig-2", "rec", "caption", calibration=None, panel=None,
                            known_kind="raster")
    assert "raster image confirmed" in text
    assert "Skip straight to Step 3" in text


def test_unknown_kind_keeps_full_probing_instructions():
    text = digitize._prompt("fig-2", "rec", "caption", calibration=None, panel=None)
    assert "Check whether the plot area contains" in text


def test_known_kind_resolution():
    assert digitize._known_kind(True, False) == "vector"
    assert digitize._known_kind(False, True) == "raster"
    assert digitize._known_kind(True, True) is None      # mixed -- still ambiguous
    assert digitize._known_kind(None, None) is None
    assert digitize._known_kind(False, False) is None


def test_resolve_calibration_picks_the_named_panel():
    calib = {"panels": {"1": {"x_quantity": "time"}, "2": {"x_quantity": "dT_wall"}}}
    assert digitize._resolve_calibration(calib, 2) == {"x_quantity": "dT_wall"}
    assert digitize._resolve_calibration(calib, 1) == {"x_quantity": "time"}
    # No panel selected this run and the calibration is panel-scoped: nothing
    # flat to fall back to.
    assert digitize._resolve_calibration(calib, None) == calib


def test_missing_claude_binary_raises_needs_calibration(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: None)
    with pytest.raises(NeedsCalibration, match="not on PATH"):
        digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1")


def test_missing_output_file_raises_needs_calibration(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(
        digitize.subprocess, "run",
        lambda cmd, cwd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(NeedsCalibration, match="did not write output.json"):
        digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1")


def test_invalid_json_output_raises_needs_calibration(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")

    def fake_run(cmd, cwd, **kwargs):
        Path(cwd, "output.json").write_text("not json", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(digitize.subprocess, "run", fake_run)
    with pytest.raises(NeedsCalibration, match="not valid JSON"):
        digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1")


def test_schema_invalid_series_raises_needs_calibration(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")
    bad = {"series": [{"series_id": "x", "figure_id": "fig-1"}]}  # missing axes/points
    monkeypatch.setattr(digitize.subprocess, "run", _fake_run_writing(bad))
    with pytest.raises(NeedsCalibration, match="schema validation"):
        digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1")


def test_valid_output_is_returned(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(digitize.subprocess, "run", _fake_run_writing(VALID_SERIES))
    got = digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1",
                          caption="Fig 1.", record_id="rec-1")
    assert got == VALID_SERIES["series"]


def test_not_a_plot_returns_empty_list_not_an_error(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(digitize.subprocess, "run", _fake_run_writing({"series": []}))
    assert digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1") == []


def test_timeout_raises_needs_calibration(monkeypatch, tmp_path):
    import subprocess as sp

    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")

    def fake_run(cmd, cwd, **kwargs):
        raise sp.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(digitize.subprocess, "run", fake_run)
    with pytest.raises(NeedsCalibration, match="timed out"):
        digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1", timeout=1)


def test_model_flag_is_passed_through(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")
    seen = {}

    def fake_run(cmd, cwd, **kwargs):
        seen["cmd"] = cmd
        Path(cwd, "output.json").write_text(json.dumps(VALID_SERIES), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(digitize.subprocess, "run", fake_run)
    digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1", model="claude-opus-5")
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "claude-opus-5"
    assert "--permission-mode" in seen["cmd"]


def test_known_vector_crop_gets_a_smaller_turn_and_effort_budget(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")
    seen = {}

    def fake_run(cmd, cwd, **kwargs):
        seen["cmd"] = cmd
        Path(cwd, "output.json").write_text(json.dumps(VALID_SERIES), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(digitize.subprocess, "run", fake_run)
    digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1",
                    has_vector=True, has_raster=False)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--max-turns") + 1] == str(digitize.VECTOR_MAX_TURNS)
    assert cmd[cmd.index("--effort") + 1] == digitize.VECTOR_EFFORT
    assert int(cmd[cmd.index("--max-turns") + 1]) < digitize.DEFAULT_MAX_TURNS


def test_raster_crop_keeps_the_full_budget(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")
    seen = {}

    def fake_run(cmd, cwd, **kwargs):
        seen["cmd"] = cmd
        Path(cwd, "output.json").write_text(json.dumps(VALID_SERIES), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(digitize.subprocess, "run", fake_run)
    digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1",
                    has_vector=False, has_raster=True)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--max-turns") + 1] == str(digitize.DEFAULT_MAX_TURNS)
    assert cmd[cmd.index("--effort") + 1] == digitize.DEFAULT_EFFORT


def test_explicit_max_turns_and_effort_override_the_vector_fast_path(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    _one_page_pdf(pdf)
    monkeypatch.setattr(digitize.shutil, "which", lambda name: "claude")
    seen = {}

    def fake_run(cmd, cwd, **kwargs):
        seen["cmd"] = cmd
        Path(cwd, "output.json").write_text(json.dumps(VALID_SERIES), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(digitize.subprocess, "run", fake_run)
    digitize_figure(pdf, 1, (0, 0, 400, 300), figure_id="fig-1",
                    has_vector=True, has_raster=False, max_turns=99, effort="low")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--max-turns") + 1] == "99"
    assert cmd[cmd.index("--effort") + 1] == "low"


# ------------------------------------------------------ select_boiling_curve_figure


def _crop(element_id, label="", caption=""):
    return SimpleNamespace(element_id=element_id, label=label, caption=caption)


class _FakeSelectionBackend:
    """Stands in for ClaudeCodeBackend in the cheap per-paper selection call."""

    figure_id = None
    panel = None
    reason = "test"
    seen_model = None
    seen_instruction = None
    raises = None

    def __init__(self, model=None):
        type(self).seen_model = model

    def complete(self, system_parts, instruction, schema, effort="high"):
        type(self).seen_instruction = instruction
        if type(self).raises:
            raise type(self).raises
        return SimpleNamespace(parsed=SimpleNamespace(figure_id=type(self).figure_id,
                                                       panel=type(self).panel,
                                                       reason=type(self).reason))


def test_select_returns_none_when_no_crops_have_captions():
    crops = [_crop("fig-1"), _crop("fig-2")]   # no label, no caption on any
    figure_id, panel, reason = select_boiling_curve_figure(crops)
    assert figure_id is None
    assert "no captioned" in reason


def test_select_returns_none_when_no_backend_available(monkeypatch):
    def raise_no_backend(model=None):
        raise RuntimeError("`claude` not found on PATH")

    monkeypatch.setattr(backends_module, "ClaudeCodeBackend", raise_no_backend)
    crops = [_crop("fig-1", caption="Boiling curve for bare copper.")]
    figure_id, panel, reason = select_boiling_curve_figure(crops)
    assert figure_id is None
    assert "unavailable" in reason


def test_select_returns_the_backends_pick(monkeypatch):
    _FakeSelectionBackend.figure_id = "fig-8"
    _FakeSelectionBackend.panel = None
    _FakeSelectionBackend.reason = "main comparison plot with a reference surface"
    _FakeSelectionBackend.raises = None
    monkeypatch.setattr(backends_module, "ClaudeCodeBackend", _FakeSelectionBackend)
    crops = [
        _crop("fig-1", caption="Schematic of experimental setup."),
        _crop("fig-8", caption="Pool boiling curve for different surfaces."),
    ]
    figure_id, panel, reason = select_boiling_curve_figure(crops)
    assert figure_id == "fig-8"
    assert panel is None
    assert "comparison" in reason
    # Both captions should have reached the instruction, not just the winner.
    assert "fig-1" in _FakeSelectionBackend.seen_instruction
    assert "fig-8" in _FakeSelectionBackend.seen_instruction


def test_select_returns_the_backends_panel_pick(monkeypatch):
    _FakeSelectionBackend.figure_id = "fig-7"
    _FakeSelectionBackend.panel = 1
    _FakeSelectionBackend.reason = "panel (a) is the boiling curve, (b) is HTC"
    _FakeSelectionBackend.raises = None
    monkeypatch.setattr(backends_module, "ClaudeCodeBackend", _FakeSelectionBackend)
    crops = [
        _crop("fig-7", caption="(a) heat flux vs wall superheat, (b) HTC vs heat flux."),
    ]
    figure_id, panel, reason = select_boiling_curve_figure(crops)
    assert figure_id == "fig-7"
    assert panel == 1


def test_select_returns_none_when_the_backend_finds_no_boiling_curve(monkeypatch):
    _FakeSelectionBackend.figure_id = None
    _FakeSelectionBackend.panel = None
    _FakeSelectionBackend.reason = "no figure describes a boiling curve"
    _FakeSelectionBackend.raises = None
    monkeypatch.setattr(backends_module, "ClaudeCodeBackend", _FakeSelectionBackend)
    crops = [_crop("fig-1", caption="SEM images of the coated surface.")]
    figure_id, panel, reason = select_boiling_curve_figure(crops)
    assert figure_id is None
    assert "no figure" in reason


def test_select_returns_none_when_the_call_fails(monkeypatch):
    _FakeSelectionBackend.raises = RuntimeError("boom")
    monkeypatch.setattr(backends_module, "ClaudeCodeBackend", _FakeSelectionBackend)
    crops = [_crop("fig-1", caption="Boiling curve.")]
    figure_id, panel, reason = select_boiling_curve_figure(crops)
    assert figure_id is None
    assert "failed" in reason
    _FakeSelectionBackend.raises = None


def test_select_passes_model_through(monkeypatch):
    _FakeSelectionBackend.figure_id = "fig-1"
    _FakeSelectionBackend.panel = None
    _FakeSelectionBackend.reason = "test"
    _FakeSelectionBackend.raises = None
    _FakeSelectionBackend.seen_model = None
    monkeypatch.setattr(backends_module, "ClaudeCodeBackend", _FakeSelectionBackend)
    crops = [_crop("fig-1", caption="Boiling curve.")]
    select_boiling_curve_figure(crops, model="claude-sonnet-5")
    assert _FakeSelectionBackend.seen_model == "claude-sonnet-5"
