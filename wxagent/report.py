"""
Markdown rendering for the bulletins.

Layout follows Guide s27's component table: location, valid period, probability
of any rain, most likely character, heavy-spell probability, most likely
window, confidence, reasoning, main uncertainty - in that order, because that
order is what makes a forecast auditable after the fact.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

from . import config as C
from .diagnostics import DayDiagnosis, compass
from .doctrine import (
    Confidence, DISCLAIMER, TimingWindow, compose_forecast, impact_notes,
    main_uncertainty,
)
from .windy import LayerLink


BAR_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float], vmax: float | None = None) -> str:
    """Tiny inline bar chart for hourly rain - readable in a plain text file."""
    if not values:
        return ""
    top = vmax or max(values) or 1.0
    if top <= 0:
        return "▁" * len(values)
    out = []
    for v in values:
        frac = min(1.0, max(0.0, v / top))
        out.append(BAR_BLOCKS[min(len(BAR_BLOCKS) - 1,
                                  int(frac * (len(BAR_BLOCKS) - 1) + 0.5))])
    return "".join(out)


def h(level: int, text: str) -> str:
    return f"{'#' * level} {text}\n"


def fmt(value: float | None, spec: str = ".0f", dash: str = "--") -> str:
    return dash if value is None else format(value, spec)


# --------------------------------------------------------------------------
# Ingredient table - Guide Appendix B checklist, filled in
# --------------------------------------------------------------------------

def ingredients_table(dd: DayDiagnosis) -> str:
    m, l, s, o = dd.moisture, dd.lift, dd.stability, dd.organisation
    rows = [
        ("**Moisture**", "", ""),
        ("Dew point", f"{fmt(m.dew_point, '.1f')} °C",
         f"{_dew_feel(m.dew_point)}"),
        ("Temp − dew point spread", f"{fmt(m.spread, '.1f')} °C",
         "Near saturation - very little extra lift needed"
         if (m.spread is not None and m.spread <= C.NEAR_SATURATION_SPREAD)
         else "Some cooling still required to saturate"),
        ("RH 925 / 850 / 700 hPa",
         f"{fmt(m.rh_925)} / {fmt(m.rh_850)} / {fmt(m.rh_700)} %",
         f"Moisture depth: **{m.depth_class}**"),
        ("Precipitable water", f"{fmt(m.pwat, '.0f')} mm",
         _pwat_note(m.pwat)),
        ("Est. convective cloud base", f"{fmt(m.cloud_base_m)} m",
         "Guide s4.3 rule: 125 × (T − Td)"),

        ("**Lift**", "", ""),
        ("850 hPa wind",
         f"{compass(l.wind_850_dir)} {fmt(l.wind_850_speed, '.1f')} m/s",
         "The moisture-transport layer (Guide s10.1)"),
        ("925 hPa wind",
         f"{compass(l.wind_925_dir)} {fmt(l.wind_925_speed, '.1f')} m/s",
         "Very-low-level flow"),
        ("Terrain-normal component (850)",
         f"{fmt(l.orographic_850, '+.1f')} m/s",
         f"Orographic forcing: **{l.forcing_class}**"),

        ("**Instability**", "", ""),
        ("CAPE (peak / mean)",
         f"{fmt(s.cape_peak)} / {fmt(s.cape_mean)} J/kg",
         f"**{s.cape_class}**"),
        ("CIN (mean)", f"{fmt(s.cin_mean)} J/kg",
         "The cap that must break before deep convection begins"),
        ("Bulk shear 925→500", f"{fmt(s.shear_925_500, '.1f')} m/s",
         f"**{s.shear_class}** shear"),

        ("**Organisation**", "", ""),
        ("Wet hours / longest run", f"{o.wet_hours} h / {o.longest_run_h} h",
         f"**{o.character}**"),
        ("Peak hourly rate", f"{fmt(o.max_hourly, '.1f')} mm/h", o.note),
        ("Low / mid cloud", f"{fmt(o.cloud_low)} / {fmt(o.cloud_mid)} %", ""),
    ]

    lines = ["| Ingredient | Value | Reading |", "|---|---|---|"]
    for name, value, note in rows:
        lines.append(f"| {name} | {value} | {note} |")
    return "\n".join(lines) + "\n"


def _dew_feel(td: float | None) -> str:
    from .diagnostics import dewpoint_feel
    return dewpoint_feel(td)


def _pwat_note(pwat: float | None) -> str:
    if pwat is None:
        return ""
    if pwat >= C.PWAT_VERY_HIGH:
        return "Very high column moisture"
    if pwat >= C.PWAT_HIGH:
        return "High column moisture"
    return "Modest column moisture - Guide s4.4: high PWAT alone is not rainfall"


# --------------------------------------------------------------------------
# Model comparison table - Ch.21, presented WITHOUT a mean
# --------------------------------------------------------------------------

def model_table(dd: DayDiagnosis) -> str:
    rs = dd.rain
    lines = ["| Model | 24 h total | IMD band |", "|---|---|---|"]
    from .diagnostics import imd_category
    for label, mm in rs.per_model.items():
        lines.append(f"| {label} | {mm:.1f} mm | {imd_category(mm)[0]} |")
    lines.append(f"| **Range** | **{rs.lo:.1f} – {rs.hi:.1f} mm** | "
                 f"median {rs.median:.1f} mm |")
    out = "\n".join(lines) + "\n"
    out += ("\n> No arithmetic mean is given, deliberately. Guide Case Study F: "
            "averaging models that represent different physical scenarios "
            "hides the reason they disagree.\n")
    if rs.scenario_note:
        out += f"\n> **Spread warning.** {rs.scenario_note}\n"
    return out


# --------------------------------------------------------------------------
# Timing table
# --------------------------------------------------------------------------

def timing_table(windows: Sequence[TimingWindow]) -> str:
    lines = ["| Window | Rain | Peak rate | Risk |", "|---|---|---|---|"]
    for w in windows:
        lines.append(
            f"| {w.label} ({w.start_hour:02d}–{w.end_hour:02d}) | "
            f"{w.total_mm:.1f} mm | {w.peak_mm_h:.1f} mm/h | {w.risk} |"
        )
    return "\n".join(lines) + "\n"


def hourly_strip(pf, day: date, primary_model: str) -> str:
    from .diagnostics import window_indices
    ms = pf.models.get(primary_model) or next(iter(pf.models.values()))
    idx = window_indices(pf.times, day, 0, 24)
    vals = [ms.at("precipitation", i) or 0.0 for i in idx]
    if not vals:
        return ""
    spark = sparkline(vals)
    return (f"```\n00        06        12        18      23\n"
            f"{spark}\npeak {max(vals):.1f} mm/h   total {sum(vals):.1f} mm "
            f"({ms.label})\n```\n")


# --------------------------------------------------------------------------
# Windy links
# --------------------------------------------------------------------------

def windy_section(links: Sequence[LayerLink], title: str = "Check it on Windy"
                  ) -> str:
    out = h(2, title)
    out += ("Worked in the order of Handbook Ch.25's morning routine - pressure "
            "first, rain last.\n\n")
    for lk in links:
        out += f"- **[{lk.title}]({lk.url})**  \n  {lk.why}\n"
    return out + "\n"


def imd_section() -> str:
    out = h(2, "Official sources (these outrank this bulletin)")
    out += ("Handbook Ch.25: \"Start with the authoritative baseline, not "
            "Windy... nothing in this guide should ever lead you to contradict "
            "an official IMD warning, only to add context and local nuance "
            "around it.\"\n\n")
    for name, url in C.IMD_URLS.items():
        out += f"- [{name}]({url})\n"
    return out + "\n"


# --------------------------------------------------------------------------
# Full daily bulletin
# --------------------------------------------------------------------------

def render_daily(dd: DayDiagnosis, conf: Confidence, site, pf,
                 windows: Sequence[TimingWindow], links: Sequence[LayerLink],
                 synoptic: str, primary_model: str,
                 issued: datetime) -> str:
    out = h(1, f"Rain bulletin — {site.name}")
    out += (f"**Issued** {issued:%Y-%m-%d %H:%M IST}  \n"
            f"**Valid** {dd.day:%A %d %B %Y}, 00:00–24:00 IST  \n"
            f"**Lead time** {dd.lead_hours} h  \n"
            f"**Primary model** {pf.models[primary_model].label if primary_model in pf.models else '--'}"
            f" (compared against {', '.join(pf.model_labels())})\n\n")

    out += "---\n\n"
    out += h(2, "Forecast")
    out += compose_forecast(dd, conf, site.name, windows) + "\n\n"

    out += h(2, "Mechanism")
    out += (f"**{dd.regime}** — {dd.regime_note}\n\n{dd.mechanism}\n\n")
    out += ("> Guide s12 system-thinking rule: system → wind response → "
            "moisture transport → lifting zone → expected rain footprint.\n\n")

    out += h(2, "Synoptic setting")
    out += synoptic + "\n"

    out += h(2, "Ingredients")
    out += ("Guide's learning rule: never begin with the rain layer. Moisture, "
            "lift, instability and organisation come first.\n\n")
    out += ingredients_table(dd) + "\n"
    out += f"- **Moisture:** {dd.moisture.interpretation}\n"
    out += f"- **Lift:** {dd.lift.interpretation}\n"
    out += f"- **Instability:** {dd.stability.cape_meaning} {dd.stability.storm_mode}\n\n"

    out += h(2, "Model comparison")
    out += model_table(dd) + "\n"

    out += h(2, "Timing")
    out += timing_table(windows) + "\n"
    out += hourly_strip(pf, dd.day, primary_model) + "\n"

    notes = impact_notes(dd, site.name)
    if notes:
        out += h(2, "What this means for decisions")
        for n in notes:
            out += f"- {n}\n"
        out += "\n"

    out += h(2, "Main uncertainty")
    out += main_uncertainty(dd) + "\n\n"

    out += h(2, "Confidence")
    out += (f"| Aspect | Level |\n|---|---|\n"
            f"| Rain occurs | {conf.occurrence} |\n"
            f"| Amount | {conf.amount} |\n"
            f"| Timing | {conf.timing} |\n\n")
    out += f"{conf.rationale}\n\n"

    out += windy_section(links)
    out += imd_section()
    out += "---\n\n" + DISCLAIMER + "\n"
    return out
