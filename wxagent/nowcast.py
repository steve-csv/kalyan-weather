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

# --------------------------------------------------------------------------
# Answering the five questions from gridded data
# --------------------------------------------------------------------------
# The five-question scan is written to be run against a radar loop. This
# answers the same five from the model analysis instead - sampling an upstream
# fan, reading the steering flow, and comparing the last few hours.
#
# It is NOT radar and does not pretend to be. Radar sees what is falling now;
# this sees what the analysis thinks is falling, which lags and smooths. The
# answers are a structured starting point that updates itself every run, and
# the radar loop remains the authority inside three hours - which is why the
# links stay directly above them.

import math

EARTH_R_KM = 6371.0


def _destination(lat: float, lon: float, bearing_deg: float,
                 dist_km: float) -> tuple[float, float]:
    """Point `dist_km` from (lat, lon) along `bearing_deg`."""
    br = math.radians(bearing_deg)
    d = dist_km / EARTH_R_KM
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d)
                   + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


# Ranges sampled upstream, in km. Beyond ~200 km a cell will have evolved
# beyond recognition before it arrives, so there is little point looking further.
SCAN_RANGES = (25.0, 50.0, 75.0, 100.0, 150.0, 200.0)
SCAN_SPREAD = (-40.0, -20.0, 0.0, 20.0, 40.0)   # degrees either side of upstream


@dataclass
class ScanCell:
    distance_km: float
    bearing_deg: float
    mm_now: float
    mm_prev: float

    @property
    def trend(self) -> float:
        return self.mm_now - self.mm_prev


@dataclass
class ScanAnswers:
    upstream_bearing: float
    steer_speed_kmh: float
    nearest: ScanCell | None
    cells: list[ScanCell]
    eta_hours: float | None
    answers: list[tuple[str, str]]
    generated: datetime



def compass_from(bearing_deg: float | None) -> str:
    """Compass point a flow is coming FROM, for the page's animation label."""
    from .diagnostics import compass
    if bearing_deg is None:
        return "the west"
    return f"the {compass(bearing_deg)}"

def _bearing_between(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def scan(site, *, now: datetime | None = None,
         quiet: bool = True) -> ScanAnswers | None:
    """
    Sample an upstream fan and answer the five questions.

    The fan is aimed INTO the steering flow: where the air is coming from is
    where the next few hours' weather is coming from. A fixed westward box
    would miss the band entirely on a day the flow backs southerly.
    """
    from .diagnostics import compass, _circular_mean
    from .sources import _get_json
    from . import config as _C

    now = now or datetime.now()

    # 1. Steering flow at 850 hPa over the home point, from the current run.
    try:
        steer = _get_json(_C.FORECAST_URL, {
            "latitude": site.lat, "longitude": site.lon,
            "hourly": "wind_speed_850hPa,wind_direction_850hPa",
            "models": _C.MODELS[0].key, "forecast_days": 1, "past_days": 1,
            "timezone": _C.TIMEZONE, "wind_speed_unit": "kmh",
        }, timeout=60)
    except Exception:                             # noqa: BLE001
        return None
    if isinstance(steer, list):
        steer = steer[0]
    sh = steer.get("hourly", {})
    times = sh.get("time", [])
    idx = [i for i, t in enumerate(times)
           if abs((datetime.fromisoformat(t) - now).total_seconds()) < 5400]
    if not idx:
        return None
    spd = [sh.get("wind_speed_850hPa", [])[i] for i in idx]
    dirs = [sh.get("wind_direction_850hPa", [])[i] for i in idx]
    spd = [v for v in spd if v is not None]
    from_dir = _circular_mean([v for v in dirs if v is not None])
    if not spd or from_dir is None:
        return None
    steer_kmh = sum(spd) / len(spd)

    # 2. Sample the fan upstream, i.e. toward where the wind is coming FROM.
    points, meta = [], []
    for off in SCAN_SPREAD:
        br = (from_dir + off) % 360
        for rng in SCAN_RANGES:
            la, lo = _destination(site.lat, site.lon, br, rng)
            points.append((la, lo))
            meta.append((rng, br))

    try:
        raw = _get_json(_C.FORECAST_URL, {
            "latitude": ",".join(f"{p[0]:.3f}" for p in points),
            "longitude": ",".join(f"{p[1]:.3f}" for p in points),
            "hourly": "precipitation",
            "models": _C.MODELS[0].key,
            "past_days": 1, "forecast_days": 1,
            "timezone": _C.TIMEZONE,
        }, timeout=90)
    except Exception:                             # noqa: BLE001
        return None
    if not isinstance(raw, list):
        raw = [raw]

    cells: list[ScanCell] = []
    for (rng, br), loc in zip(meta, raw):
        h = loc.get("hourly", {})
        ts, pr = h.get("time", []), h.get("precipitation", [])
        cur = prev = 0.0
        for t, v in zip(ts, pr):
            if v is None:
                continue
            dt = datetime.fromisoformat(t)
            age = (now - dt).total_seconds() / 3600.0
            if -1 <= age < 1:
                cur = max(cur, v)
            elif 1 <= age < 3:
                prev = max(prev, v)
        if cur > 0.05 or prev > 0.05:
            cells.append(ScanCell(rng, br, round(cur, 2), round(prev, 2)))

    active = [c for c in cells if c.mm_now >= 0.2]
    nearest = min(active, key=lambda c: c.distance_km) if active else None
    eta = (nearest.distance_km / steer_kmh) if (nearest and steer_kmh > 1) else None

    # ---- compose the five answers ----
    A: list[tuple[str, str]] = []

    if nearest is None:
        A.append((RADAR_SCAN_QUESTIONS[0],
                  "Nothing active in the upstream fan out to 200 km. Any rain "
                  "in the next couple of hours would have to develop locally "
                  "rather than arrive."))
    else:
        A.append((RADAR_SCAN_QUESTIONS[0],
                  f"Yes — nearest active area about "
                  f"**{nearest.distance_km:.0f} km** away, upstream toward "
                  f"{compass(nearest.bearing_deg)}. "
                  f"{len(active)} of {len(points)} sampled points are wet."))

    A.append((RADAR_SCAN_QUESTIONS[1],
              f"Steering flow is from {compass(from_dir)} at about "
              f"**{steer_kmh:.0f} km/h**, so anything upstream should track "
              f"toward {compass((from_dir + 180) % 360)}"
              + (f" and reach Kalyan in roughly **{eta:.1f} h** if it holds "
                 "together." if eta else ".")))

    if not active:
        A.append((RADAR_SCAN_QUESTIONS[2], "Nothing to assess."))
    else:
        rising = sum(1 for c in active if c.trend > 0.2)
        falling = sum(1 for c in active if c.trend < -0.2)
        moving = rising + falling
        # A trend needs a meaningful share of the field to be moving. One
        # point out of twelve changing while eleven hold steady is noise, and
        # calling that "strengthening" would be a confident answer to a
        # question the data has not answered.
        if moving < max(2, len(active) * 0.25):
            verb = "is" if moving == 1 else "are"
            verdict = (f"**Holding** — only {moving} of {len(active)} active "
                       f"points {verb} changing appreciably, which is too few "
                       "to call a trend either way.")
        elif rising > falling * 1.5:
            verdict = (f"**Strengthening** — {rising} of {len(active)} active "
                       f"points intensifying against {falling} easing.")
        elif falling > rising * 1.5:
            verdict = (f"**Weakening** — {falling} of {len(active)} easing "
                       f"against {rising} intensifying, so expect less than "
                       "the raw distance suggests.")
        else:
            verdict = (f"**Mixed** — {rising} intensifying, {falling} easing. "
                       "No clear direction.")
        A.append((RADAR_SCAN_QUESTIONS[2], verdict))

    far = [c for c in active if c.distance_km >= 100]
    near = [c for c in active if c.distance_km < 100]
    if far and near:
        A.append((RADAR_SCAN_QUESTIONS[3],
                  f"Yes — {len(far)} active area(s) beyond 100 km behind the "
                  f"{len(near)} closer in. That is a train, not a single band: "
                  "the first arrival is unlikely to be the last."))
    elif far:
        A.append((RADAR_SCAN_QUESTIONS[3],
                  "Activity is all beyond 100 km — developing upstream but not "
                  "yet close. Worth re-checking in an hour."))
    elif near:
        A.append((RADAR_SCAN_QUESTIONS[3],
                  "Nothing new behind the near activity — what is close now "
                  "looks like the whole of it for the moment."))
    else:
        A.append((RADAR_SCAN_QUESTIONS[3], "No upstream development."))

    if not active:
        A.append((RADAR_SCAN_QUESTIONS[4], "Nothing to track."))
    elif steer_kmh < 20:
        A.append((RADAR_SCAN_QUESTIONS[4],
                  f"Slow steering ({steer_kmh:.0f} km/h). Anything that forms "
                  "will move little and can sit over the same suburbs — the "
                  "setup that produces large local totals from a modest-looking "
                  "system."))
    elif far and near:
        A.append((RADAR_SCAN_QUESTIONS[4],
                  "Repeat likely — successive areas along the same track will "
                  "cross in sequence rather than one band clearing through."))
    else:
        A.append((RADAR_SCAN_QUESTIONS[4],
                  f"Fast steering ({steer_kmh:.0f} km/h) with a single area — "
                  "more likely one spell passing through than a repeat."))

    return ScanAnswers(upstream_bearing=from_dir, steer_speed_kmh=steer_kmh,
                       nearest=nearest, cells=cells, eta_hours=eta,
                       answers=A, generated=now)


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

def render_scan(sa: ScanAnswers | None) -> str:
    """The five questions, answered."""
    if sa is None:
        return ""
    out = ("**The five-question scan, answered from the current analysis** "
           f"(as of {sa.generated:%H:%M}).\n\n")
    for i, (q, a) in enumerate(sa.answers, 1):
        out += f"{i}. **{q}**  \n   {a}\n\n"
    out += (
        "> These answers come from the gridded analysis, not from radar. The "
        "analysis lags and smooths, so it will miss a cell that has just fired "
        "and will understate an intense core. Inside three hours the radar loop "
        "above is still the authority — use these to know *what to look for* "
        "on it.\n"
    )
    return out


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

    # The questions themselves are not listed here - render_scan() prints them
    # with their answers immediately below, and printing them twice made the
    # section read as though the checklist had gone unanswered.
    out += "> **What radar will not tell you** (Guide §16.2): "
    out += " ".join(RADAR_LIMITS)
    out += ("\n>\n> This agent deliberately does not compute arrival times from "
            "radar images. Inverting a colour scale into reflectivity and "
            "extrapolating it would produce a number that looks precise and "
            "is not. Use IMD's own nowcasts for anything safety-critical.\n\n")
    return out
