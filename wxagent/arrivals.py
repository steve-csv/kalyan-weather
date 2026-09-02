"""
Per-region arrival times, from a three-model steering wind.

WHAT THIS ADDS
--------------
The five-question scan already looks upstream, but it does so from ONE place
(Kalyan) using ONE model's wind (ECMWF). That answers "when does it reach me".
It does not answer "when does it reach Malad, and is that before or after
Panvel", which is the question a region-wise alert has to answer.

So this samples upstream of EVERY belt separately, and it takes the steering
wind from ECMWF, GFS and ICON together.

WHY THREE MODELS RATHER THAN ONE
--------------------------------
Arrival time is distance divided by speed, so an error in the wind is an error
in the timing, proportionally. One model gives a single number with no way to
tell whether it is trustworthy. Three give a consensus AND a spread, and the
spread is the honest width of the answer: when the models agree on the wind to
within a few km/h the arrival window is tight, and when they disagree it is
wide - which is exactly when a single-model figure would have been most
confidently wrong.

The models are never averaged into one wind and then quoted as fact. The
consensus is used for the central estimate and the disagreement is reported
as the range, which is the treatment the rest of this agent gives model
spread everywhere else.

WHAT THIS IS NOT
----------------
It is not radar. The rain areas it finds are the model's analysis, not echoes,
because IMD publishes no georeferencing for its radar images - there is no
documented map extent or projection in the file, so pixel-to-place would be
guesswork, and a guessed geometry would put arrival alerts on the wrong
suburbs. Inside an hour the radar loop still outranks this.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime

from . import config as C
from .belts import BELTS, TRACE_MM_H
from .nowcast import _destination
from .sources import FetchError, _get_json

# How far upstream to look, in km. Beyond ~120 km an echo will have evolved
# past recognition before it arrives, and the arrival time stops meaning much.
RANGES_KM = (15.0, 30.0, 45.0, 60.0, 80.0, 100.0, 120.0)

# Wet enough to be worth calling an approaching area.
WET_MM_H = TRACE_MM_H


@dataclass
class Steering:
    speed_kmh: float                 # consensus
    from_deg: float                  # consensus, direction wind blows FROM
    per_model: dict[str, tuple[float, float]]
    spread_kmh: float

    @property
    def toward_deg(self) -> float:
        return (self.from_deg + 180.0) % 360.0

    @property
    def agreement(self) -> str:
        if self.spread_kmh <= 6:
            return "tight"
        if self.spread_kmh <= 14:
            return "moderate"
        return "wide"


@dataclass
class Arrival:
    belt_key: str
    belt_name: str
    distance_km: float | None
    mm_h: float
    eta_min: int | None
    eta_lo_min: int | None
    eta_hi_min: int | None
    raining_now: bool

    @property
    def sentence(self) -> str:
        if self.raining_now:
            return "**Already raining here.**"
        if self.distance_km is None or self.eta_min is None:
            return "Nothing upstream within 120 km."
        window = ""
        if (self.eta_lo_min is not None and self.eta_hi_min is not None
                and self.eta_hi_min - self.eta_lo_min >= 10):
            window = (f" (models put it between {self.eta_lo_min} and "
                      f"{self.eta_hi_min} minutes)")
        return (f"Rain **{self.distance_km:.0f} km** upstream, arriving in "
                f"roughly **{self.eta_min} minutes**{window}.")


def _circ_mean(degs: list[float]) -> float | None:
    if not degs:
        return None
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    if x == 0 and y == 0:
        return None
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def steering(site=None, *, now: datetime | None = None,
             quiet: bool = True) -> Steering | None:
    """850 hPa steering wind from all three models at once."""
    site = site or C.HOME
    now = now or datetime.now()
    ids = [m.key for m in C.MODELS]
    try:
        raw = _get_json(C.FORECAST_URL, {
            "latitude": site.lat, "longitude": site.lon,
            "hourly": "wind_speed_850hPa,wind_direction_850hPa",
            "models": ",".join(ids), "forecast_days": 1, "past_days": 1,
            "timezone": C.TIMEZONE, "wind_speed_unit": "kmh",
        }, timeout=75)
    except FetchError as exc:
        if not quiet:
            print(f"  ! steering wind unavailable: {exc}")
        return None
    if isinstance(raw, list):
        raw = raw[0]
    h = raw.get("hourly", {})
    times = h.get("time", [])
    idx = [i for i, t in enumerate(times)
           if abs((datetime.fromisoformat(t) - now).total_seconds()) < 5400]
    if not idx:
        return None

    per: dict[str, tuple[float, float]] = {}
    for mid in ids:
        sp = h.get(f"wind_speed_850hPa_{mid}") or h.get("wind_speed_850hPa")
        dr = h.get(f"wind_direction_850hPa_{mid}") or h.get("wind_direction_850hPa")
        if not sp or not dr:
            continue
        s = [sp[i] for i in idx if i < len(sp) and sp[i] is not None]
        d = [dr[i] for i in idx if i < len(dr) and dr[i] is not None]
        dm = _circ_mean(d)
        if s and dm is not None:
            per[mid] = (sum(s) / len(s), dm)
    if not per:
        return None

    speeds = [v[0] for v in per.values()]
    cons_dir = _circ_mean([v[1] for v in per.values()])
    if cons_dir is None:
        return None
    return Steering(
        speed_kmh=statistics.median(speeds),
        from_deg=cons_dir,
        per_model=per,
        spread_kmh=max(speeds) - min(speeds),
    )


def arrivals(st: Steering | None, *, quiet: bool = True) -> list[Arrival]:
    """Where the rain is upstream of each belt, and when it gets there."""
    if st is None or st.speed_kmh < 3:
        return []

    # One sample line per belt, aimed into the wind from that belt's own
    # position - the whole point is that Malad and Panvel have different
    # upstream, so a single fan drawn from Kalyan cannot answer for both.
    pts: list[tuple[float, float]] = []
    meta: list[tuple[str, float]] = []
    for belt in BELTS:
        la0 = sum(p[1] for p in belt.points) / len(belt.points)
        lo0 = sum(p[2] for p in belt.points) / len(belt.points)
        pts.append((la0, lo0))
        meta.append((belt.key, 0.0))
        for rng in RANGES_KM:
            la, lo = _destination(la0, lo0, st.from_deg, rng)
            pts.append((la, lo))
            meta.append((belt.key, rng))

    try:
        raw = _get_json(C.FORECAST_URL, {
            "latitude": ",".join(f"{p[0]:.4f}" for p in pts),
            "longitude": ",".join(f"{p[1]:.4f}" for p in pts),
            "hourly": "precipitation", "models": "ecmwf_ifs025",
            "forecast_days": 1, "past_hours": 1,
            "timezone": C.TIMEZONE,
        }, timeout=90)
    except FetchError as exc:
        if not quiet:
            print(f"  ! upstream sampling failed: {exc}")
        return []
    if not isinstance(raw, list):
        raw = [raw]
    if len(raw) < len(pts):
        return []

    now_mm: dict[tuple[str, float], float] = {}
    for (key, rng), loc in zip(meta, raw):
        vals = (loc.get("hourly", {}).get("precipitation") or [])
        v = next((x for x in vals if x is not None), 0.0)
        now_mm[(key, rng)] = float(v)

    # Fast and slow ends of the model spread, for the arrival window.
    speeds = [v[0] for v in st.per_model.values()] or [st.speed_kmh]
    fast, slow = max(speeds), min(speeds)

    out: list[Arrival] = []
    for belt in BELTS:
        here = now_mm.get((belt.key, 0.0), 0.0)
        if here >= WET_MM_H:
            out.append(Arrival(belt.key, belt.name, 0.0, here,
                               0, 0, 0, raining_now=True))
            continue
        hit = next((r for r in RANGES_KM
                    if now_mm.get((belt.key, r), 0.0) >= WET_MM_H), None)
        if hit is None:
            out.append(Arrival(belt.key, belt.name, None, 0.0,
                               None, None, None, raining_now=False))
            continue
        mm = now_mm[(belt.key, hit)]
        out.append(Arrival(
            belt.key, belt.name, hit, mm,
            eta_min=int(round(hit / st.speed_kmh * 60)),
            eta_lo_min=int(round(hit / fast * 60)),
            eta_hi_min=int(round(hit / slow * 60)),
            raining_now=False,
        ))
    return out


def render(st: Steering | None, arr: list[Arrival]) -> str:
    """Markdown for the bulletin."""
    if st is None or not arr:
        return ""
    from .diagnostics import compass

    names = {m.key: m.label for m in C.MODELS}
    out = "**When the rain reaches each area**\n\n"
    out += (f"The wind carrying it is from **{compass(st.from_deg)}** at about "
            f"**{st.speed_kmh:.0f} km/h**, so rain moves toward "
            f"**{compass(st.toward_deg)}**. ")
    per = " · ".join(f"{names.get(k, k)} {v[0]:.0f}"
                     for k, v in st.per_model.items())
    if st.agreement == "tight":
        out += (f"All three models agree closely on that ({per} km/h), so the "
                "timings below are as firm as this method gets.\n\n")
    elif st.agreement == "moderate":
        out += (f"The models differ a little ({per} km/h), so treat the "
                "timings as give-or-take.\n\n")
    else:
        out += (f"The models disagree noticeably ({per} km/h), so the timings "
                "below carry a wide margin — that disagreement is itself the "
                "warning.\n\n")

    out += "| Region | Rain upstream | Expected here |\n|---|---|---|\n"
    for a in arr:
        if a.raining_now:
            out += f"| **{a.belt_name}** | — | Already raining |\n"
        elif a.distance_km is None:
            out += f"| **{a.belt_name}** | none within 120 km | — |\n"
        else:
            win = ""
            if (a.eta_hi_min is not None and a.eta_lo_min is not None
                    and a.eta_hi_min - a.eta_lo_min >= 10):
                win = f" ({a.eta_lo_min}–{a.eta_hi_min})"
            out += (f"| **{a.belt_name}** | {a.distance_km:.0f} km, "
                    f"{a.mm_h:.1f} mm/hr | ~{a.eta_min} min{win} |\n")

    out += ("\n> Arrival times are distance divided by the steering wind, so "
            "they assume the rain keeps moving and keeps going. Cells grow and "
            "die, and one that dies on the way never arrives. The rain areas "
            "come from the model analysis rather than radar, because IMD's "
            "radar images carry no map extent or projection, and a guessed "
            "geometry would put these alerts on the wrong suburbs. **Inside an "
            "hour the radar loop above beats this table.**\n")
    return out
