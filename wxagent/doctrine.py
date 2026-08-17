"""
Forecast composition doctrine.

Guide s27 specifies exactly what a professional rainfall forecast must
separate: occurrence, intensity, timing and confidence - plus an explicit
statement of the main uncertainty.

Two rules from the guides are enforced structurally here rather than left to
good intentions:

  1. Occurrence confidence and amount confidence are computed and reported
     SEPARATELY. Guide Appendix E, Q12: "Agreement mainly increases confidence
     in shared features. Exact totals can still remain uncertain."

  2. No single mechanically-averaged rainfall number is ever presented as "the
     forecast" (Guide s24, Case Study F).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from . import config as C
from .diagnostics import (
    DayDiagnosis, compass, imd_category, lead_time_guidance, season_for,
)


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

CONFIDENCE_ORDER = ("very low", "low", "moderate", "moderate-high", "high")


def _step_down(level: str, steps: int = 1) -> str:
    i = CONFIDENCE_ORDER.index(level)
    return CONFIDENCE_ORDER[max(0, i - steps)]


@dataclass
class Confidence:
    occurrence: str
    amount: str
    timing: str
    rationale: str


def assess_confidence(dd: DayDiagnosis) -> Confidence:
    """
    Handbook Ch.27 sets the honest ceiling by lead time:
      - 0-2 days: genuinely reliable on broad categories (85-95% in an active
        monsoon signal)
      - 3-5 days: reliable on the broad pattern, hedge the language
      - beyond 7-10 days: possibilities, not facts
    """
    ep = dd.ensemble
    rs = dd.rain

    # --- occurrence -------------------------------------------------------
    if ep is not None and ep.p_measurable is not None:
        p = ep.p_measurable
        if p >= 0.85 or p <= 0.10:
            occ = "high"
        elif p >= 0.70 or p <= 0.25:
            occ = "moderate-high"
        elif p >= 0.55 or p <= 0.40:
            occ = "moderate"
        else:
            occ = "low"
        basis = f"{ep.n_members}-member ensemble puts measurable rain at {p:.0%}"
    else:
        frac = rs.occurrence_fraction
        if frac in (0.0, 1.0):
            occ = "moderate-high"
        elif frac >= 0.66:
            occ = "moderate"
        else:
            occ = "low"
        basis = (f"{rs.agree_on_occurrence} of {rs.n_models} models exceed the "
                 f"{C.MEASURABLE_RAIN_MM:g} mm threshold (no ensemble available)")

    # --- amount -----------------------------------------------------------
    if rs.categories_agree and rs.hi > 0:
        amt = "moderate-high"
    elif rs.scenario_note:
        amt = "low"
    elif rs.hi <= 0.5:
        amt = "moderate-high"
    else:
        amt = "moderate"

    # --- timing -----------------------------------------------------------
    if dd.organisation.character in ("continuous / steady",
                                     "organised with embedded heavy bursts"):
        tim = "moderate-high"
    elif dd.organisation.character == "intermittent spells":
        tim = "moderate"
    else:
        tim = "low"

    # Convective regimes are inherently unplaceable at suburb level
    # (Handbook Ch.4 / Ch.16 honesty note).
    if "CONVECTIVE" in dd.regime:
        tim = _step_down(tim)
        amt = _step_down(amt)

    # --- lead-time ceiling ------------------------------------------------
    lead = dd.lead_hours
    if lead >= 120:
        occ, amt, tim = (_step_down(occ, 2), "very low", "very low")
    elif lead >= 72:
        occ, amt, tim = (_step_down(occ), _step_down(amt), _step_down(tim, 2))
    elif lead >= 48:
        amt, tim = (_step_down(amt), _step_down(tim))

    label, guidance = lead_time_guidance(lead)
    rationale = f"{basis}. Lead time {lead}h ({label}) - {guidance}"
    return Confidence(occ, amt, tim, rationale)


# --------------------------------------------------------------------------
# Timing windows
# --------------------------------------------------------------------------

@dataclass
class TimingWindow:
    label: str
    start_hour: int
    end_hour: int
    total_mm: float
    peak_mm_h: float
    risk: str


DAYPARTS = (
    ("Early morning", 0, 6),
    ("Morning", 6, 12),
    ("Afternoon", 12, 17),
    ("Evening", 17, 21),
    ("Night", 21, 24),
)


def daypart_breakdown(pf, day: date, primary_model: str) -> list[TimingWindow]:
    """
    The rain-window breakdown that makes a bulletin actionable - the shape of
    the paid construction bulletin in Guide s29.4.
    """
    from .diagnostics import window_indices

    ms = pf.models.get(primary_model) or next(iter(pf.models.values()))
    out: list[TimingWindow] = []
    for label, lo, hi in DAYPARTS:
        idx = window_indices(pf.times, day, lo, hi)
        if not idx:
            continue
        vals = [ms.at("precipitation", i) or 0.0 for i in idx]
        total, peak = sum(vals), max(vals)
        if total < 0.5:
            risk = "low"
        elif peak >= C.HEAVY_SPELL_MM_PER_H:
            risk = "high - heavy spell possible"
        elif total >= 5.0:
            risk = "moderate"
        else:
            risk = "low-moderate"
        out.append(TimingWindow(label, lo, hi, total, peak, risk))
    return out


def most_likely_window(windows: Sequence[TimingWindow]) -> TimingWindow | None:
    wet = [w for w in windows if w.total_mm >= 1.0]
    return max(wet, key=lambda w: w.total_mm) if wet else None


# --------------------------------------------------------------------------
# Uncertainty statement
# --------------------------------------------------------------------------

def main_uncertainty(dd: DayDiagnosis) -> str:
    """
    Guide s27 requires the forecast to name its own main uncertainty. This
    picks the dominant one from the diagnosis rather than emitting a generic
    disclaimer.
    """
    rs, org, lift, moist = dd.rain, dd.organisation, dd.lift, dd.moisture

    if rs.scenario_note:
        return (
            "Model disagreement on amount is the dominant uncertainty. "
            + rs.scenario_note
        )
    if "CONVECTIVE" in dd.regime:
        return (
            "Where convection actually initiates is the dominant uncertainty. "
            "Handbook Ch.4: unstable air does not guarantee a storm - it only "
            "means a storm is possible if something lifts the air to "
            "saturation, and the trigger may fire a few kilometres away. It is "
            "normal and correct for one part of Kalyan to get a downpour while "
            "another stays dry."
        )
    if org.character == "isolated showers":
        return (
            "Band placement is the dominant uncertainty. Guide Case Study B: a "
            "small offshore band that tracks 20 km north or south can miss the "
            "target entirely. Radar in the last 2-3 hours will resolve this far "
            "better than any model can now."
        )
    if moist.depth_class == "shallow":
        return (
            "Whether cloud can grow deep enough to rain properly is the "
            "dominant uncertainty. The low levels are moist but 700 hPa is dry, "
            "so dry-air entrainment may cap cloud growth (Guide s4.4)."
        )
    if lift.forcing_class in ("moderate", "weak"):
        return (
            "Whether the onshore flow holds its angle is the dominant "
            "uncertainty. A modest veer or backing of the 850 hPa wind changes "
            "the terrain-normal component substantially, and with it how much "
            "the Ghats enhance the rain (Guide s11.1)."
        )
    if dd.lead_hours >= 72:
        return (
            "Lead time itself is the dominant uncertainty. At this range the "
            "broad pattern is meaningful but exact timing and totals are not."
        )
    return (
        "Exact intensity distribution within the rain area is the main "
        "uncertainty - the mechanism and the occurrence are both well "
        "supported."
    )


# --------------------------------------------------------------------------
# Headline
# --------------------------------------------------------------------------

def headline(dd: DayDiagnosis, conf: Confidence, site_name: str) -> str:
    """Short line for a notification toast - must survive being read alone."""
    rs = dd.rain
    ep = dd.ensemble

    if ep is not None and ep.p_measurable is not None:
        prob = f"{ep.p_measurable:.0%}"
    else:
        prob = f"{rs.occurrence_fraction:.0%} of models"

    if rs.hi < C.MEASURABLE_RAIN_MM:
        return f"{site_name}: little or no rain expected. Rain chance {prob}."

    if rs.categories_agree:
        cat = rs.category_lo
    else:
        cat = f"{rs.category_lo} to {rs.category_hi}"

    band = f"{rs.lo:.0f}-{rs.hi:.0f} mm" if rs.hi > rs.lo else f"~{rs.hi:.0f} mm"
    return (f"{site_name}: {cat.lower()} likely ({band}), rain chance {prob}. "
            f"Confidence {conf.occurrence} on occurrence.")


# --------------------------------------------------------------------------
# The forecast paragraph
# --------------------------------------------------------------------------

def compose_forecast(dd: DayDiagnosis, conf: Confidence, site_name: str,
                     windows: Sequence[TimingWindow]) -> str:
    """
    Write the forecast the way Guide s27 and Handbook Ch.22 Step 7 do: commit
    clearly where the evidence supports commitment, hedge specifically and
    honestly where it does not.
    """
    rs, org = dd.rain, dd.organisation
    ep = dd.ensemble
    parts: list[str] = []

    season_label = C.SEASON_LABELS[season_for(dd.day)]
    parts.append(f"**{site_name}, {dd.day:%A %d %B}** - {season_label}, "
                 f"regime: {dd.regime.lower()}.")

    # Occurrence
    if ep is not None and ep.p_measurable is not None:
        parts.append(
            f"Probability of measurable rain (>= {C.MEASURABLE_RAIN_MM:g} mm "
            f"over the day) is **{ep.p_measurable:.0%}**, from a "
            f"{ep.n_members}-member ensemble."
        )
        if ep.p_heavy and ep.p_heavy >= 0.05:
            parts.append(
                f"Probability of reaching the IMD heavy-rain threshold "
                f"({C.HEAVY_DAY_MM:g} mm/24h) is **{ep.p_heavy:.0%}**; the "
                f"90th-percentile member gives {ep.p90_mm:.0f} mm."
            )
    else:
        parts.append(
            f"{rs.agree_on_occurrence} of {rs.n_models} models produce "
            f"measurable rain."
        )

    # Amount - always as a range, never as a mean
    if rs.hi >= C.MEASURABLE_RAIN_MM:
        model_bits = ", ".join(f"{k} {v:.0f} mm" for k, v in rs.per_model.items())
        parts.append(
            f"Model guidance spans **{rs.lo:.0f}-{rs.hi:.0f} mm** ({model_bits}); "
            f"median {rs.median:.0f} mm. That places the day in the "
            f"**{rs.category_lo}**"
            + ("" if rs.categories_agree else f" to **{rs.category_hi}**")
            + " band on the IMD scale. Treat the category as the forecast, not "
              "the millimetre figure."
        )

    # Character
    if org.character != "essentially dry":
        parts.append(f"Expected character: **{org.character}** - {org.note}")
        if org.wet_hours:
            parts.append(
                f"Around {org.wet_hours} hours with rain, longest unbroken run "
                f"about {org.longest_run_h} h, peak hourly rate "
                f"{org.max_hourly:.1f} mm/h."
            )

    # Timing
    win = most_likely_window(windows)
    if win is not None:
        parts.append(
            f"Most likely window: **{win.label.lower()} "
            f"({win.start_hour:02d}:00-{win.end_hour:02d}:00)**, about "
            f"{win.total_mm:.0f} mm, peak {win.peak_mm_h:.1f} mm/h."
        )

    # Confidence, stated separately for each thing
    parts.append(
        f"Confidence: **{conf.occurrence}** on whether it rains, "
        f"**{conf.amount}** on how much, **{conf.timing}** on exact timing."
    )
    return " ".join(parts)


# --------------------------------------------------------------------------
# Impact / decision layer
# --------------------------------------------------------------------------

def impact_notes(dd: DayDiagnosis, site_name: str) -> list[str]:
    """
    Guide s29: "People rarely pay for a weather map. They pay for a decision."
    """
    notes: list[str] = []
    rs, stab = dd.rain, dd.stability
    ep = dd.ensemble

    heavy_risk = (
        (ep.p_heavy if ep and ep.p_heavy is not None else 0.0) >= 0.15
        or rs.hi >= C.HEAVY_DAY_MM
    )

    if heavy_risk:
        notes.append(
            "**Waterlogging / disruption risk.** Totals reach the IMD heavy "
            "band in at least one scenario. Expect slowed traffic and "
            "waterlogging in poor-drainage areas."
        )
        notes.append(
            "**Check the tide.** Handbook Ch.15: Mumbai's gravity-fed "
            "stormwater outfalls are throttled when the sea outside them is "
            "high, so heavy rain landing near high tide is a materially more "
            "serious flooding scenario than the same total near low tide. This "
            "agent deliberately does not synthesise tide times - look them up: "
            f"{C.TIDE_TABLE_URL}"
        )

    if (stab.cape_peak or 0) >= 1500:
        notes.append(
            f"**Lightning and gusts.** Peak CAPE around "
            f"{stab.cape_peak:.0f} J/kg ({stab.cape_class.lower()}). "
            f"{stab.storm_mode}"
        )

    if dd.organisation.max_hourly and dd.organisation.max_hourly >= C.HEAVY_SPELL_MM_PER_H:
        notes.append(
            f"**Short-duration intensity.** Peak modelled rate "
            f"{dd.organisation.max_hourly:.0f} mm/h. Guide s8.1: a day's total "
            "can be dominated by a few intense bursts - plan around the burst, "
            "not the daily average."
        )

    month = dd.day.month
    if month in C.CYCLONE_WATCH_MONTHS:
        notes.append(
            "**Cyclone season note.** This month falls in an Arabian Sea "
            "cyclone window (Handbook Ch.17). For anything beyond casual "
            "tracking, IMD's official cyclone bulletins are authoritative - "
            "this agent is not."
        )
    return notes


DISCLAIMER = (
    "_Independent interpretation, not an official IMD product. For flooding, "
    "lightning, transport and emergency decisions, follow IMD nowcasts, "
    "warnings and local authority instructions (Guide s29.5)._"
)
