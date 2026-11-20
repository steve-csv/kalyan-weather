"""
Air quality - the winter story a rain page never told.

WHY THIS EXISTS
---------------
From November to February the weather that actually harms people in the MMR is
not rain and rarely cold. It is the air. Calm nights, a shallow inversion and a
land breeze draining off the plateau trap traffic, construction and industrial
emissions in the bottom few hundred metres, and Kalyan-Dombivli-Bhiwandi sits
in exactly the basin where that happens. This repository forecast rain to the
millimetre and said nothing about any of it until 20 Nov 2026.

THE INDEX: INDIA'S, NOT AMERICA'S
---------------------------------
The data source also publishes a US AQI, and it is the wrong number to show a
reader in Kalyan: different breakpoints, different averaging periods, and
different category names from the ones in Indian news and on CPCB's own boards.
So this computes CPCB's National Air Quality Index (2014) from the pollutant
concentrations directly:

  * PM2.5, PM10, NO2, SO2 on 24-hour means; O3 and CO on the day's worst
    8-hour running mean, as CPCB specifies.
  * Each pollutant gets a sub-index by linear interpolation inside its band;
    the AQI is the worst sub-index, and that pollutant is named as the cause.
  * CPCB requires at least three pollutants, one of them PM2.5 or PM10, before
    an AQI may be stated. Enforced, not assumed.

WHAT THIS CAN AND CANNOT SEE - STATED BECAUSE IT MATTERS MORE HERE THAN ANYWHERE
--------------------------------------------------------------------------------
The concentrations come from CAMS, the European global atmospheric-composition
model, via Open-Meteo, on a grid of roughly 40 km. Three consequences:

  1. It cannot see a junction, a construction site, a crematorium or a garbage
     fire. City air is made of exactly those, so the model describes the
     background the city sits in, not the street a reader stands on.
  2. Global aerosol models are known to run low over South Asian cities, and
     coarse dust especially. On the first day this was run PM10 came out almost
     equal to PM2.5 - physically implausible in a city where road and building
     dust should push PM10 well above it. Treat a model "Moderately polluted"
     as a floor, not a ceiling.
  3. Most of the MMR falls in two or three of its grid cells, so it cannot
     separate Kalyan from Thane. No regional table is published for that reason:
     it would show near-identical numbers and imply a precision the model does
     not have.

The official readings are CPCB's and MPCB's continuous monitoring stations and
SAFAR-Mumbai. This module forecasts the SHAPE - which days are worse, which
hours are cleanest, and why - which the model does get right, because that is
driven by the weather it was built on.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from . import config as C
from .sources import _get_json

AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

FIELDS = ("pm2_5", "pm10", "nitrogen_dioxide", "ozone",
          "carbon_monoxide", "sulphur_dioxide")

# CPCB National AQI (2014): concentration breakpoints per pollutant, each
# mapped to the index band [0-50], [51-100], [101-200], [201-300], [301-400],
# [401-500]. Units are ug/m3 except CO, which CPCB states in mg/m3.
_IDX = ((0, 50), (51, 100), (101, 200), (201, 300), (301, 400), (401, 500))
BREAKPOINTS = {
    "pm2_5":            ((0, 30), (31, 60), (61, 90), (91, 120),
                         (121, 250), (251, 500)),
    "pm10":             ((0, 50), (51, 100), (101, 250), (251, 350),
                         (351, 430), (431, 800)),
    "nitrogen_dioxide": ((0, 40), (41, 80), (81, 180), (181, 280),
                         (281, 400), (401, 800)),
    "sulphur_dioxide":  ((0, 40), (41, 80), (81, 380), (381, 800),
                         (801, 1600), (1601, 3200)),
    "ozone":            ((0, 50), (51, 100), (101, 168), (169, 208),
                         (209, 748), (749, 1500)),
    "carbon_monoxide":  ((0, 1.0), (1.1, 2.0), (2.1, 10.0), (10.1, 17.0),
                         (17.1, 34.0), (34.1, 68.0)),
}
EIGHT_HOUR = {"ozone", "carbon_monoxide"}

NAMES = {
    "pm2_5": "PM2.5", "pm10": "PM10", "nitrogen_dioxide": "NO₂",
    "ozone": "ozone", "carbon_monoxide": "CO", "sulphur_dioxide": "SO₂",
}

# CPCB's own category names and health statements, verbatim in substance.
CATEGORIES = (
    (50, "Good", "Minimal impact."),
    (100, "Satisfactory",
     "Minor breathing discomfort to sensitive people."),
    (200, "Moderately polluted",
     "Breathing discomfort to people with lung disease such as asthma, and "
     "discomfort to people with heart disease, children and older adults."),
    (300, "Poor",
     "Breathing discomfort to most people on prolonged exposure, and "
     "discomfort to people with heart disease."),
    (400, "Very poor",
     "Respiratory illness on prolonged exposure, more pronounced in people "
     "with lung and heart disease."),
    (500, "Severe",
     "Respiratory effects even on healthy people, and serious impacts on "
     "people with lung or heart disease - even during light activity."),
)

POOR = 201
VERY_POOR = 301


@dataclass
class AQIDay:
    day: date
    aqi: int | None = None
    category: str = ""
    health: str = ""
    dominant: str = ""
    sub: dict[str, int] = field(default_factory=dict)
    pm25_mean: float | None = None
    pm10_mean: float | None = None
    cleanest_hour: int | None = None
    dirtiest_hour: int | None = None
    sentence: str = ""

    @property
    def pm_index(self) -> int | None:
        """The particulate half of the index: worst of PM2.5 and PM10.

        This is what the alerts key on, not the full AQI. On the first day
        this ran, the model's ozone drove its AQI on five of six days and put
        Sunday at "very poor 304" - while particulates alone said 192.
        Mumbai's official stations usually name PM10 or PM2.5 as the cause in
        winter, and surface ozone is the least constrained thing a global model
        produces over India, so an alert raised on ozone alone would most
        likely be crying wolf. Particulates are the health-relevant part and
        the part the model, if anything, reads low.
        """
        pm = [v for k, v in self.sub.items() if k in ("pm2_5", "pm10")]
        return max(pm) if pm else None

    @property
    def ozone_led(self) -> bool:
        return self.dominant == "ozone"


def category(aqi: int | None) -> tuple[str, str]:
    if aqi is None:
        return ("", "")
    for top, name, health in CATEGORIES:
        if aqi <= top:
            return name, health
    return CATEGORIES[-1][1], CATEGORIES[-1][2]


def sub_index(pollutant: str, conc: float | None) -> int | None:
    """CPCB linear interpolation inside the band the concentration falls in."""
    if conc is None:
        return None
    bands = BREAKPOINTS[pollutant]
    for (lo_c, hi_c), (lo_i, hi_i) in zip(bands, _IDX):
        if conc <= hi_c:
            if conc < lo_c:            # in the gap between two integer bands
                conc = lo_c
            return round(lo_i + (hi_i - lo_i) * (conc - lo_c) / (hi_c - lo_c))
    return 500


def _hours(times: Sequence[str], day: date) -> list[int]:
    out = []
    for i, t in enumerate(times):
        try:
            if datetime.fromisoformat(t).date() == day:
                out.append(i)
        except ValueError:
            continue
    return out


def _worst_8h(vals: Sequence[float | None]) -> float | None:
    clean = [v for v in vals if v is not None]
    if len(clean) < 8:
        return max(clean) if clean else None
    return max(statistics.mean(clean[i:i + 8]) for i in range(len(clean) - 7))


def fetch(site=None, days: int = 7) -> dict | None:
    """Hourly pollutant concentrations for one point, or None on failure."""
    site = site or C.HOME
    try:
        raw = _get_json(AQ_URL, {
            "latitude": site.lat, "longitude": site.lon,
            "hourly": ",".join(FIELDS),
            "forecast_days": min(days, 7), "timezone": C.TIMEZONE})
    except Exception:                                   # noqa: BLE001
        return None
    if isinstance(raw, list):
        raw = raw[0]
    return raw.get("hourly") if raw else None


def assess_day(hourly: dict, day: date) -> AQIDay:
    out = AQIDay(day=day)
    idx = _hours(hourly.get("time", []), day)
    if len(idx) < 18:          # a partial day would understate a 24-h mean
        return out

    sub: dict[str, int] = {}
    for p in FIELDS:
        series = hourly.get(p) or []
        vals = [series[i] for i in idx if i < len(series)]
        if p in EIGHT_HOUR:
            conc = _worst_8h(vals)
        else:
            clean = [v for v in vals if v is not None]
            conc = statistics.mean(clean) if clean else None
        if conc is not None and p == "carbon_monoxide":
            conc = conc / 1000.0                       # ug/m3 -> mg/m3
        si = sub_index(p, conc)
        if si is not None:
            sub[p] = si

    # CPCB's rule: three pollutants, one of them particulate.
    if len(sub) < 3 or not ({"pm2_5", "pm10"} & set(sub)):
        return out

    dom = max(sub, key=sub.get)
    out.sub, out.aqi, out.dominant = sub, sub[dom], dom
    out.category, out.health = category(out.aqi)

    pm = hourly.get("pm2_5") or []
    day_pm = [(datetime.fromisoformat(hourly["time"][i]).hour, pm[i])
              for i in idx if i < len(pm) and pm[i] is not None]
    if day_pm:
        out.cleanest_hour = min(day_pm, key=lambda x: x[1])[0]
        out.dirtiest_hour = max(day_pm, key=lambda x: x[1])[0]
        out.pm25_mean = statistics.mean(v for _, v in day_pm)
    p10 = [hourly["pm10"][i] for i in idx
           if i < len(hourly.get("pm10", [])) and hourly["pm10"][i] is not None]
    if p10:
        out.pm10_mean = statistics.mean(p10)
    out.sentence = _sentence(out)
    return out


def _sentence(a: AQIDay) -> str:
    if a.aqi is None:
        return ""
    s = (f"AQI around **{a.aqi}** — {a.category.lower()} on India's "
         f"national scale, driven by {NAMES.get(a.dominant, a.dominant)}.")
    if a.ozone_led and a.pm_index is not None:
        pm_cat = category(a.pm_index)[0].lower()
        s += (f" Read that with care: ozone is the model's least reliable "
              f"pollutant here and an unusual cause for Mumbai in winter, "
              f"when the official stations usually name particulates. On "
              f"particulates alone it reads **{a.pm_index}**, {pm_cat}.")
    if a.cleanest_hour is not None and a.dirtiest_hour is not None:
        s += (f" Cleanest around {a.cleanest_hour:02d}:00, worst around "
              f"{a.dirtiest_hour:02d}:00")
        # The usual winter shape, explained once rather than left as numbers.
        if 10 <= a.cleanest_hour <= 16 and (a.dirtiest_hour >= 19
                                            or a.dirtiest_hour <= 7):
            s += (" — the midday sea breeze and a deep mixed layer dilute it; "
                  "after dark the layer collapses to a few hundred metres and "
                  "everything emitted in the evening stays at breathing "
                  "height.")
        else:
            s += "."
    # Health advice follows the particulate reading, for the reason pm_index
    # gives: an ozone-only "poor" should not tell asthmatics to stay indoors.
    if a.pm_index is not None and a.pm_index >= POOR:
        s += f" {category(a.pm_index)[1]}"
    return s


def outlook(days: Sequence[date], site=None) -> list[AQIDay]:
    hourly = fetch(site, days=len(days) + 1)
    if not hourly:
        return []
    return [d for d in (assess_day(hourly, x) for x in days) if d.aqi is not None]


CAVEAT = ("Computed here on CPCB's national AQI scale from CAMS model "
          "concentrations at about 40 km. That grid cannot see a junction or a "
          "construction site, and global models tend to read low over Indian "
          "cities — treat this as a floor and as the shape of the week, not a "
          "street reading. The official numbers are CPCB / MPCB stations and "
          "SAFAR-Mumbai.")


def render(days: Sequence[AQIDay]) -> str:
    rows = [a for a in days if a.aqi is not None]
    if not rows:
        return ""
    out = "## Air quality\n\n"
    out += ("| Day | AQI | Category | Driven by | Particulates only "
            "| Cleanest | Worst |\n|---|---|---|---|---|---|---|\n")
    for a in rows:
        out += (f"| {a.day:%a %d %b} | {a.aqi} | {a.category} "
                f"| {NAMES.get(a.dominant, a.dominant)} "
                f"| {a.pm_index if a.pm_index is not None else '—'} "
                f"| {('%02d:00' % a.cleanest_hour) if a.cleanest_hour is not None else '—'} "
                f"| {('%02d:00' % a.dirtiest_hour) if a.dirtiest_hour is not None else '—'} |\n")
    out += f"\n**Today** — {rows[0].sentence}\n\n> {CAVEAT}\n\n"
    return out
