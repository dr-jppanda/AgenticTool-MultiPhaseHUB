"""Pydantic models for the extraction stages and the catalog record.

These are the structured-output schemas handed to Claude. Three narrow schemas
(conditions / taxonomy / application) beat one wide one — smaller output shapes
extract markedly more reliably, and each can be re-run independently when its
facet changes.

Every extracted value carries `evidence`: verbatim quotes, nothing else. Pages
and sections are resolved afterwards by docmodel.locate(); the model is never
asked for them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------- shared


class Evidence(BaseModel):
    """A verbatim span from the paper supporting one extracted value."""

    quote: str = Field(
        description=(
            "Verbatim text copied EXACTLY from the paper, 15-300 characters, "
            "that states this value. Copy character for character — do not "
            "paraphrase, reformat, or fix typos. If no such span exists, omit "
            "the field entirely rather than inventing a quote."
        )
    )


class NumericRange(BaseModel):
    """One numeric field of the operating envelope, in the paper's own units."""

    min: float | None = Field(default=None, description="Lower end of the reported range.")
    max: float | None = Field(default=None, description="Upper end. Equal to min if a single value.")
    unit: str | None = Field(
        default=None,
        description=(
            "Unit EXACTLY as printed in the paper, e.g. 'kW/m2', 'W/cm^2', "
            "'bar', 'psia', 'kg/m2s', 'mm'. Do not convert."
        ),
    )
    evidence: list[Evidence] = Field(default_factory=list)


# ------------------------------------------------------------------ S1 triage


class Triage(BaseModel):
    paper_type: Literal[
        "experimental", "numerical_cfd", "correlation_only", "review_compilation", "other"
    ]
    contains_dataset: bool = Field(
        description="True only if the paper reports its own quantitative measurements or simulation results."
    )
    primary_phenomenon: Literal[
        "pool_boiling", "flow_boiling", "condensation",
        "evaporation", "adiabatic_two_phase", "none",
    ]
    title: str | None = None
    year: int | None = None
    reason: str = Field(description="One sentence justifying the classification.")


# -------------------------------------------------------------- S2 conditions


class Conditions(BaseModel):
    """Numeric operating envelope. Units stay as printed; SI conversion is code's job."""

    p_sat: NumericRange = Field(default_factory=NumericRange, description="System / saturation pressure.")
    T_sat: NumericRange = Field(default_factory=NumericRange, description="Saturation temperature.")
    G: NumericRange = Field(default_factory=NumericRange, description="Mass flux (mass velocity). Flow cases only.")
    q_flux: NumericRange = Field(default_factory=NumericRange, description="Applied heat flux.")
    x_quality: NumericRange = Field(default_factory=NumericRange, description="Vapor quality. Negative for subcooled.")
    dT_sub: NumericRange = Field(default_factory=NumericRange, description="Liquid subcooling.")
    dT_wall: NumericRange = Field(default_factory=NumericRange, description="Wall superheat / excess temperature.")
    D_h: NumericRange = Field(default_factory=NumericRange, description="Hydraulic or tube diameter; heater size for pool boiling.")
    L_heated: NumericRange = Field(default_factory=NumericRange, description="Heated length.")
    aspect_ratio: NumericRange = Field(default_factory=NumericRange)
    n_channels: NumericRange = Field(default_factory=NumericRange)
    Ra_surface: NumericRange = Field(default_factory=NumericRange, description="Arithmetic mean surface roughness.")
    contact_angle: NumericRange = Field(default_factory=NumericRange, description="Static/apparent contact angle in degrees.")
    gravity_level: NumericRange = Field(default_factory=NumericRange, description="In multiples of g. 1 unless stated otherwise.")
    n_data_points: NumericRange = Field(default_factory=NumericRange, description="Count of reported operating points.")

    orientation: Literal[
        "horizontal", "vertical_upflow", "vertical_downflow", "inclined", "unspecified"
    ] = "unspecified"
    heating_mode: Literal[
        "uniform_heat_flux", "constant_wall_temperature", "one_side_heated",
        "circumferentially_uniform", "unspecified",
    ] = "unspecified"
    surface_material: str | None = Field(default=None, description="e.g. copper, silicon, stainless steel 316.")


# ---------------------------------------------------------------- S3 taxonomy


class FacetPick(BaseModel):
    """One facet assignment, drawn from the controlled vocabulary."""

    tier1: str = Field(description="Tier-1 term. MUST come from the allowed list for this facet.")
    tier2: str | None = Field(default=None, description="Tier-2 term under the chosen tier1, if applicable.")
    tier3: str | None = Field(default=None, description="Tier-3 term, if the vocabulary defines one.")
    propose_new: str | None = Field(
        default=None,
        description=(
            "Set ONLY when no existing term fits. Give the term you would add "
            "and leave the tier fields at the closest existing parent. Never "
            "invent a value in tier1/tier2/tier3."
        ),
    )
    evidence: list[Evidence] = Field(default_factory=list)


class Taxonomy(BaseModel):
    phenomenon: FacetPick
    configuration: FacetPick
    method: FacetPick
    fluid: list[FacetPick] = Field(default_factory=list, description="One entry per working fluid studied.")
    surface_enhancement: list[FacetPick] = Field(default_factory=list, description="Use plain/plain if the surface is untreated.")
    measured_quantity: list[str] = Field(
        default_factory=list,
        description="Flat terms from the measured_quantity vocabulary. CHF is here, not in phenomenon.",
    )
    measurement_modality: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- S4 application


class ApplicationPick(BaseModel):
    tier1: str = Field(description="Tier-1 application term, or 'fundamental' if none is stated.")
    tier2: str | None = None
    stated: bool = Field(
        description=(
            "True only if the paper explicitly names this application. False if "
            "you inferred it. An inferred application still needs evidence."
        )
    )
    evidence: list[Evidence] = Field(default_factory=list)


class Application(BaseModel):
    targets: list[ApplicationPick] = Field(
        default_factory=list,
        description=(
            "Applications this dataset targets. If the paper states no "
            "application, return exactly one entry with tier1='fundamental'. "
            "Do not pad the list — an unstated application is not a finding."
        ),
    )


# ----------------------------------------------------------------- S6 verify


class FieldVerdict(BaseModel):
    field_path: str = Field(description="Dotted path, e.g. 'conditions.q_flux' or 'taxonomy.phenomenon'.")
    verdict: Literal["supported", "contradicted", "unsupported", "partially_supported"]
    note: str = Field(description="One sentence. Cite the contradicting text if verdict is 'contradicted'.")


class Verification(BaseModel):
    verdicts: list[FieldVerdict] = Field(default_factory=list)
    overall: Literal["clean", "minor_issues", "major_issues"]
