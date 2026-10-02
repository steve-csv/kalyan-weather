"""
Surface wind as something forecast, not just something used.

WHY THIS EXISTS
---------------
Wind was in this repository from the start, but only ever as an INPUT: the
850 hPa flow for moisture transport and the terrain-normal component for
orographic lift. Nobody reading the page was told what the wind would actually
do at ground level, which is the version that decides whether the balcony
drying works, whether the ferry runs, whether a crane stops, and whether the
afternoon is bearable.

THE SEA BREEZE IS THE LOCAL STORY
---------------------------------
On this coast the daily wind cycle matters more than the synoptic one for most
of the year. The land heats faster than the sea, pressure falls over the land,
and air floods in off the water from late morning. It is why a Mumbai
afternoon in April is survivable and why the same afternoon inland at Kalyan
is not, for an hour or two longer: the front takes time to travel inland. By
the time it reaches Kalyan, 30-odd km from the shore, it is usually mid to
late afternoon, and it arrives as a noticeable shift rather than a gradual
freshening - direction swings round to the west, speed picks up, and the
humidity jumps.

At night the cycle reverses, weakly. The land cools below the sea, the flow
drains back offshore, and because that land breeze is light and stable it is
also what lets fog and haze settle - which is why wind.py and fog.py keep
pointing at each other.

WHAT 'FEELS LIKE' MEANS HERE, AND WHAT IT LEAVES OUT
----------------------------------------------------
Feels-like on this page is the NWS heat index that thermal.py already
computes: a function of temperature and humidity only. In a city whose dew
point sits in the mid-20s for half the year that is the right primary measure,
and it is the one IMD frames its own heat-index product around. But it is a
SHADE value and it assumes light wind, so it does two things wrong in opposite
directions - it ignores the sun, which adds several degrees to anyone standing
in it, and it ignores the breeze, which takes some back.

Open-Meteo's own apparent temperature, which does model sun and wind, was
compared against it over a full day here: the two sat within about a degree of
each other for most hours and never diverged by two. Publishing both would
have cost a reader more confusion than it bought them in accuracy, so the page
carries the heat index and this module reports the breeze separately, in m/s,
where it can be judged rather than buried in a single number.

The gap between feels-like and the thermometer is reported explicitly, because
that gap IS the humidity, and it is the part people find surprising: 34C at
50% humidity feels like 40C, and no amount of looking at the forecast
temperature tells you that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from . import config as C
from .diagnostics import compass
from .thermal import heat_index_band, heat_index_c

# Beaufort, trimmed to the range this coast actually sees, in m/s.
BEAUFORT = (
    (0.5, "calm", "Smoke rises straight up. Nothing moves."),
    (1.6, "light air", "Barely perceptible; smoke drifts."),
    (3.4, "light breeze", "Felt on the face, leaves rustle."),
    (5.5, "gentle breeze", "Leaves and small twigs in constant motion."),
    (8.0, "moderate breeze", "Raises dust and loose paper; small branches move."),
    (10.8, "fresh breeze", "Small trees sway; whitecaps on open water."),
    (13.9, "strong breeze", "Large branches move; umbrellas hard to use."),
    (17.2, "near gale", "Whole trees in motion; walking into it is work."),
    (99.0, "gale", "Twigs break off trees; structural damage possible."),
)

# IMD's marine and land thresholds, converted from the kmph the bulletins use.
# 35 kmph is where the fishermen's advisory starts; 45 is "squally"; 55 is
# where IMD's own wording turns to damage.
STRONG_MS = 9.7          # 35 kmph
SQUALLY_MS = 12.5        # 45 kmph
DAMAGING_MS = 15.3       # 55 kmph

# Onshore and offshore arcs, from the coast's orientation. The Ghats' upslope
# normal is a wind FROM 260 deg (config.GHAT_UPSLOPE_NORMAL_DEG), so the sea
# lies broadly west and the land breeze drains from the eastern half.
ONSHORE = (200.0, 320.0)
OFFSHORE = (20.0, 160.0)

# Sea-breeze detection. Morning is sampled before the front can plausibly have
# arrived this far inland; afternoon after.
MORNING_HRS = (6, 11)
AFTERNOON_HRS = (13, 19)
SEABREEZE_MIN_SWING = 45.0     # degrees of veer
SEABREEZE_MIN_FRESHEN = 1.0    # m/s

# Feels-like reporting. The gap is the humidity penalty; below this it is not
# worth a sentence.
FEELS_GAP_NOTE = 3.0
FEELS_ALERT = 40.0             # watch
FEELS_DANGER = 45.0            # IMD's own danger band starts at 41


@dataclass
class WindDay:
    day: date
    mean_ms: float | None = None
    max_ms: float | None = None
    max_gust: float | None = None
    gust_spread: tuple[float, float] | None = None   # across models
    prevailing: float | None = None
    beaufort: str = ""
    beaufort_note: str = ""
    sea_breeze_hour: int | None = None
    morning_dir: float | None = None
    afternoon_dir: float | None = None
    feels_max: float | None = None
    feels_at: int | None = None
    temp_at_feels: float | None = None
    band: tuple[str, str] | None = None
    sentence: str = ""

    @property
    def feels_gap(self) -> float | None:
        if self.feels_max is None or self.temp_at_feels is None:
            return None
        return self.feels_max - self.temp_at_feels

    @property
    def strong(self) -> bool:
        return (self.max_gust or 0.0) >= STRONG_MS


def _arc(deg: float | None, arc: tuple[float, float]) -> bool:
    return deg is not None and arc[0] <= deg <= arc[1]


def _vector_mean(dirs: Sequence[float | None],
                 speeds: Sequence[float | None]) -> float | None:
    """Speed-weighted mean direction. Directions only average as vectors -
    the arithmetic mean of 350 and 10 is 180, the exact opposite wind."""
    import math
    x = y = 0.0
    for d, s in zip(dirs, speeds):
        if d is None or s is None:
            continue
        r = math.radians(d)
        x += s * math.sin(r)
        y += s * math.cos(r)
    if x == 0.0 and y == 0.0:
        return None
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _beaufort(ms: float | None) -> tuple[str, str]:
    if ms is None:
        return ("", "")
    for limit, name, note in BEAUFORT:
        if ms < limit:
            return name, note
    return BEAUFORT[-1][1], BEAUFORT[-1][2]


def _hours(times: Sequence[str], day: date,
           span: tuple[int, int]) -> list[int]:
    out = []
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if dt.date() == day and span[0] <= dt.hour < span[1]:
            out.append(i)
    return out


def _veer(frm: float, to: float) -> float:
    """Smallest signed angle from one bearing to another."""
    d = (to - frm + 540.0) % 360.0 - 180.0
    return d


def assess_day(pf, day: date, primary: str = "ecmwf_ifs025") -> WindDay:
    out = WindDay(day=day)
    ms = pf.models.get(primary) or next(iter(pf.models.values()), None)
    if ms is None:
        return out

    allday = _hours(pf.times, day, (0, 24))
    if not allday:
        return out

    spd = ms.get("wind_speed_10m")
    dirs = ms.get("wind_direction_10m")
    gust = ms.get("wind_gusts_10m")
    temp = ms.get("temperature_2m")
    rh = ms.get("relative_humidity_2m")

    take = lambda src, idx: [src[i] for i in idx
                             if i < len(src) and src[i] is not None]

    speeds = take(spd, allday)
    gusts = take(gust, allday)
    out.mean_ms = sum(speeds) / len(speeds) if speeds else None
    out.max_ms = max(speeds) if speeds else None
    out.max_gust = max(gusts) if gusts else None
    out.prevailing = _vector_mean([dirs[i] if i < len(dirs) else None
                                   for i in allday],
                                  [spd[i] if i < len(spd) else None
                                   for i in allday])
    out.beaufort, out.beaufort_note = _beaufort(out.max_ms)

    # Gusts across models: the spread is the honest uncertainty on the one
    # number that decides whether something gets tied down.
    peaks = []
    for m in pf.models.values():
        g = take(m.get("wind_gusts_10m"), allday)
        if g:
            peaks.append(max(g))
    if len(peaks) > 1:
        out.gust_spread = (min(peaks), max(peaks))

    # ---- sea breeze ------------------------------------------------------
    morn = _hours(pf.times, day, MORNING_HRS)
    aft = _hours(pf.times, day, AFTERNOON_HRS)
    out.morning_dir = _vector_mean([dirs[i] if i < len(dirs) else None
                                    for i in morn],
                                   [spd[i] if i < len(spd) else None
                                    for i in morn])
    out.afternoon_dir = _vector_mean([dirs[i] if i < len(dirs) else None
                                      for i in aft],
                                     [spd[i] if i < len(spd) else None
                                      for i in aft])
    mspd = take(spd, morn)
    aspd = take(spd, aft)
    if (out.morning_dir is not None and out.afternoon_dir is not None
            and mspd and aspd):
        veer = _veer(out.morning_dir, out.afternoon_dir)
        freshens = (sum(aspd) / len(aspd)) - (sum(mspd) / len(mspd))
        if (veer >= SEABREEZE_MIN_SWING
                and freshens >= SEABREEZE_MIN_FRESHEN
                and _arc(out.afternoon_dir, ONSHORE)):
            # The hour it arrives: first afternoon hour already onshore and
            # above the morning's mean.
            base = sum(mspd) / len(mspd)
            for i in aft:
                d = dirs[i] if i < len(dirs) else None
                s = spd[i] if i < len(spd) else None
                if _arc(d, ONSHORE) and s is not None and s > base:
                    out.sea_breeze_hour = datetime.fromisoformat(
                        pf.times[i]).hour
                    break

    # ---- feels like ------------------------------------------------------
    best = None
    for i in allday:
        t = temp[i] if i < len(temp) else None
        r = rh[i] if i < len(rh) else None
        hi = heat_index_c(t, r)
        if hi is None:
            continue
        if best is None or hi > best[0]:
            best = (hi, datetime.fromisoformat(pf.times[i]).hour, t)
    if best:
        out.feels_max, out.feels_at, out.temp_at_feels = best
        out.band = heat_index_band(out.feels_max)

    out.sentence = _sentence(out)
    return out


def _sentence(w: WindDay) -> str:
    bits: list[str] = []
    if w.max_ms is not None:
        bits.append(
            f"{w.beaufort.capitalize()} — peaking near {w.max_ms:.0f} m/s "
            f"({w.max_ms * 3.6:.0f} kmph)"
            + (f" from the {compass(w.prevailing)}" if w.prevailing else "")
            + ".")
    if w.sea_breeze_hour is not None:
        bits.append(
            f"The sea breeze reaches Kalyan around "
            f"{w.sea_breeze_hour:02d}:00, swinging the wind round to the "
            f"{compass(w.afternoon_dir)} and freshening it — the afternoon "
            "cools and the humidity climbs at the same time.")
    elif w.prevailing is not None and not _arc(w.prevailing, ONSHORE):
        # Anything that is not onshore is a day without the sea's help. Testing
        # a separate offshore arc missed a NNE flow sitting a couple of degrees
        # outside it, which is the same wind for every purpose a reader has.
        bits.append(
            f"The flow stays offshore from the {compass(w.prevailing)} — no "
            "sea breeze to take the edge off, and the air keeps the land's "
            "heat.")
    if w.max_gust is not None and w.max_gust >= STRONG_MS:
        word = ("damaging" if w.max_gust >= DAMAGING_MS
                else "squally" if w.max_gust >= SQUALLY_MS else "strong")
        bits.append(
            f"Gusts to {w.max_gust:.0f} m/s ({w.max_gust * 3.6:.0f} kmph) — "
            f"IMD would call that {word}."
            + (f" The models bracket the peak between "
               f"{w.gust_spread[0] * 3.6:.0f} and "
               f"{w.gust_spread[1] * 3.6:.0f} kmph."
               if w.gust_spread and
               (w.gust_spread[1] - w.gust_spread[0]) >= 2.0 else ""))
    gap = w.feels_gap
    if w.feels_max is not None and gap is not None and gap >= FEELS_GAP_NOTE:
        bits.append(
            f"Feels like {w.feels_max:.0f}°C around "
            f"{(w.feels_at or 14):02d}:00 against a real "
            f"{w.temp_at_feels:.0f}°C — the humidity is adding "
            f"{gap:.0f}°C."
            + (f" {w.band[0]}: {w.band[1]}" if w.band else ""))
    return " ".join(bits)


def outlook(pf, days: Sequence[date],
            primary: str = "ecmwf_ifs025") -> list[WindDay]:
    return [assess_day(pf, d, primary) for d in days]


def render(days: Sequence[WindDay]) -> str:
    rows = [w for w in days if w.sentence]
    if not rows:
        return ""
    out = "## Wind and how it will feel\n\n"
    out += ("| Day | Wind | Gust | From | Feels like |\n"
            "|---|---|---|---|---|\n")
    for w in rows:
        out += (f"| {w.day:%a %d %b} "
                f"| {('%.0f' % w.max_ms) + ' m/s' if w.max_ms is not None else '—'} "
                f"| {('%.0f' % (w.max_gust * 3.6)) + ' kmph' if w.max_gust else '—'} "
                f"| {compass(w.prevailing)} "
                f"| {('%.0f' % w.feels_max) + '°C' if w.feels_max is not None else '—'} "
                f"|\n")
    out += "\n"
    for w in rows[:3]:
        out += f"**{w.day:%A %d %b}** — {w.sentence}\n\n"
    out += ("> Feels-like is the NWS heat index: temperature and humidity "
            "only. It is a shade value — standing in the sun is worse, and "
            "the breeze column is the relief it does not count.\n\n")
    return out
