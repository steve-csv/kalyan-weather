"""
Weekly Mumbai MMR outlook, built from wind-pattern analysis.

The organising idea is Guide s11.1: "'strong wind' is not enough; examine its
direction relative to the terrain." So the spine of this product is the
seven-day evolution of the *terrain-normal component* of the 850 hPa wind - the
part of the monsoon current actually being forced up the Ghat face - rather
than wind speed or a rainfall total.

Handbook Ch.13's active/break cycle is what the week is classified against:
"the same nominal season can deliver a soaking active spell or a near-rainless
break within the same fortnight, and telling the two apart, a few days ahead,
using the trough's position, is one of the most valuable specific skills this
guide can give you."
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import html as _html
import re

from . import config as C
from . import (
    climate, oscillations, plain, report, synoptic, systems, thermal,
    upstream, web,
)
from .diagnostics import (
    compass, daily_rain_spread, diagnose_day, imd_category, lift_profile,
    moisture_profile, orographic_reading, season_for, stability_profile,
    window_indices,
)
from .doctrine import DISCLAIMER, assess_confidence
from .notify import notify
from .sources import fetch_ensemble, fetch_point, fetch_sites
from .windy import diagnostic_links

PRIMARY_MODEL = "ecmwf_ifs025"

# Sites carried in the weekly table. Ghat and leeward references are included
# because the contrast between them is what proves the regime.
WEEKLY_KEYS = (
    "colaba", "santacruz", "borivali", "chembur",
    "vasai_virar", "palghar", "thane", "bhiwandi",
    "kalyan_west", "dombivli", "panvel", "alibag",
    "badlapur", "karjat", "igatpuri", "matheran", "malshej",
    "lonavala", "pune",
)

# Sites whose heat/cold contrast is worth showing. Handbook Ch.19 makes this a
# gradient question, not a single number: coastal Mumbai is sea-breeze
# protected, Kalyan sits inland of that protection, Pune is on the plateau.
THERMAL_KEYS = ("santacruz", "kalyan_west", "karjat", "pune")

WIND_ARROWS = {
    "N": "↓", "NNE": "↓", "NE": "↙", "ENE": "↙",
    "E": "←", "ESE": "←", "SE": "↖", "SSE": "↖",
    "S": "↑", "SSW": "↑", "SW": "↗", "WSW": "↗",
    "W": "→", "WNW": "→", "NW": "↘", "NNW": "↘",
}


def arrow(direction: float | None) -> str:
    """Arrow showing where the air is going, not where it is from."""
    return WIND_ARROWS.get(compass(direction), "·")


# --------------------------------------------------------------------------
# Wind-pattern spine
# --------------------------------------------------------------------------

def wind_pattern_table(pf, days: list[date]) -> str:
    """
    Day-by-day 850 hPa wind and its terrain-normal component at the home point.
    This is the table the whole outlook hangs off.
    """
    lines = [
        "| Day | 850 hPa wind | Speed | Toward | Terrain-normal | Forcing |",
        "|---|---|---|---|---|---|",
    ]
    ms = pf.models.get(PRIMARY_MODEL) or next(iter(pf.models.values()))
    for d in days:
        idx = window_indices(pf.times, d, 0, 24)
        if not idx:
            continue
        lp = lift_profile(ms, idx)
        if lp.wind_850_dir is None:
            wind_cell = "--"
        else:
            wind_cell = f"{compass(lp.wind_850_dir)} ({lp.wind_850_dir:.0f}°)"
        lines.append(
            f"| {d:%a %d %b} "
            f"| {wind_cell} "
            f"| {lp.wind_850_speed or 0:.1f} m/s "
            f"| {arrow(lp.wind_850_dir)} "
            f"| {(lp.orographic_850 or 0):+.1f} m/s "
            f"| **{lp.forcing_class}** |"
        )
    out = "\n".join(lines) + "\n\n"
    out += (
        "> The terrain-normal column is the number that matters. It is the "
        "component of the 850 hPa wind aimed square at the Ghat face "
        f"(upslope normal taken as {C.GHAT_UPSLOPE_NORMAL_DEG:.0f}°). "
        "A fast wind blowing parallel to the ridge lifts almost nothing; a "
        "moderate wind blowing straight at it lifts a great deal "
        "(Guide s7.1, s11.1). Negative values mean descent and active rain "
        "suppression — the Foehn side of Handbook Ch.14.\n\n"
        f"> Reference points: {C.OROG_MODERATE:.1f} m/s ≈ the 15 kt that "
        f"Handbook Ch.22 Step 4 calls a loaded orographic engine; "
        f"{C.OROG_STRONG:.1f} m/s ≈ 20 kt.\n"
    )
    return out


def wind_profile_strip(pf, days: list[date]) -> str:
    """Visual strip of the terrain-normal component across the week."""
    ms = pf.models.get(PRIMARY_MODEL) or next(iter(pf.models.values()))
    values, labels = [], []
    for d in days:
        idx = window_indices(pf.times, d, 0, 24)
        if not idx:
            continue
        lp = lift_profile(ms, idx)
        values.append(max(0.0, lp.orographic_850 or 0.0))
        labels.append(f"{d:%a}"[:2])
    if not values:
        return ""
    strip = report.sparkline(values, vmax=max(C.OROG_VERY_STRONG, max(values)))

    # Fixed-width columns so the three rows line up under each other.
    w = 4
    row_lbl = "".join(s.ljust(w) for s in labels)
    row_bar = "".join(ch.ljust(w) for ch in strip)
    row_val = "".join(f"{v:.0f}".ljust(w) for v in values)

    thresholds = (f"weak<{C.OROG_WEAK:.0f}  "
                  f"moderate>={C.OROG_MODERATE:.0f}  "
                  f"strong>={C.OROG_STRONG:.0f}  "
                  f"very strong>={C.OROG_VERY_STRONG:.0f}")
    return (f"```\nOrographic forcing - 850 hPa terrain-normal component (m/s)\n"
            f"{row_lbl}\n{row_bar}\n{row_val}\n\n{thresholds}\n```\n")


def week_regime(pf, days: list[date]) -> tuple[str, str]:
    """Classify the week as a whole - active spell, break, or mixed."""
    ms = pf.models.get(PRIMARY_MODEL) or next(iter(pf.models.values()))
    comps = []
    for d in days:
        idx = window_indices(pf.times, d, 0, 24)
        if idx:
            lp = lift_profile(ms, idx)
            if lp.orographic_850 is not None:
                comps.append(lp.orographic_850)
    if not comps:
        return "UNKNOWN", "Insufficient wind data to classify the week."

    strong_days = sum(1 for c in comps if c >= C.OROG_STRONG)
    weak_days = sum(1 for c in comps if c < C.OROG_WEAK)
    mean = sum(comps) / len(comps)

    if strong_days >= 4:
        return ("ACTIVE SPELL", (
            f"{strong_days} of {len(comps)} days carry a strong terrain-normal "
            f"component (mean {mean:+.1f} m/s). Handbook Ch.13: this is the "
            "active configuration — rain widespread and often heavy across the "
            "west coast, with the Ghats heavily enhanced."))
    if weak_days >= 4:
        return ("BREAK / SUBDUED", (
            f"{weak_days} of {len(comps)} days have negligible onshore forcing "
            f"(mean {mean:+.1f} m/s). Handbook Ch.13: the break pattern — "
            "longer dry intervals, shallower cloud, weaker rain bands. Local "
            "convection can still fire, but the monsoon engine is idling."))
    if strong_days >= 2:
        return ("PULSING", (
            f"The onshore component swings between strong and weak across the "
            f"week (mean {mean:+.1f} m/s). Expect the monsoon's own rhythm — "
            "wet spells separated by quieter days rather than a uniform week."))
    return ("MODERATE / MIXED", (
        f"Mean terrain-normal component {mean:+.1f} m/s, without a sustained "
        "surge or a clean break. Ordinary wet-season rhythm; day-to-day "
        "detail matters more than the weekly headline."))


# --------------------------------------------------------------------------
# Spatial table
# --------------------------------------------------------------------------

def mmr_table(forecasts: dict, days: list[date]) -> str:
    header = "| Site | Zone | " + " | ".join(f"{d:%a %d}" for d in days) + " | Week |"
    sep = "|---|---|" + "---|" * (len(days) + 1)
    lines = [header, sep]

    for key in WEEKLY_KEYS:
        pf, site = forecasts.get(key), C.SITES_BY_KEY.get(key)
        if pf is None or site is None:
            continue
        cells, week_total = [], 0.0
        for d in days:
            idx = window_indices(pf.times, d, 0, 24)
            if not idx:
                cells.append("--")
                continue
            rs = daily_rain_spread(pf, idx)
            week_total += rs.median
            cells.append(f"{rs.lo:.0f}–{rs.hi:.0f}" if rs.hi >= 1 else "—")
        marker = " **(home)**" if key == C.HOME.key else ""
        lines.append(f"| {site.name}{marker} | {site.zone} | "
                     + " | ".join(cells) + f" | ~{week_total:.0f} mm |")

    out = "\n".join(lines) + "\n\n"
    out += (
        "> Cells show the **range across ECMWF, GFS and ICON** in mm/24h, not a "
        "single number — Guide Case Study F. The week column sums the daily "
        "medians and is an order-of-magnitude figure only.\n\n"
        "> The Matheran/Lonavala versus Pune contrast is the diagnostic to "
        "watch. A large contrast confirms a classic westerly regime with the "
        "rain shadow intact (Handbook Ch.14). A small contrast means deep "
        "moisture and broad ascent are overwhelming the shadow — usually an "
        "inland system, and usually a wetter, longer-lasting event for "
        "everyone including Pune (Guide Case Study E).\n\n"
        "> Note the zones: a westerly is *upslope* at Matheran and Lonavala but "
        "*descending* at Pune. Same wind, opposite effect — which is why Pune "
        "is carried here as a control rather than as another forecast point.\n"
    )
    return out


def day_narratives(pf, ens, days: list[date]) -> str:
    """One short paragraph per day, mechanism first."""
    out = ""
    for d in days:
        dd = diagnose_day(pf, d, ens=ens, primary_model=PRIMARY_MODEL)
        if dd is None:
            continue
        conf = assess_confidence(dd)
        if dd.ensemble and dd.ensemble.p_measurable is not None:
            chance = f"{dd.ensemble.p_measurable:.0%} chance of measurable rain"
        else:
            chance = (f"{dd.rain.agree_on_occurrence}/{dd.rain.n_models} models "
                      f"produce rain")
        out += (
            f"**{d:%A %d %B} — {dd.regime}**  \n"
            f"{dd.mechanism} "
            f"Guidance spans {dd.rain.lo:.0f}–{dd.rain.hi:.0f} mm "
            f"({dd.rain.category_hi.lower()}); {chance}. "
            f"Character: {dd.organisation.character}. "
            f"Confidence {conf.occurrence} on occurrence, {conf.amount} on "
            f"amount.\n\n"
        )
    return out


def trough_evolution(trough_t, days: list[date]) -> str:
    """Track the monsoon trough axis day by day - Handbook Ch.13."""
    from .diagnostics import monsoon_trough
    from .synoptic import _hour_index

    if trough_t is None:
        return "Trough transect unavailable this run.\n"

    lines = ["| Day | Trough axis | Phase |", "|---|---|---|"]
    for d in days:
        td = monsoon_trough(trough_t, _hour_index(trough_t, d))
        axis = f"{td.axis_lat:.1f}°N" if td.axis_lat is not None else "--"
        lines.append(f"| {d:%a %d %b} | {axis} | {td.phase} |")
    out = "\n".join(lines) + "\n\n"
    out += (
        "> Handbook Ch.13: a trough hugging the foothills (≥28°N) for several "
        "consecutive days is the break signal for Mumbai and Pune. A trough "
        "sitting back over the plains is the active signal. Watch the "
        "*direction of travel* across the week, not any single day.\n"
    )
    return out


# --------------------------------------------------------------------------
# Web payload builders - same numbers as the markdown, shaped for the page
# --------------------------------------------------------------------------

def _synoptic_html(markdown: str) -> str:
    out = _html.escape(markdown)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    parts = []
    for para in (p.strip() for p in out.split("\n\n")):
        if not para:
            continue
        para = para.replace("\n", " ")
        if para.startswith("&gt;"):
            parts.append(f'<div class="quote">{para[4:].strip()}</div>')
        else:
            parts.append(f"<p style='margin:0 0 10px'>{para}</p>")
    return "".join(parts)


def _driver_coherence(enso, iod, mjo, miso, regime: str) -> str:
    """
    Say plainly whether the background drivers and the week's local pattern
    agree - and when they don't, which one to trust.

    This matters because the two answer different questions. A seasonal tilt
    toward below-normal rain does not stop the Ghats squeezing a strong
    westerly next Tuesday. Presenting four indices without reconciling them
    against the actual forecast would leave the reader to guess.
    """
    tilts: list[str] = []
    score = 0

    if enso is not None:
        if enso.phase == "El Nino":
            tilts.append(f"{enso.strength} El Nino (against)"); score -= 1
        elif enso.phase == "La Nina":
            tilts.append(f"{enso.strength} La Nina (for)"); score += 1
    if iod is not None and iod.phase != "neutral":
        tilts.append(f"{iod.phase} IOD "
                     f"({'for' if iod.phase == 'positive' else 'against'})")
        score += 1 if iod.phase == "positive" else -1
    if mjo is not None:
        if mjo.favourability == "favourable":
            tilts.append(f"MJO phase {mjo.phase} (for)"); score += 1
        elif mjo.favourability == "unfavourable":
            tilts.append(f"MJO phase {mjo.phase} (against)"); score -= 1
    if miso is not None and miso.band_lat is not None:
        if 15 <= miso.band_lat < 22:
            tilts.append("MISO band overhead (for)"); score += 1
        elif miso.band_lat < 10:
            tilts.append("MISO band far south (against)"); score -= 1

    if not tilts:
        return ""

    local_wet = "ACTIVE" in regime or "PULSING" in regime
    summary = ", ".join(tilts)

    if score < 0 and local_wet:
        return (
            f"> **The drivers and this week disagree.** Background signals lean "
            f"dry ({summary}), yet the local pattern this week is *{regime.lower()}* "
            "— a strong onshore current aimed at the Ghats. Trust the local "
            "pattern for the next seven days. Seasonal tilts shift the odds "
            "over a whole monsoon; they do not stop terrain from wringing out "
            "a moist westerly on a given Tuesday. Where the tilt shows up is in "
            "how long the breaks run once this surge fades.")
    if score > 0 and not local_wet:
        return (
            f"> **The drivers and this week disagree.** Background signals lean "
            f"wet ({summary}) while the local pattern is *{regime.lower()}*. "
            "Trust the local pattern for the week ahead, but treat a return to "
            "active conditions as more likely than usual once the current lull "
            "breaks down.")
    if score < 0:
        return (f"> **Drivers and pattern agree — leaning dry.** {summary}, and "
                f"the local pattern is *{regime.lower()}*. Both point the same "
                "way, which raises confidence in a subdued stretch.")
    return (f"> **Drivers and pattern agree — leaning wet.** {summary}, and the "
            f"local pattern is *{regime.lower()}*. Both point the same way.")


def _wind_rows(pf, days: list[date]) -> list[dict]:
    ms = pf.models.get(PRIMARY_MODEL) or next(iter(pf.models.values()))
    rows = []
    for d in days:
        idx = window_indices(pf.times, d, 0, 24)
        if not idx:
            continue
        lp = lift_profile(ms, idx)
        rows.append({
            "day": d.strftime("%a %d %b"), "short": d.strftime("%a"),
            "dir": (f"{compass(lp.wind_850_dir)} ({lp.wind_850_dir:.0f}°)"
                    if lp.wind_850_dir is not None else "--"),
            "speed": round(lp.wind_850_speed or 0.0, 1),
            "orographic": round(lp.orographic_850 or 0.0, 1),
            "forcing": lp.forcing_class,
        })
    return rows


def _site_rows(forecasts: dict, days: list[date]) -> list[dict]:
    rows = []
    for key in WEEKLY_KEYS:
        pf, site = forecasts.get(key), C.SITES_BY_KEY.get(key)
        if pf is None or site is None:
            continue
        cells, week = [], 0.0
        for d in days:
            idx = window_indices(pf.times, d, 0, 24)
            if not idx:
                cells.append({"short": d.strftime("%a"), "lo": 0.0, "hi": 0.0,
                              "tip": "no data"})
                continue
            rs = daily_rain_spread(pf, idx)
            week += rs.median
            cells.append({
                "short": d.strftime("%a"),
                "lo": round(rs.lo, 1), "hi": round(rs.hi, 1),
                "tip": (f"{site.name} {d:%a %d %b}: {rs.lo:.0f}-{rs.hi:.0f} mm "
                        f"({rs.category_hi})"),
            })
        rows.append({"name": site.name, "zone": site.zone,
                     "home": key == C.HOME.key, "cells": cells,
                     "week": round(week, 1)})
    return rows


def _trough_rows(trough_t, days: list[date]) -> list[dict]:
    from .diagnostics import monsoon_trough
    from .synoptic import _hour_index

    rows = []
    for d in days:
        if trough_t is None:
            rows.append({"day": d.strftime("%a %d %b"), "axis": "--",
                         "phase": "unknown"})
            continue
        td = monsoon_trough(trough_t, _hour_index(trough_t, d))
        rows.append({
            "day": d.strftime("%a %d %b"),
            "axis": (f"{td.axis_lat:.1f}°N"
                     if td.axis_lat is not None else "--"),
            "phase": td.phase,
        })
    return rows


def _narrative_rows(pf, ens, days: list[date]) -> list[dict]:
    rows = []
    for d in days:
        dd = diagnose_day(pf, d, ens=ens, primary_model=PRIMARY_MODEL)
        if dd is None:
            continue
        conf = assess_confidence(dd)
        if dd.ensemble and dd.ensemble.p_measurable is not None:
            chance = f"{dd.ensemble.p_measurable:.0%} chance of measurable rain"
        else:
            chance = (f"{dd.rain.agree_on_occurrence}/{dd.rain.n_models} models "
                      f"produce rain")
        rows.append({
            "day": d.strftime("%A %d %B"),
            "regime": dd.regime,
            "text": (f"{dd.mechanism} Guidance spans {dd.rain.lo:.0f}-"
                     f"{dd.rain.hi:.0f} mm ({dd.rain.category_hi.lower()}); "
                     f"{chance}. Character: {dd.organisation.character}. "
                     f"Confidence {conf.occurrence} on occurrence, "
                     f"{conf.amount} on amount."),
        })
    return rows


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def run(start: date | None = None, *, days: int = 7, quiet: bool = False,
        no_notify: bool = False) -> tuple[str, str]:
    issued = datetime.now()
    start = start or issued.date()
    day_list = [start + timedelta(days=i) for i in range(days)]
    season = season_for(start)

    if not quiet:
        print(f"MMR weekly outlook from {start:%Y-%m-%d} ({days} days)")
        print("  fetching home point + ensemble...")

    pf = fetch_point(C.HOME, days=days + 1)
    ens = fetch_ensemble(C.HOME, days=days + 1)

    if not quiet:
        print(f"  fetching {len(WEEKLY_KEYS)} MMR sites...")
    sites = [C.SITES_BY_KEY[k] for k in WEEKLY_KEYS if k in C.SITES_BY_KEY]
    forecasts = fetch_sites(sites, days=days + 1)

    if not quiet:
        print("  fetching synoptic pressure field...")
    trough_t, offshore_t, inland_t = synoptic.fetch_synoptic(days=days + 1)
    sp = synoptic.build(trough_t, offshore_t, inland_t, start, season)

    regime, regime_note = week_regime(pf, day_list)

    if not quiet:
        print("  tracking low pressure systems...")
    sys_pic = systems.analyse(days=days + 1, today=start, quiet=quiet)

    if not quiet:
        print("  computing Indian Ocean Dipole...")
    iod = climate.compute_iod(quiet=quiet)

    if not quiet:
        print("  fetching ENSO state...")
    enso = climate.fetch_enso(today=start, quiet=quiet)

    if not quiet:
        print("  deriving MJO and MISO from the convection field...")
    mjo = oscillations.analyse_mjo(today=start, quiet=quiet)
    miso = oscillations.analyse_miso(today=start, quiet=quiet)
    crosscheck = oscillations.cpc_crosscheck(mjo)

    if not quiet:
        print("  sampling upstream drivers (Somali jet, dry-air intrusion)...")
    up = upstream.fetch(days=len(day_list), quiet=quiet)

    # ---- area roll-up, alerts, heat/cold across the region ---------------
    areas = plain.summarise_areas(forecasts, day_list, C.MMR_AREAS)

    week_diag = [d for d in
                 (diagnose_day(pf, wd, ens=ens, primary_model=PRIMARY_MODEL)
                  for wd in day_list) if d is not None]

    if not quiet:
        print("  building heat/cold outlook across the MMR...")
    thermals: list = []
    for key in THERMAL_KEYS:
        site = C.SITES_BY_KEY.get(key)
        spf = forecasts.get(key)
        if site is None or spf is None:
            continue
        thermals.append(thermal.analyse(site, spf, day_list,
                                        primary_model=PRIMARY_MODEL,
                                        quiet=quiet))

    home_thermal = next((t for t in thermals
                         if t.site_name == C.HOME.name), None)
    alerts = plain.detect_shifts(week_diag, thermal_outlook=home_thermal,
                                 systems_picture=sys_pic)

    # ---- render ----------------------------------------------------------
    out = report.h(1, "Mumbai MMR — weekly outlook")
    out += (f"**Issued** {issued:%Y-%m-%d %H:%M IST}  \n"
            f"**Valid** {day_list[0]:%a %d %b} – {day_list[-1]:%a %d %b %Y}  \n"
            f"**Method** wind-pattern analysis — 850 hPa terrain-normal "
            f"component, monsoon trough position, offshore trough  \n"
            f"**Models** {', '.join(pf.model_labels())}\n\n---\n\n")

    out += report.h(2, "Week in one line")
    out += f"**{regime}.** {regime_note}\n\n"

    out += report.h(2, "⚡ Major weather shifts this week")
    out += plain.render_alerts(alerts) + "\n"

    out += report.h(2, "Across the MMR — area by area")
    out += plain.render_areas(areas, home_key=C.HOME_AREA) + "\n"

    out += report.h(2, "Upstream drivers — Somali jet and mid-level dry air")
    out += upstream.render_zones(up, day=day_list[0]) + "\n"

    out += report.h(2, "Low pressure systems, troughs and storms")
    out += systems.render(sys_pic)

    if thermals:
        out += report.h(2, "Heat and cold across the region")
        out += ("Handbook Ch.19 makes this a gradient, not a number: coastal "
                "Mumbai is sea-breeze protected, Kalyan sits just inland of "
                "that protection, and the plateau is exposed. Same week, "
                "different exposure.\n\n")
        out += ("| Place | Type | Warmest | Coolest night | Peak feels-like | "
                "Flag |\n|---|---|---|---|---|---|\n")
        for t in thermals:
            hot = max((d for d in t.days if d.tmax is not None),
                      key=lambda x: x.tmax, default=None)
            cold = min((d for d in t.days if d.tmin is not None),
                       key=lambda x: x.tmin, default=None)
            hi = max((d.heat_index for d in t.days
                      if d.heat_index is not None), default=None)
            flag = ""
            if len(t.heat_spell) >= 2:
                flag = "🔥 heatwave criteria"
            elif t.heat_spell:
                flag = "🌡️ one hot day"
            elif len(t.cold_spell) >= 2:
                flag = "🥶 cold criteria"
            elif hi is not None and hi >= 41:
                flag = "🥵 danger (feels-like)"
            elif hi is not None and hi >= 32:
                flag = "🟡 extreme caution"
            out += (f"| {t.site_name} | {t.station_type} | "
                    f"{hot.tmax:.0f}°C ({hot.day:%a}) | "
                    f"{cold.tmin:.0f}°C ({cold.day:%a}) | "
                    f"{hi:.0f}°C | {flag} |\n")
        out += (f"\n> {thermals[0].caveat}\n\n")

    out += report.h(2, "Synoptic setting")
    out += synoptic.render(sp) + "\n"
    if enso is not None or iod is not None or mjo is not None:
        out += report.h(3, "Background climate drivers")
        out += ("These set the season's odds, not any single day. Read them as "
                "a tilt underneath the week-to-week pattern above.\n\n")
        if enso is not None:
            out += climate.render_enso(enso) + "\n"
        if iod is not None:
            out += climate.render(iod) + "\n"
        if mjo is not None or miso is not None:
            out += oscillations.render(mjo, miso, crosscheck) + "\n"

        verdict = _driver_coherence(enso, iod, mjo, miso, regime)
        if verdict:
            out += verdict + "\n"

    out += report.h(2, "Wind pattern — the spine of this outlook")
    out += wind_pattern_table(pf, day_list) + "\n"
    out += wind_profile_strip(pf, day_list) + "\n"

    out += report.h(2, "Monsoon trough through the week")
    out += trough_evolution(trough_t, day_list) + "\n"

    out += report.h(2, "Rainfall across the MMR")
    out += mmr_table(forecasts, day_list) + "\n"

    out += report.h(2, "Day by day")
    out += day_narratives(pf, ens, day_list)

    out += report.h(2, "How to read this")
    out += (
        "Guide s18 sets the honest precision for each lead time:\n\n"
        "- **Days 1–2** — probability, broad timing, footprint and intensity "
        "category are all defensible.\n"
        "- **Days 3–5** — trend and risk window only. Hedge the language; "
        "avoid exact hourly or suburb-level claims.\n"
        "- **Days 6–7** — scenario outlook only; confidence should be "
        "explicitly low.\n\n"
        "Handbook Ch.27: what real skill looks like is high confidence 0–2 "
        "days out on broad categories, good confidence 3–5 days out on the "
        "pattern, and low confidence beyond 7–10 days on anything specific. "
        "Anyone promising more than that is not being straight with you.\n\n"
    )

    out += report.windy_section(
        diagnostic_links(C.HOME.lat, C.HOME.lon, season=season),
        "Verify this on Windy",
    )
    out += report.imd_section()
    out += "---\n\n" + DISCLAIMER + "\n"

    path = C.FORECAST_DIR / f"{start:%Y-%m-%d}_mmr_weekly.md"
    path.write_text(out, encoding="utf-8")

    # ---- web page --------------------------------------------------------
    weekly_payload = web.build_weekly_payload(
        day_list,
        _wind_rows(pf, day_list),
        _site_rows(forecasts, day_list),
        _trough_rows(trough_t, day_list),
        _narrative_rows(pf, ens, day_list),
        (regime, regime_note),
        issued,
        _synoptic_html(synoptic.render(sp)),
    )
    weekly_payload["alerts"] = [
        {"severity": a.severity, "icon": a.icon, "title": a.title,
         "body": a.body, "label": plain.SEVERITY_LABEL.get(a.severity, "")}
        for a in alerts
    ]
    weekly_payload["areas"] = [
        {"key": a.key, "name": a.name, "character": a.character,
         "weekMm": round(a.week_mm, 1),
         "wettestDay": a.wettest_day.strftime("%a %d") if a.wettest_day else "",
         "wettestMm": round(a.wettest_mm, 1), "band": a.band,
         "plain": a.plain, "rank": a.rank,
         "home": a.key == C.HOME_AREA}
        for a in areas
    ]
    weekly_payload["systems"] = {
        "trough": sys_pic.trough.note if sys_pic else "",
        "cycloneWindow": bool(sys_pic and sys_pic.cyclone_window),
        "list": [
            {"relevance": a.relevance, "headline": a.headline,
             "reasoning": a.reasoning,
             "distanceKm": round(a.track.closest_approach.distance_km),
             "pressure": round(a.track.peak.pressure, 1),
             "movedKm": round(a.track.moved_km)}
            for a in (sys_pic.significant if sys_pic else [])
        ],
    }
    # Drivers are cached separately so the daily page and `render` can show
    # them too - they are computed here because this is the run that already
    # pays for the synoptic fetches.
    drivers = {
        "enso": ({"phase": enso.phase, "strength": enso.strength,
                  "anomaly": enso.anomaly, "text": enso.interpretation,
                  "when": f"{enso.year}-{enso.month:02d}",
                  "lag": enso.months_lag,
                  "recent": [a for _y, _m, a in enso.recent]}
                 if enso else None),
        "iod": ({"phase": iod.phase, "dmi": round(iod.dmi, 2),
                 "text": iod.interpretation} if iod else None),
        "mjo": ({"phase": mjo.phase, "region": mjo.region,
                 "favourability": mjo.favourability, "text": mjo.interpretation,
                 "lon": round(mjo.prop.current or 0, 1),
                 "track": [round(v, 1) for _d, v in mjo.prop.recent]}
                if mjo else None),
        "miso": ({"regime": miso.regime, "lat": round(miso.band_lat or 0, 1),
                  "text": miso.interpretation, "eta": miso.days_to_arrival,
                  "track": [round(v, 1) for _d, v in miso.prop.recent]}
                 if miso else None),
        "coherence": _driver_coherence(enso, iod, mjo, miso, regime),
        "crosscheck": crosscheck,
    }
    web.cache_payload(drivers, C.CACHE_DIR / "drivers.json")
    weekly_payload["drivers"] = drivers

    weekly_payload["thermalRows"] = [
        {"place": t.site_name, "type": t.station_type,
         "warmest": max((d.tmax for d in t.days if d.tmax is not None), default=None),
         "coolest": min((d.tmin for d in t.days if d.tmin is not None), default=None),
         "peakFeels": max((d.heat_index for d in t.days
                           if d.heat_index is not None), default=None),
         "heatSpell": len(t.heat_spell), "coldSpell": len(t.cold_spell)}
        for t in thermals
    ]
    page = C.FORECAST_DIR / "mmr.html"
    page.write_text(
        web.render(weekly_payload,
                   diagnostic_links(C.HOME.lat, C.HOME.lon, season=season),
                   []),
        encoding="utf-8",
    )
    web.cache_payload(weekly_payload, web.WEEKLY_CACHE)

    head = f"MMR week ahead: {regime.lower()}. {regime_note.split('.')[0]}."
    if alerts and alerts[0].severity in ("critical", "warning"):
        head = f"{alerts[0].icon} {alerts[0].title}. MMR: {regime.lower()}."
    if not quiet:
        print(f"\n{head}\n  written: {path}")
    if not no_notify:
        notify("MMR weekly outlook", head, launch=page.resolve().as_uri())

    return head, str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly Mumbai MMR wind outlook.")
    ap.add_argument("--start", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)

    start = date.fromisoformat(args.start) if args.start else None
    run(start, days=args.days, quiet=args.quiet, no_notify=args.no_notify)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
