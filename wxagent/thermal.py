"""
Heatwave and cold-snap prediction, 7 days ahead.

Criteria are IMD's, taken verbatim from Handbook Ch.19 (heat) and Ch.18 (cold):

  HEAT - absolute threshold AND departure from that station's own normal.
    Plains  (Pune):   Tmax >= 40C, heatwave at +4.5 to +6.4, severe at +6.5.
                      Absolute 45C is a heatwave regardless of departure,
                      47C a severe heatwave regardless.
    Coastal (Mumbai): Tmax >= 37C, heatwave at +4.5 or more.
    Hilly:            Tmax >= 30C, heatwave at +4.5 or more.

  COLD - Handbook Ch.18: minimum <= 10C with a departure of at least -4.5C
    (or an absolute minimum <= 4C regardless), with a coastal-specific
    threshold of 15C.

Two things the Handbook insists on, both implemented here rather than left to
the reader:

  1. IMD declares a heatwave only when the criteria are met at two or more
     stations in a subdivision on TWO CONSECUTIVE DAYS, declared on the second.
     A single hot day is not a heatwave, and this module will not call one.

  2. Ch.19: "Don't mistake 'no heatwave was declared' for 'no heat risk' in a
     humid coastal city." Mumbai's dew points sit at 24-26C through the hot
     season, so the heat-index risk is real on days that never approach the
     dry-bulb threshold. Heat index is therefore computed alongside, and
     reported even when no heatwave criterion is met.

Normals are computed from ERA5 over recent years for the same calendar window.
They are NOT IMD's official 1991-2020 normals, and the departure figures below
inherit that difference - stated wherever a departure is reported.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Sequence

from . import config as C
from .sources import _get_json

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Years of ERA5 used to build the day-of-year normal.
NORMAL_YEARS = 10
NORMAL_WINDOW_DAYS = 7          # +/- around the target day-of-year

# IMD thresholds by station type (Handbook Ch.19 / Ch.18).
HEAT_THRESHOLD = {"coastal": 37.0, "plains": 40.0, "hilly": 30.0}
HEAT_DEPARTURE = 4.5
SEVERE_DEPARTURE = 6.5
HEAT_ABSOLUTE = 45.0            # plains only
SEVERE_ABSOLUTE = 47.0          # plains only

COLD_THRESHOLD = {"coastal": 15.0, "plains": 10.0, "hilly": 10.0}
COLD_DEPARTURE = -4.5
COLD_ABSOLUTE = 4.0

# Heat index caution bands (NWS Rothfusz), degrees C.
HEAT_INDEX_BANDS = (
    (27.0, 32.0, "Caution",
     "Fatigue possible with prolonged exposure or activity."),
    (32.0, 41.0, "Extreme caution",
     "Heat cramps and heat exhaustion possible; heat stroke with prolonged exertion."),
    (41.0, 54.0, "Danger",
     "Heat cramps and heat exhaustion likely; heat stroke probable with continued exposure."),
    (54.0, 99.0, "Extreme danger",
     "Heat stroke highly likely."),
)


def station_type(site) -> str:
    """
    IMD station category for the heat/cold thresholds.

    Read from the site's explicit `imd_type` rather than inferred from its
    rainfall zone - the two are different questions, and inferring one from the
    other mis-classified inland foothill stations like Karjat as coastal.
    """
    stype = getattr(site, "imd_type", None)
    if stype in HEAT_THRESHOLD:
        return stype
    # Fallback for any site defined without the field.
    if site.elevation_m >= 400:
        return "hilly" if site.zone == "ghat" else "plains"
    return "coastal" if site.zone == "coastal" else "plains"


# --------------------------------------------------------------------------
# Heat index
# --------------------------------------------------------------------------

def heat_index_c(temp_c: float | None, rh_pct: float | None) -> float | None:
    """
    NWS Rothfusz heat index ('feels like'), in Celsius.

    Below about 27C the regression is not meaningful, so the dry-bulb value is
    returned unchanged.
    """
    if temp_c is None or rh_pct is None:
        return None
    if temp_c < 26.7:
        return temp_c

    t = temp_c * 9 / 5 + 32
    r = rh_pct
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * r
          - 0.22475541 * t * r - 0.00683783 * t * t
          - 0.05481717 * r * r + 0.00122874 * t * t * r
          + 0.00085282 * t * r * r - 0.00000199 * t * t * r * r)

    if r < 13 and 80 <= t <= 112:
        hi -= ((13 - r) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
    elif r > 85 and 80 <= t <= 87:
        hi += ((r - 85) / 10) * ((87 - t) / 5)

    return (hi - 32) * 5 / 9


def heat_index_band(hi: float | None) -> tuple[str, str] | None:
    if hi is None:
        return None
    for lo, high, label, meaning in HEAT_INDEX_BANDS:
        if lo <= hi < high:
            return label, meaning
    return None


# --------------------------------------------------------------------------
# Normals
# --------------------------------------------------------------------------

@dataclass
class Normals:
    tmax: dict[tuple[int, int], float] = field(default_factory=dict)
    tmin: dict[tuple[int, int], float] = field(default_factory=dict)
    years: int = 0
    source: str = ""

    def max_for(self, d: date) -> float | None:
        return self.tmax.get((d.month, d.day))

    def min_for(self, d: date) -> float | None:
        return self.tmin.get((d.month, d.day))


def _cache_path(site, first: date, last: date, years: int):
    span = f"{first:%m%d}-{last:%m%d}"
    return C.CACHE_DIR / f"normals_{site.key}_{span}_{years}y.json"


def fetch_normals(site, target_days: Sequence[date], *,
                  years: int = NORMAL_YEARS,
                  quiet: bool = True) -> Normals:
    """
    Build day-of-year normals from ERA5 for the calendar window covering the
    target days, averaged over the last `years` years.

    Cached on disk. Climatological normals for a fixed calendar window do not
    change between runs, and recomputing them costs one archive request per
    year per site - forty requests a run across the MMR thermal sites, enough
    on its own to trip the free tier's rate limit.
    """
    if not target_days:
        return Normals()

    first, last = min(target_days), max(target_days)

    cache = _cache_path(site, first, last, years)
    if cache.exists():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            return Normals(
                tmax={tuple(int(x) for x in k.split("-")): v
                      for k, v in blob["tmax"].items()},
                tmin={tuple(int(x) for x in k.split("-")): v
                      for k, v in blob["tmin"].items()},
                years=blob.get("years", years),
                source=blob.get("source", ""),
            )
        except (json.JSONDecodeError, KeyError, OSError, ValueError):
            pass                                   # rebuild on any cache fault

    this_year = first.year
    pad = timedelta(days=NORMAL_WINDOW_DAYS)

    samples_max: dict[tuple[int, int], list[float]] = {}
    samples_min: dict[tuple[int, int], list[float]] = {}

    for back in range(1, years + 1):
        y = this_year - back
        try:
            start = date(y, first.month, first.day) - pad
            end = date(y, last.month, last.day) + pad
        except ValueError:                        # 29 Feb
            continue
        params = {
            "latitude": site.lat, "longitude": site.lon,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": C.TIMEZONE,
        }
        try:
            raw = _get_json(ARCHIVE_URL, params, timeout=90)
        except Exception as exc:                  # noqa: BLE001
            if not quiet:
                print(f"  ! normals {y} failed: {exc}")
            continue
        if isinstance(raw, list):
            raw = raw[0]
        daily = raw.get("daily", {})
        for t, mx, mn in zip(daily.get("time", []),
                             daily.get("temperature_2m_max", []),
                             daily.get("temperature_2m_min", [])):
            d = date.fromisoformat(t)
            key = (d.month, d.day)
            if mx is not None:
                samples_max.setdefault(key, []).append(mx)
            if mn is not None:
                samples_min.setdefault(key, []).append(mn)

    normals = Normals(
        tmax={k: statistics.mean(v) for k, v in samples_max.items() if v},
        tmin={k: statistics.mean(v) for k, v in samples_min.items() if v},
        years=years,
        source=f"ERA5, {years}-year mean over a "
               f"+/-{NORMAL_WINDOW_DAYS}-day window",
    )

    if normals.tmax:
        try:
            cache.write_text(json.dumps({
                "tmax": {f"{m}-{d}": v for (m, d), v in normals.tmax.items()},
                "tmin": {f"{m}-{d}": v for (m, d), v in normals.tmin.items()},
                "years": normals.years, "source": normals.source,
            }), encoding="utf-8")
        except OSError:
            pass                                   # cache is an optimisation
    return normals


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------

@dataclass
class ThermalDay:
    day: date
    tmax: float | None
    tmin: float | None
    normal_max: float | None
    normal_min: float | None
    rh_afternoon: float | None
    heat_index: float | None
    heat_flag: str = ""          # "" | "heatwave-criteria" | "severe-criteria"
    cold_flag: str = ""          # "" | "cold-criteria"
    note: str = ""

    @property
    def departure_max(self) -> float | None:
        if self.tmax is None or self.normal_max is None:
            return None
        return self.tmax - self.normal_max

    @property
    def departure_min(self) -> float | None:
        if self.tmin is None or self.normal_min is None:
            return None
        return self.tmin - self.normal_min


@dataclass
class ThermalOutlook:
    site_name: str
    station_type: str
    days: list[ThermalDay]
    normals: Normals
    heat_spell: list[date]       # consecutive days meeting heat criteria
    cold_spell: list[date]
    headline: str
    caveat: str


def _evaluate_day(d: ThermalDay, stype: str) -> None:
    thresh = HEAT_THRESHOLD[stype]
    dep = d.departure_max

    if d.tmax is not None:
        if stype == "plains" and d.tmax >= SEVERE_ABSOLUTE:
            d.heat_flag = "severe-criteria"
            d.note = (f"{d.tmax:.1f}°C meets IMD's absolute severe-heatwave "
                      f"threshold ({SEVERE_ABSOLUTE:g}°C) regardless of departure.")
        elif stype == "plains" and d.tmax >= HEAT_ABSOLUTE:
            d.heat_flag = "heatwave-criteria"
            d.note = (f"{d.tmax:.1f}°C meets IMD's absolute heatwave threshold "
                      f"({HEAT_ABSOLUTE:g}°C) regardless of departure.")
        elif d.tmax >= thresh and dep is not None and dep >= HEAT_DEPARTURE:
            severe = stype == "plains" and dep >= SEVERE_DEPARTURE
            d.heat_flag = "severe-criteria" if severe else "heatwave-criteria"
            d.note = (f"{d.tmax:.1f}°C clears the {stype} threshold "
                      f"({thresh:g}°C) and runs {dep:+.1f}°C against normal.")

    cthresh = COLD_THRESHOLD[stype]
    cdep = d.departure_min
    if d.tmin is not None:
        if d.tmin <= COLD_ABSOLUTE:
            d.cold_flag = "cold-criteria"
            d.note = (f"{d.tmin:.1f}°C meets IMD's absolute cold threshold "
                      f"({COLD_ABSOLUTE:g}°C) regardless of departure.")
        elif d.tmin <= cthresh and cdep is not None and cdep <= COLD_DEPARTURE:
            d.cold_flag = "cold-criteria"
            d.note = (f"{d.tmin:.1f}°C is under the {stype} threshold "
                      f"({cthresh:g}°C) and {cdep:+.1f}°C against normal.")


def _longest_run(days: Sequence[ThermalDay], attr: str) -> list[date]:
    best: list[date] = []
    run: list[date] = []
    for d in days:
        if getattr(d, attr):
            run.append(d.day)
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    return best


def analyse(site, pf, days: Sequence[date], *,
            primary_model: str = "ecmwf_ifs025",
            quiet: bool = True) -> ThermalOutlook:
    """
    Build the 7-day heat/cold outlook for one site from an existing
    PointForecast plus ERA5-derived normals.
    """
    from .diagnostics import window_indices

    stype = station_type(site)
    ms = pf.models.get(primary_model) or next(iter(pf.models.values()))
    normals = fetch_normals(site, days, quiet=quiet)

    out: list[ThermalDay] = []
    for d in days:
        idx = window_indices(pf.times, d, 0, 24)
        if not idx:
            continue
        temps = [ms.at("temperature_2m", i) for i in idx]
        temps = [t for t in temps if t is not None]
        if not temps:
            continue

        # Afternoon RH, for the heat index at the time of the daily maximum.
        aft = window_indices(pf.times, d, 12, 17)
        rhs = [ms.at("relative_humidity_2m", i) for i in aft]
        rhs = [r for r in rhs if r is not None]
        rh = sum(rhs) / len(rhs) if rhs else None

        td = ThermalDay(
            day=d, tmax=max(temps), tmin=min(temps),
            normal_max=normals.max_for(d), normal_min=normals.min_for(d),
            rh_afternoon=rh,
            heat_index=heat_index_c(max(temps), rh),
        )
        _evaluate_day(td, stype)
        out.append(td)

    heat_run = _longest_run(out, "heat_flag")
    cold_run = _longest_run(out, "cold_flag")

    # IMD declares on two consecutive qualifying days, not one.
    if len(heat_run) >= 2:
        headline = (f"Heatwave criteria met on {len(heat_run)} consecutive days "
                    f"from {heat_run[0]:%a %d %b}")
    elif len(heat_run) == 1:
        headline = (f"One day ({heat_run[0]:%a %d %b}) meets the temperature "
                    "criteria — below IMD's two-consecutive-day rule")
    elif len(cold_run) >= 2:
        headline = (f"Cold criteria met on {len(cold_run)} consecutive days "
                    f"from {cold_run[0]:%a %d %b}")
    elif len(cold_run) == 1:
        headline = (f"One unusually cold night ({cold_run[0]:%a %d %b}) — "
                    "below IMD's two-consecutive-day rule")
    else:
        hottest = max((d for d in out if d.tmax is not None),
                      key=lambda x: x.tmax, default=None)
        if hottest is not None:
            headline = (f"No heat or cold criteria met. Warmest day "
                        f"{hottest.day:%a %d %b} at {hottest.tmax:.0f}°C")
        else:
            headline = "No temperature data available."

    caveat = (
        f"Normals are {normals.source} — **not** IMD's official 1991–2020 "
        "normals, so every departure figure here carries that difference. IMD "
        "also requires the criteria at two or more stations in a subdivision "
        "on two consecutive days before declaring; a single point cannot make "
        "that call. This is a pattern-recognition aid, never a substitute for "
        "an official IMD heat warning."
    )

    return ThermalOutlook(
        site_name=site.name, station_type=stype, days=out, normals=normals,
        heat_spell=heat_run, cold_spell=cold_run,
        headline=headline, caveat=caveat,
    )


def render(to: ThermalOutlook) -> str:
    out = f"**{to.headline}.**\n\n"
    out += (f"| Day | Max | vs normal | Min | vs normal | Feels like | Flag |\n"
            f"|---|---|---|---|---|---|---|\n")
    for d in to.days:
        dep = f"{d.departure_max:+.1f}°C" if d.departure_max is not None else "--"
        dmin = f"{d.departure_min:+.1f}°C" if d.departure_min is not None else "--"
        hi = f"{d.heat_index:.0f}°C" if d.heat_index is not None else "--"
        band = heat_index_band(d.heat_index)
        flag = ""
        if d.heat_flag == "severe-criteria":
            flag = "🔴 severe-heat criteria"
        elif d.heat_flag == "heatwave-criteria":
            flag = "🟠 heatwave criteria"
        elif d.cold_flag:
            flag = "🔵 cold criteria"
        elif band and band[0] in ("Danger", "Extreme danger"):
            flag = f"🟠 {band[0].lower()} (heat index)"
        elif band and band[0] == "Extreme caution":
            flag = "🟡 extreme caution (heat index)"
        out += (f"| {d.day:%a %d %b} | {d.tmax:.0f}°C | {dep} | "
                f"{d.tmin:.0f}°C | {dmin} | {hi} | {flag} |\n")
    out += "\n"

    flagged = [d for d in to.days if d.note]
    for d in flagged:
        out += f"- **{d.day:%a %d %b}** — {d.note}\n"
    if flagged:
        out += "\n"

    worst = max((d for d in to.days if d.heat_index is not None),
                key=lambda x: x.heat_index, default=None)
    if worst is not None:
        band = heat_index_band(worst.heat_index)
        if band:
            out += (f"> **Humid-heat note.** Peak heat index "
                    f"{worst.heat_index:.0f}°C on {worst.day:%a %d %b} — "
                    f"**{band[0]}**: {band[1]} Handbook Ch.19: in a humid "
                    "coastal city the dry-bulb IMD criteria and the actual heat "
                    "risk are measuring different things — do not read 'no "
                    "heatwave declared' as 'no heat risk'.\n\n")

    out += f"> {to.caveat}\n"
    return out


# --------------------------------------------------------------------------
# The coldest places on the list
# --------------------------------------------------------------------------
#
# Ranked on the primary model's night minimum, the same model the rest of this
# module reads, with the other models' range carried beside each place rather
# than averaged in. That range is not decoration: on 20 Nov 2026 Colaba ran
# from 13.2C in one model to 24.0C in another - a grid cell sitting over land
# - and Panvel from 13.3 to 20.0, so a lowland ranking can turn on which model
# you ask.
#
# Two things about the answer are predictable and said rather than hidden.
# The top of the list is altitude: Matheran, the ghat stations and the Pune
# plateau take it every winter night, which is true and worth knowing for
# anyone going up, and tells someone in Dombivli nothing. So the coldest
# place in the lowland belt is reported as well, because the inland Ulhas
# valley runs several degrees colder than the coast and that is the cold most
# readers actually wake up in. And places a fraction of a degree apart are
# called level, not ranked: 0.1C is inside the noise of any one model.
#
# A night is 18:00 to 09:00 and is named by the dawn it ends in. The first
# version took the minimum over each calendar date, and the cloud build at
# 13:18 on 21 Nov 2026 ranked all four places on THAT morning - a cold that
# had already come and gone - and labelled it "Sat 21 Nov", which a reader at
# lunchtime takes to mean tonight. A night whose dawn is already past at issue
# time is dropped; the one still under way at a 06:15 build is kept.

COLD_TIE_C = 0.3
LOWLAND_ZONES = ("coastal", "transition")
NIGHT_START_H = 18
NIGHT_END_H = 9


def coldest_places(forecasts: dict, days: Sequence[date], sites_by_key: dict,
                   *, primary: str = "ecmwf_ifs025", n: int = 3,
                   now: datetime | None = None) -> dict:
    """Rank the list's places by their coldest night in the window.

    `days` are the dawns: the night into each date, 18:00 the evening before
    to 09:00. With `now`, nights already over by then are skipped.

    Returns {"top": [...n...], "level": [...], "lowland": {...} | None}, each
    entry a dict of place, zone, tmin (primary model), lo/hi (all models),
    day (label) and elevation in metres.
    """
    nights = []
    for d in days:
        end = datetime(d.year, d.month, d.day, NIGHT_END_H)
        if now is not None and now >= end:
            continue
        nights.append((d, end - timedelta(hours=24 - NIGHT_START_H
                                          + NIGHT_END_H), end))

    def nightly_min(pf, ms):
        t = ms.get("temperature_2m")
        stamps = []
        for ts in pf.times:
            try:
                stamps.append(datetime.fromisoformat(ts))
            except ValueError:
                stamps.append(None)
        best = None
        for d, start, end in nights:
            vals = [t[i] for i, st in enumerate(stamps)
                    if st is not None and i < len(t) and t[i] is not None
                    and start <= st <= end]
            if vals and (best is None or min(vals) < best[0]):
                best = (min(vals), d)
        return best

    rows = []
    for key, pf in (forecasts or {}).items():
        site = sites_by_key.get(key)
        if site is None or not getattr(pf, "models", None):
            continue
        per = {}
        for mk, ms in pf.models.items():
            b = nightly_min(pf, ms)
            if b is not None:
                per[mk] = b
        if not per:
            continue
        main = per.get(primary) or min(per.values())
        vals = [v for v, _ in per.values()]
        rows.append({
            "place": site.name, "zone": getattr(site, "zone", ""),
            "tmin": round(main[0], 1),
            "lo": round(min(vals), 1), "hi": round(max(vals), 1),
            "day": f"night into {main[1]:%a %d %b}",
            "elev": (round(pf.elevation_m)
                     if getattr(pf, "elevation_m", None) is not None else None),
        })
    if not rows:
        return {"top": [], "level": [], "lowland": None}

    rows.sort(key=lambda r: r["tmin"])
    top = rows[:n]
    # Places just outside the top that are level with the last one in it.
    level = [r for r in rows[n:]
             if r["tmin"] - top[-1]["tmin"] <= COLD_TIE_C]
    lowland = None
    if not any(r["zone"] in LOWLAND_ZONES for r in top):
        lowland = next((r for r in rows if r["zone"] in LOWLAND_ZONES), None)
    return {"top": top, "level": level, "lowland": lowland}


def render_coldest(c: dict) -> str:
    """Markdown block for the bulletins."""
    top = c.get("top") or []
    if not top:
        return ""
    out = "## Coldest places on the list\n\n"
    out += "| # | Place | When | Low | Across models |\n"
    out += "|---|---|---|---|---|\n"
    for i, r in enumerate(top, 1):
        elev = f" ({r['elev']} m)" if r.get("elev") and r["elev"] >= 150 else ""
        out += (f"| {i} | **{r['place']}**{elev} | {r['day']} | "
                f"**{r['tmin']:.1f}°C** | {r['lo']:.1f}–{r['hi']:.1f}°C |\n")
    out += "\n"
    if c.get("level"):
        lv = [r["place"] for r in c["level"]]
        names = (lv[0] if len(lv) == 1
                 else ", ".join(lv[:-1]) + " and " + lv[-1])
        out += (f"{names} {'is' if len(c['level']) == 1 else 'are'} level "
                f"with #{len(top)} — within {COLD_TIE_C:g}°C, which is inside "
                "the noise of any one model.\n\n")
    lw = c.get("lowland")
    if lw:
        out += (f"**In the lowland belt:** {lw['place']} is coldest, "
                f"{lw['tmin']:.1f}°C on the {lw['day']} (models "
                f"{lw['lo']:.1f}–{lw['hi']:.1f}°C). The top of the list is "
                "altitude; this is the cold most people wake up in. A 25 km "
                "model smooths the valley floor into warmer surroundings, so "
                "the coldest pockets run lower still.\n\n")
    return out
