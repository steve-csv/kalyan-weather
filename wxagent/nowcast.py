"""
Nowcasting - the 0 to 6 hour window, where radar outranks every model.

Guide s18 sets the hierarchy plainly: for 0-3 hours the primary tools are
"radar animation, satellite, surface observations, storm-motion estimate", and
Guide s15 Step 2 says "for the next few hours, radar and satellite usually
outrank model rainfall output."

WHAT THIS MODULE DOES AND DOES NOT DO
-------------------------------------
It surfaces the live IMD Mumbai radar products and a radar animation, and
pairs them with the five-question radar scan from Guide s16.1 so the read is
structured rather than a stare.

It does NOT claim to automate echo tracking. Deriving reflectivity, motion
vectors and arrival times from scraped radar rasters is genuinely error-prone -
the colour scale has to be inverted, the beam geometry ignored, and the result
would carry an authority it has not earned. Guide s16.2 lists exactly why the
raw image misleads: the beam rises with distance so distant shallow rain is
partly missed, hills and buildings create gaps and artifacts, reflectivity
aloft is not rainfall at the ground, and bands change shape and speed so linear
ETAs are wrong. The honest product is the animation plus the questions.

What IS automated is the model side of the short range: the next six hours of
hourly precipitation, its trend, and whether anything is arriving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from . import config as C

# --------------------------------------------------------------------------
# IMD Mumbai radar products (verified live)
# --------------------------------------------------------------------------

IMD_RADAR_BASE = "https://mausam.imd.gov.in/Radar"

IMD_RADAR_PRODUCTS: tuple[tuple[str, str, str], ...] = (
    ("caz_mum.gif", "MAX-Z (maximum reflectivity)",
     "The default read. Shows the strongest echo in the column, so growing "
     "cells stand out before they reach the ground. Guide s16: animate it — "
     "one frame is not a forecast."),
    ("sri_mum.gif", "Surface Rainfall Intensity",
     "Reflectivity converted to a rain rate at the surface. Closer to what you "
     "will actually feel than raw MAX-Z, but inherits every assumption in the "
     "Z-R conversion."),
    ("ppi_mum.gif", "PPI (Z)",
     "Single-elevation scan. Useful for structure close to the radar."),
    ("pac_mum.gif", "Precipitation Accumulation",
     "How much has already fallen. The check on whether a spell that is "
     "clearing has already done its damage."),
    ("ppv_mum.gif", "PPI (V) — Doppler velocity",
     "Motion toward and away from the radar. This is where band movement is "
     "read directly rather than inferred by eye across frames."),
)

IMD_RADAR_PAGE = "https://mausam.imd.gov.in/imd_latest/contents/index_radar.php"
RAINVIEWER_MAP = "https://www.rainviewer.com/map.html?loc=19.2437,73.1305,8"


def radar_url(product: str, *, bust: str | None = None) -> str:
    """IMD radar image URL. A cache-buster is essential — these are overwritten
    in place at the same path every scan, so a browser will happily show a
    stale image for hours."""
    stamp = bust or datetime.now().strftime("%Y%m%d%H%M")
    return f"{IMD_RADAR_BASE}/{product}?t={stamp}"


# --------------------------------------------------------------------------
# The five-question radar scan (Guide s16.1)
# --------------------------------------------------------------------------

RADAR_SCAN_QUESTIONS: tuple[str, ...] = (
    "Is precipitation already present, and how far is it from Kalyan?",
    "What direction and speed is the echo moving?",
    "Is it strengthening, weakening or holding intensity?",
    "Is new development occurring upstream or along its boundary?",
    "Will the echo repeat over the same place, or is it one fast-moving band?",
)

RADAR_LIMITS: tuple[str, ...] = (
    "The beam rises with distance, so distant shallow rain can be partly missed.",
    "Hills, buildings and filtering produce gaps and artifacts.",
    "Reflectivity aloft is not the same as rain reaching the ground.",
    "Bands change shape and speed — a straight-line arrival estimate will drift.",
)


# --------------------------------------------------------------------------
# Model-side short range
# --------------------------------------------------------------------------

@dataclass
class ShortRange:
    issued: datetime
    hours: list[tuple[datetime, float]]
    total_mm: float
    peak_mm_h: float
    first_wet_hour: datetime | None
    trend: str
    verdict: str


def short_range(pf, *, primary_model: str = "ecmwf_ifs025",
                hours_ahead: int = 6,
                now: datetime | None = None) -> ShortRange | None:
    """Next few hours of modelled precipitation at the home point."""
    now = now or datetime.now()
    ms = pf.models.get(primary_model) or next(iter(pf.models.values()))

    rows: list[tuple[datetime, float]] = []
    for i, t in enumerate(pf.times):
        dt = datetime.fromisoformat(t)
        if now <= dt <= now + timedelta(hours=hours_ahead):
            rows.append((dt, ms.at("precipitation", i) or 0.0))
    if not rows:
        return None

    total = sum(v for _, v in rows)
    peak = max(v for _, v in rows)
    first_wet = next((dt for dt, v in rows if v >= 0.2), None)

    half = max(1, len(rows) // 2)
    early = sum(v for _, v in rows[:half])
    late = sum(v for _, v in rows[half:])
    if late > early * 1.5 and late > 1.0:
        trend = "increasing"
    elif early > late * 1.5 and early > 1.0:
        trend = "easing"
    elif total < 0.5:
        trend = "dry"
    else:
        trend = "steady"

    if total < 0.5:
        verdict = ("Models show nothing meaningful in the next few hours. "
                   "Check radar anyway — a cell that has already formed will "
                   "beat the model to it.")
    elif peak >= C.HEAVY_SPELL_MM_PER_H:
        verdict = (f"An intense burst is modelled, peaking near "
                   f"{peak:.0f} mm/h. Radar should already show it building "
                   "upstream if it is real.")
    elif first_wet is not None:
        verdict = (f"Rain modelled from about {first_wet:%H:%M}, "
                   f"{total:.0f} mm over the window, {trend}.")
    else:
        verdict = f"Light and intermittent, {total:.0f} mm over the window."

    return ShortRange(issued=now, hours=rows, total_mm=total, peak_mm_h=peak,
                      first_wet_hour=first_wet, trend=trend, verdict=verdict)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render(sr: ShortRange | None) -> str:
    out = "Guide §18: for the next 0–3 hours, **radar outranks every model on this page.**\n\n"

    if sr is not None:
        out += f"**Model short range (next {len(sr.hours)} h).** {sr.verdict}\n\n"
        out += "| Hour | Rain |\n|---|---|\n"
        for dt, v in sr.hours:
            bar = "█" * min(20, int(v * 2)) if v > 0 else "·"
            out += f"| {dt:%H:%M} | {v:.1f} mm {bar} |\n"
        out += "\n"

    out += "**Live IMD Mumbai radar**\n\n"
    for product, name, why in IMD_RADAR_PRODUCTS:
        out += f"- [{name}]({radar_url(product)}) — {why}\n"
    out += (f"\n- [IMD radar page (animated)]({IMD_RADAR_PAGE}) — the sequence, "
            "which is the one that actually matters\n"
            f"- [RainViewer animation]({RAINVIEWER_MAP}) — ~2 h of frames, "
            "easier to loop than IMD's viewer\n\n")

    out += "**The five-question scan** (Guide §16.1)\n\n"
    for i, q in enumerate(RADAR_SCAN_QUESTIONS, 1):
        out += f"{i}. {q}\n"
    out += "\n> **What radar will not tell you** (Guide §16.2): "
    out += " ".join(RADAR_LIMITS)
    out += ("\n>\n> This agent deliberately does not compute arrival times from "
            "radar images. Inverting a colour scale into reflectivity and "
            "extrapolating it would produce a number that looks precise and "
            "is not. Use IMD's own nowcasts for anything safety-critical.\n\n")
    return out
