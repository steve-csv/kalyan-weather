"""
Upstream drivers: the Somali jet and mid-level dry-air intrusion.

Everything else in this agent looks at the column over the MMR. These two
mechanisms sit hundreds to thousands of kilometres upwind, and they decide what
that column is *made of* before it ever arrives.

THE SOMALI (FINDLATER) JET
--------------------------
Southeast trades cross the equator off East Africa, are turned by the reversed
Coriolis force into a southwesterly stream, and are squeezed against the
Ethiopian highlands into a narrow low-level jet with its core near 850 hPa. One
branch runs northeast across the Arabian Sea toward the Konkan. It is the
conveyor that carries the monsoon's moisture; when it slackens, the supply line
to the Ghats slackens with it.

MID-LEVEL DRY-AIR INTRUSION
---------------------------
Air subsiding over Arabia, the Thar and the northern Arabian Sea is extremely
dry between roughly 700 and 500 hPa - measured values of 10-20% RH are routine
in the source region. When that layer is drawn over the MMR, rising parcels
entrain it and their buoyancy collapses. This is the mechanism behind the
Guide's warning that air can be "moist at 850 but dry at 700": the low levels
look perfect, the sky stays flat, and the model rain never materialises.

WHAT THE MEASUREMENTS SAID
--------------------------
Both were validated against three monsoons of ERA5 rainfall (2024-26, 316 days)
before being given any weight here, and the measurements changed the design
twice:

  1. THE LAG IS ZERO, NOT TWO TO THREE DAYS. The physical reasoning - a parcel
     takes ~2.5 days to cross the Arabian Sea at jet speed - suggested today's
     Somali jet should predict rain two or three days out. It does not.
     Correlation is strongest at lag 0 and decays steadily (corridor index:
     +0.55, +0.47, +0.39, +0.32, +0.26, +0.20 at lags 0-5). These indices
     describe the state of the monsoon circulation, and that whole circulation
     strengthens and weakens as one system. So they are read from the FORECAST
     fields at each target day, never from today's value projected forward.

  2. THE CORRIDOR BEATS THE CORE. The jet measured over the mid-Arabian Sea
     (rho +0.55) carries far more information for the MMR than the jet measured
     at its Somali source (+0.39). Only one branch of the source jet turns
     toward India; the corridor is the part that actually arrives. The core is
     still sampled, because a corridor pulse with no source behind it is a
     pulse that will fade.

ZONE SENSITIVITY IS REAL AND WAS ALSO MEASURED
----------------------------------------------
The same upstream state does not mean the same thing everywhere in the MMR.
Correlation of the corridor index against observed rainfall, by zone:

    ghat        +0.63     (Malshej +0.68, Igatpuri +0.65 - the most exposed)
    coastal     +0.59
    transition  +0.55     (Kalyan West +0.55)
    leeward     +0.29     (Pune - half the sensitivity of the crest)

That ordering is the orographic story in one column: a stronger jet means a
stronger terrain-normal component, and the Ghats convert that into rain far
more efficiently than the plains do. Pune sits in the rain shadow, where the
jet's strength matters much less than whether anything survives the crest.

WHAT THIS DOES NOT DO
---------------------
It does not adjust the rainfall numbers. The quantitative forecast stays the
multi-model median; inventing a correction factor from a correlation of 0.55
would be false precision. What these indices do is tell you which way the
models are likely to be wrong, and how much confidence the wording deserves -
which is exactly where the backtest said the agent's weakness lies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from . import config as C
from .diagnostics import _circular_mean, compass
from .sources import FetchError, _get_json

# --------------------------------------------------------------------------
# Sampling geometry
# --------------------------------------------------------------------------

# The Findlater jet core off Somalia - the source region.
CORE_POINTS: tuple[tuple[float, float], ...] = (
    (10.0, 52.0), (8.0, 55.0), (12.0, 50.0),
)

# The mid-Arabian Sea corridor - the branch that actually reaches the Konkan.
# This is the primary index; see the module docstring for why.
CORRIDOR_POINTS: tuple[tuple[float, float], ...] = (
    (14.0, 62.0), (17.0, 68.0),
)

# The dry-air source: Thar, Kutch and the sea off Saurashtra. Deliberately
# north and west of the MMR, so the index measures an upstream airmass rather
# than the local weather it is supposed to be explaining.
DRY_POINTS: tuple[tuple[float, float], ...] = (
    (22.0, 68.0), (24.0, 71.0), (20.0, 65.0),
)

ALL_POINTS = CORE_POINTS + CORRIDOR_POINTS + DRY_POINTS

FIELDS = ("wind_speed_850hPa,wind_direction_850hPa,"
          "wind_direction_600hPa,wind_speed_600hPa,"
          "relative_humidity_700hPa,relative_humidity_600hPa,"
          "relative_humidity_500hPa")

# Whether the dry airmass is being steered onto us is judged at 600 hPa, not
# 850. The dry layer sits at mid-level; the 850 hPa flow beneath it is the
# monsoon westerly and says nothing about where the dry air above is going.
ADVECTION_TOLERANCE_DEG = 75.0

# Zone sensitivity, from the measured per-zone correlations above. Used to
# scale how strongly the upstream state is allowed to colour the wording - not
# to scale any rainfall number.
ZONE_SENSITIVITY: dict[str, float] = {
    "ghat": 1.00,
    "coastal": 0.94,
    "transition": 0.87,
    "leeward": 0.46,
}


# --------------------------------------------------------------------------
# Bands - measured percentiles, not round numbers
# --------------------------------------------------------------------------

# Quintile edges of the 2024-26 monsoon distribution (316 days), with the
# rainfall actually observed at Kalyan West in each band. These are not round
# numbers because the atmosphere does not produce round numbers - a "20 m/s
# jet" threshold would have been invented, and these were counted.
#
#   corridor  <9.1   8.6 mm/day mean, rain on 56% of days,  1 of 12 heavy days
#             9.1    9.9                    70%             0
#             13.2  13.7                    86%             1
#             16.0  20.4                    98%             3
#             19.5  32.5                   100%             7
#
# The top quintile of jet strength contains 7 of the 12 heavy days in three
# monsoons, and the top two quintiles contain 10 of 12. That makes this the
# most useful heavy-rain discriminator in the agent - which matters, because
# the backtest showed heavy rain is precisely where it is weakest.
CORRIDOR_BANDS: tuple[tuple[float, str, str], ...] = (
    (0.0, "very weak",
     "supply line largely shut; rain on 56% of such days, and heavy rain "
     "almost never"),
    (9.1, "weak",
     "thin supply; rain on 70% of such days but amounts near the low end"),
    (13.2, "moderate",
     "ordinary monsoon delivery; rain on 86% of such days"),
    (16.0, "strong",
     "well-fed flow; rain on 98% of such days, averaging 20 mm at Kalyan"),
    (19.5, "very strong",
     "the heavy-rain signature: every such day rained, averaging 33 mm, and "
     "7 of the last three monsoons' 12 heaviest days sat in this band"),
)

# Same treatment for the upstream mid-level airmass (mean of 600 and 500 hPa).
#
#   midrh     <26.7  7.7 mm/day mean, rain on 60% of days,  0 of 12 heavy days
#             26.7  13.3                    73%             2
#             45.5  15.2                    83%             2
#             59.1  25.3                    95%             5
#             72.9  23.7                    98%             3
#
# The driest quintile produced NO heavy day in 316 days. That is the single
# cleanest negative signal available to this agent: when the upstream mid-level
# airmass is in its driest fifth, a heavy-rain forecast deserves suspicion
# however good the low-level moisture looks.
MIDRH_BANDS: tuple[tuple[float, str, str], ...] = (
    (0.0, "very dry",
     "entrainment will cap growth; no heavy-rain day occurred in this band in "
     "three monsoons"),
    (26.7, "dry",
     "still capping; amounts tend to verify below the model median"),
    (45.5, "middling",
     "neither helping nor hindering"),
    (59.1, "moist",
     "nothing aloft to cap growth; rain on 95% of such days"),
    (72.9, "very moist",
     "deep cloud unimpeded; rain on 98% of such days"),
)

BAND_PROVENANCE = (
    "Bands are quintiles of the 2024-26 monsoon distribution (316 days), "
    "labelled with the rainfall actually observed at Kalyan West in each."
)


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle initial bearing from point 1 to point 2, degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def _source_to_home_bearing() -> float:
    """Bearing from the centre of the dry-source region to Kalyan.

    Computed rather than hard-coded, so moving a sampling point cannot quietly
    leave a stale constant behind.
    """
    lat = sum(p[0] for p in DRY_POINTS) / len(DRY_POINTS)
    lon = sum(p[1] for p in DRY_POINTS) / len(DRY_POINTS)
    return _bearing(lat, lon, C.HOME.lat, C.HOME.lon)


def _band(value: float | None,
          bands: Sequence[tuple[float, str, str]]) -> tuple[str, str]:
    if value is None or not bands:
        return "unknown", ""
    label, note = bands[0][1], bands[0][2]
    for threshold, lab, nt in bands:
        if value >= threshold:
            label, note = lab, nt
    return label, note


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

@dataclass
class UpstreamDay:
    """The upstream state for one forecast day."""
    day: date
    core_speed: float | None = None
    corridor_speed: float | None = None
    corridor_dir: float | None = None
    mid_rh: float | None = None          # mean of 600 and 500 hPa upstream
    rh700: float | None = None
    rh600: float | None = None
    rh500: float | None = None
    dry_dir: float | None = None         # mid-level flow at the dry source

    @property
    def jet_label(self) -> str:
        return _band(self.corridor_speed, CORRIDOR_BANDS)[0]

    @property
    def jet_note(self) -> str:
        return _band(self.corridor_speed, CORRIDOR_BANDS)[1]

    @property
    def dry_label(self) -> str:
        return _band(self.mid_rh, MIDRH_BANDS)[0]

    @property
    def dry_note(self) -> str:
        return _band(self.mid_rh, MIDRH_BANDS)[1]

    @property
    def source_supports(self) -> bool | None:
        """Is there a Somali source behind the corridor pulse?

        A fast corridor with a slack source is a pulse already past its peak -
        there is nothing behind it to sustain the flow.
        """
        if self.core_speed is None or self.corridor_speed is None:
            return None
        return self.core_speed >= 0.6 * self.corridor_speed

    @property
    def advecting(self) -> bool | None:
        """Is the dry airmass being steered toward the MMR?

        `dry_dir` is a meteorological wind direction - the bearing the wind
        blows FROM. What matters here is where the air is going, so it is
        turned through 180 degrees before being compared with the bearing from
        the dry source region to Kalyan. Comparing the two directly would test
        for dry air travelling away from us and report the exact opposite.
        """
        off = self.advection_offset
        return None if off is None else off <= ADVECTION_TOLERANCE_DEG

    @property
    def advection_offset(self) -> float | None:
        """Degrees between where the mid-level air is going and where we are."""
        if self.dry_dir is None:
            return None
        travel = (self.dry_dir + 180.0) % 360.0
        target = _source_to_home_bearing()
        return abs((travel - target + 180) % 360 - 180)

    @property
    def advection_state(self) -> str:
        """Three-state, because a bare yes/no flickers near the boundary.

        Adjacent days sitting either side of a single tolerance angle would
        read as the flow reversing overnight when it has barely shifted, so
        the middle case is named rather than forced to one side.
        """
        off = self.advection_offset
        if off is None:
            return "unknown"
        if off <= 50.0:
            return "steered onto us"
        if off <= 85.0:
            return "glancing"
        return "steered away"


@dataclass
class UpstreamState:
    days: list[UpstreamDay] = field(default_factory=list)

    def for_day(self, day: date) -> UpstreamDay | None:
        for d in self.days:
            if d.day == day:
                return d
        return None


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch(*, days: int = 7, model: str = "ecmwf_ifs025",
          quiet: bool = True) -> UpstreamState | None:
    """Sample the upstream points and reduce to one record per forecast day."""
    params = {
        "latitude": ",".join(str(a) for a, _ in ALL_POINTS),
        "longitude": ",".join(str(b) for _, b in ALL_POINTS),
        "hourly": FIELDS,
        "models": model,
        "forecast_days": days,
        "timezone": C.TIMEZONE,
        "wind_speed_unit": "ms",
    }
    try:
        raw = _get_json(C.FORECAST_URL, params, timeout=90)
    except FetchError as exc:
        if not quiet:
            print(f"  ! upstream drivers unavailable: {exc}")
        return None

    if not isinstance(raw, list):
        raw = [raw]
    if len(raw) < len(ALL_POINTS):
        if not quiet:
            print(f"  ! upstream: expected {len(ALL_POINTS)} points, "
                  f"got {len(raw)}")
        return None

    n_core = len(CORE_POINTS)
    n_corr = len(CORRIDOR_POINTS)
    core_locs = raw[:n_core]
    corr_locs = raw[n_core:n_core + n_corr]
    dry_locs = raw[n_core + n_corr:]

    def by_day(locs, fieldname):
        """{date: [values]} pooled across the given points."""
        out: dict[date, list[float]] = {}
        for loc in locs:
            h = loc.get("hourly", {})
            times = h.get("time", [])
            series = h.get(fieldname) or []
            for i, t in enumerate(times):
                if i >= len(series) or series[i] is None:
                    continue
                d = datetime.fromisoformat(t).date()
                out.setdefault(d, []).append(float(series[i]))
        return out

    core_ws = by_day(core_locs, "wind_speed_850hPa")
    corr_ws = by_day(corr_locs, "wind_speed_850hPa")
    corr_wd = by_day(corr_locs, "wind_direction_850hPa")
    dry_dir = by_day(dry_locs, "wind_direction_600hPa")
    rh = {lvl: by_day(dry_locs, f"relative_humidity_{lvl}hPa")
          for lvl in (700, 600, 500)}

    def mean(vals):
        return sum(vals) / len(vals) if vals else None

    state = UpstreamState()
    for d in sorted(corr_ws):
        rh700, rh600, rh500 = (mean(rh[lvl].get(d, [])) for lvl in (700, 600, 500))
        mid = ([v for v in (rh600, rh500) if v is not None])
        state.days.append(UpstreamDay(
            day=d,
            core_speed=mean(core_ws.get(d, [])),
            corridor_speed=mean(corr_ws.get(d, [])),
            corridor_dir=_circular_mean(corr_wd.get(d, [])),
            rh700=rh700, rh600=rh600, rh500=rh500,
            mid_rh=(sum(mid) / len(mid)) if mid else None,
            dry_dir=_circular_mean(dry_dir.get(d, [])),
        ))
    return state


# --------------------------------------------------------------------------
# Reading it for a zone
# --------------------------------------------------------------------------

def zone_reading(u: UpstreamDay, zone: str) -> str:
    """What this upstream state means for one terrain zone.

    The wording differs by zone because the measured sensitivity differs by
    zone - the crest responds to the jet roughly twice as strongly as the rain
    shadow does.
    """
    sens = ZONE_SENSITIVITY.get(zone, 0.8)
    jet = u.jet_label
    dry = u.dry_label

    if zone == "ghat":
        where = "the crest"
    elif zone == "leeward":
        where = "the rain shadow"
    elif zone == "coastal":
        where = "the coast"
    else:
        where = "the transition belt"

    bits: list[str] = []
    if jet in ("strong", "very strong"):
        if zone == "ghat":
            bits.append(f"A {jet} corridor jet is the single most favourable "
                        f"thing that can happen to {where} — this is the "
                        "setup that pins rain against the slopes for days.")
        elif zone == "coastal":
            bits.append(f"A {jet} corridor jet drives a sustained onshore "
                        f"feed across {where} — long steady spells rather "
                        "than the short sharp showers of a slack pattern.")
        elif zone == "leeward":
            bits.append(f"The corridor jet is {jet}, but {where} gains least "
                        "from that: a stronger jet mostly means more is wrung "
                        "out before the air arrives.")
        else:
            bits.append(f"A {jet} corridor jet keeps the supply line to "
                        f"{where} well fed.")
    elif jet in ("weak", "very weak"):
        if zone == "leeward":
            bits.append(f"With the jet {jet}, {where} depends on whatever "
                        "convection can fire locally rather than on anything "
                        "arriving from the sea.")
        else:
            bits.append(f"A {jet} corridor jet starves {where} of its moisture "
                        "supply — expect the models' rain to verify at the low "
                        "end.")

    if dry in ("very dry", "dry"):
        if u.advection_state == "steered onto us":
            bits.append(f"Mid-levels upstream are {dry} and the flow is "
                        f"steering that air toward us. Cloud over {where} will "
                        "struggle to grow deep even where the low levels look "
                        "humid.")
        else:
            bits.append(f"Mid-levels upstream are {dry}, but the flow is not "
                        "currently steering it this way — worth watching "
                        "rather than acting on.")
    elif dry == "very moist":
        bits.append("Mid-levels upstream are unusually moist, so nothing is "
                    "there to cap growth — towers can go deep.")

    return " ".join(bits)


# Measured rank correlation of the corridor jet against each site's observed
# rainfall, 2024-26. Kept per site rather than per zone because the spread
# inside the ghat zone is itself informative: Malshej and Igatpuri, which face
# the flow most squarely, respond far more than Matheran does.
SITE_CORRIDOR_RHO: dict[str, float] = {
    "santacruz": 0.59, "kalyan_west": 0.55, "karjat": 0.56,
    "igatpuri": 0.65, "matheran": 0.59, "malshej": 0.68, "lonavala": 0.61,
    "pune": 0.29,
}

ZONE_ORDER = ("coastal", "transition", "ghat", "leeward")
ZONE_TITLE = {
    "coastal": "Coast (Mumbai, Vasai, Alibag)",
    "transition": "Transition belt (Kalyan, Thane, Dombivli, Karjat)",
    "ghat": "Ghats (Igatpuri, Malshej, Matheran, Lonavala)",
    "leeward": "Rain shadow (Pune side)",
}


def render_zones(state: UpstreamState | None, day: date | None = None) -> str:
    """The same upstream state, read for every part of the MMR.

    One jet and one airmass sit upstream of the whole region, but the terrain
    decides what each part does with them - so the state is reported once and
    interpreted four times.
    """
    if state is None or not state.days:
        return ""
    u = (state.for_day(day) if day else None) or state.days[0]

    out = ("The Somali jet and the mid-level airmass are regional — one state "
           "covers the whole MMR. What differs is how much each part of the "
           "region converts it into rain, and that was measured rather than "
           "assumed.\n\n")

    out += ("| Where | Jet sensitivity | What this week's upstream state means "
            "there |\n|---|---|---|\n")
    for zone in ZONE_ORDER:
        rho = ZONE_SENSITIVITY.get(zone, 0.8) * 0.63
        reading = zone_reading(u, zone) or "No strong upstream signal either way."
        out += f"| **{ZONE_TITLE[zone]}** | ρ ≈ {rho:+.2f} | {reading} |\n"

    strongest = max(SITE_CORRIDOR_RHO.items(), key=lambda kv: kv[1])
    weakest = min(SITE_CORRIDOR_RHO.items(), key=lambda kv: kv[1])
    sname = C.SITES_BY_KEY[strongest[0]].name
    wname = C.SITES_BY_KEY[weakest[0]].name
    out += (f"\n**{sname}** is the most jet-sensitive location in the region "
            f"(ρ {strongest[1]:+.2f}) and **{wname}** the least "
            f"({weakest[1]:+.2f}). When the corridor jet strengthens, expect "
            "the gap between crest and rain shadow to widen, not just "
            "everywhere to get wetter — a stronger jet wrings more out on the "
            "windward side and leaves less to cross.\n")
    return out


def render(state: UpstreamState | None, *, zone: str = "transition",
           day: date | None = None) -> str:
    """Markdown section for the bulletins."""
    if state is None or not state.days:
        return ""
    u = state.for_day(day) if day else state.days[0]
    if u is None:
        u = state.days[0]

    out = "## Upstream drivers — the Somali jet and mid-level dry air\n\n"
    out += ("These two sit upwind of everything else the bulletin measures. "
            "The jet decides how much moisture is being delivered; the "
            "mid-level airmass decides whether it can turn into deep cloud "
            "once it arrives.\n\n")

    out += "| Driver | Now | Reading |\n|---|---|---|\n"
    if u.corridor_speed is not None:
        out += (f"| **Somali jet** (mid-Arabian Sea corridor) | "
                f"{u.corridor_speed:.0f} m/s from "
                f"{compass(u.corridor_dir)} | **{u.jet_label}** — "
                f"{u.jet_note} |\n")
    if u.core_speed is not None:
        supports = u.source_supports
        src = ("source holding up behind it" if supports
               else "source already slackening behind it"
               if supports is not None else "—")
        out += (f"| Jet at its Somali source | {u.core_speed:.0f} m/s | "
                f"{src} |\n")
    if u.mid_rh is not None:
        adv = u.advection_state
        out += (f"| **Mid-level airmass** (600–500 hPa upstream) | "
                f"{u.mid_rh:.0f}% RH | **{u.dry_label}**, {adv} |\n")
    if u.rh700 is not None:
        out += (f"| Upstream 700 hPa | {u.rh700:.0f}% RH | "
                "the level the Guide calls for on cloud depth |\n")

    reading = zone_reading(u, zone)
    if reading:
        out += f"\n{reading}\n"

    sens = ZONE_SENSITIVITY.get(zone, 0.8)
    out += (f"\n> **How much weight this deserves here.** Measured against "
            f"three monsoons of rainfall, the corridor jet tracks "
            f"{zone} rainfall at a rank correlation of about "
            f"{sens * 0.63:+.2f}. That is a real signal and not a "
            "deterministic one: it tells you which way the models are likely "
            "to be wrong, not what the total will be. No rainfall number in "
            "this bulletin has been adjusted by it.\n")
    return out
