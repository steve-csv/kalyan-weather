"""
Week-in-review-and-ahead summary.

Answers a question the day-by-day tables cannot: *why* is the week shaped the
way it is? That has two halves, and both belong in the answer:

  METEOROLOGICAL - what the atmosphere is doing. Monsoon trough position,
  the offshore trough, the strength and angle of the 850 hPa current, any low
  pressure area, the background dipole state.

  GEOGRAPHICAL - why the same atmosphere produces very different totals across
  a region you can drive across in two hours. The Sahyadri wall, the windward
  approach, the transition belt Kalyan sits in, the rain shadow behind.

The window deliberately spans past AND future days. Observed days are fetched
from the ERA5 archive and labelled as observations; forecast days come from the
cached model payload and are labelled as forecasts. Mixing the two silently
would be the dishonest version of this section - a reader must be able to see
where measurement stops and prediction starts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Sequence

from . import config as C
from .sources import _get_json

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


@dataclass
class DayEntry:
    day: date
    observed: bool
    mm_lo: float
    mm_hi: float
    label: str          # IMD band
    note: str = ""


@dataclass
class WeekSummary:
    start: date
    end: date
    site_name: str
    days: list[DayEntry]
    headline: str
    meteorology: list[str]
    geography: list[str]
    outlook: str
    observed_total: float
    forecast_total: float


# --------------------------------------------------------------------------
# Observed portion
# --------------------------------------------------------------------------

def fetch_observed_days(site, start: date, end: date) -> dict[date, float]:
    """
    ERA5 daily rainfall for past days.

    Uses the archive endpoint, which carries its own request budget separate
    from the forecast API - so this still works on a day the forecast quota is
    spent.
    """
    if start > end:
        return {}
    params = {
        "latitude": site.lat, "longitude": site.lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "precipitation_sum", "timezone": C.TIMEZONE,
    }
    try:
        raw = _get_json(ARCHIVE_URL, params, timeout=60)
    except Exception:                             # noqa: BLE001
        return {}
    if isinstance(raw, list):
        raw = raw[0]
    daily = raw.get("daily", {})
    out: dict[date, float] = {}
    for t, v in zip(daily.get("time", []), daily.get("precipitation_sum", [])):
        if v is not None:
            out[date.fromisoformat(t)] = float(v)
    return out


# --------------------------------------------------------------------------
# Narrative
# --------------------------------------------------------------------------

def _band(mm: float) -> str:
    from .diagnostics import imd_category
    return imd_category(mm)[0]


def _meteorology(daily: dict, weekly: dict | None) -> list[str]:
    """The atmospheric drivers, read off the diagnosis already computed."""
    out: list[str] = []

    regime = daily.get("regime") or ""
    note = daily.get("regimeNote") or ""
    if regime:
        out.append(f"**Regime — {regime.title()}.** {note}")

    sy = daily.get("systems") or (weekly or {}).get("systems") or {}
    if sy.get("trough"):
        out.append(f"**Offshore trough.** {sy['trough']}")
    lows = sy.get("list") or []
    if lows:
        out.append("**Low pressure.** " + " ".join(
            f"{x['headline']}." for x in lows[:2]))
    else:
        out.append("**Low pressure.** No organised low or depression is "
                   "tracked within range this week, so the rain is being driven "
                   "by the broad monsoon current and the terrain rather than by "
                   "a passing system.")

    ing = daily.get("ingredients") or {}
    if ing.get("orographic") is not None:
        out.append(
            f"**Onshore current.** The 850 hPa wind is "
            f"{ing.get('wind850Dir', '--')} at "
            f"{ing.get('wind850Speed', '--')} m/s, giving a terrain-normal "
            f"component of {ing['orographic']} m/s — "
            f"{ing.get('forcingClass', 'unclassified')} forcing against the "
            "Ghat face. That component, not the raw wind speed, is what "
            "decides how hard the hills squeeze the air.")
    if ing.get("rh700") is not None:
        out.append(
            f"**Moisture depth.** Relative humidity runs "
            f"{ing.get('rh925','--')}% at 925 hPa, {ing.get('rh850','--')}% at "
            f"850 and {ing['rh700']}% at 700 — a **{ing.get('depthClass','')}** "
            "column. Depth matters more than surface humidity: a moist "
            "boundary layer under a dry mid-level produces shallow cloud and "
            "disappointing rain.")

    if weekly and weekly.get("weekRegime"):
        wr = weekly["weekRegime"]
        out.append(f"**Across the week — {wr['label']}.** {wr['note']}")
    return out


def _geography(weekly: dict | None, home_area: str = "kalyan_belt") -> list[str]:
    """Why the same weather lands so unevenly across the region."""
    out = [
        "**The Sahyadri wall.** The Western Ghats run almost unbroken "
        "north–south, 30–60 km inland, straight across the path of the "
        "monsoon westerlies. Air coming off the Arabian Sea has nowhere to go "
        "but up. Rising air cools, cools air condenses, and the rain falls on "
        "the windward side. Everything else in this section is a consequence "
        "of that one fact.",

        "**Why the coast and the crest differ.** Over the sea and the coastal "
        "strip the air is merely moist; the lifting only really begins where "
        "the land starts to climb. So Mumbai's suburbs get the bands as they "
        "come ashore, while Matheran and Lonavala sit in the part of the slope "
        "where the air is being forced hardest upward — routinely double the "
        "coastal total in the same setup.",

        "**Kalyan's position is the awkward one.** The Thane–Kalyan–Karjat "
        "belt sits between the two: far enough inland to miss some of the "
        "coastal convergence, not high enough to get full orographic "
        "enhancement. It picks up coastal bands *and* a share of the terrain "
        "effect, which is why it usually lands between the suburbs and the "
        "Ghats rather than tracking either — and why a forecast written for "
        "'Mumbai' is routinely wrong here.",

        "**The rain shadow behind.** Having dumped its moisture climbing, the "
        "air descends the eastern slope, warming and drying as it goes. Pune, "
        "barely 100 km from Lonavala, receives a small fraction of the same "
        "week's rain. When that contrast narrows, it is a signal in itself: it "
        "means the moisture is deep enough to survive the crossing, which "
        "usually implies a system rather than a simple westerly.",
    ]

    areas = (weekly or {}).get("areas") or []
    if areas:
        wettest, driest = areas[0], areas[-1]
        out.append(
            f"**This week's spread.** {wettest['name']} takes about "
            f"{wettest['weekMm']:.0f} mm against {driest['weekMm']:.0f} mm for "
            f"{driest['name']} — the same monsoon, the same seven days, a "
            f"{wettest['weekMm'] - driest['weekMm']:.0f} mm gap produced almost "
            "entirely by where the land rises and where it doesn't.")
    return out


def build(daily: dict, weekly: dict | None, site, *,
          start: date, end: date, today: date | None = None) -> WeekSummary:
    today = today or date.today()

    observed = fetch_observed_days(site, start, min(end, today - timedelta(days=1)))

    fc: dict[date, tuple[float, float]] = {}
    for row in (daily.get("plainWeek") or []):
        iso = row.get("iso")
        facts = row.get("facts") or {}
        if iso and facts.get("rainHi") is not None:
            fc[date.fromisoformat(iso)] = (facts.get("rainLo", 0.0),
                                           facts["rainHi"])
    # Older payloads have no facts; fall back to the outlook rows.
    if not fc:
        for row in (daily.get("outlook") or []):
            try:
                d = datetime.strptime(
                    f"{row['day']} {today.year}", "%a %d %b %Y").date()
            except (ValueError, KeyError):
                continue
            fc[d] = (row.get("lo", 0.0), row.get("hi", 0.0))
        rhi = daily.get("rainHi")
        if rhi is not None:
            fc[today] = (daily.get("rainLo", 0.0), rhi)

    days: list[DayEntry] = []
    d = start
    while d <= end:
        if d in observed:
            mm = observed[d]
            days.append(DayEntry(d, True, mm, mm, _band(mm),
                                 "measured (reanalysis)"))
        elif d in fc:
            lo, hi = fc[d]
            days.append(DayEntry(d, False, lo, hi, _band(hi), "forecast"))
        d += timedelta(days=1)

    obs_total = sum(x.mm_hi for x in days if x.observed)
    fc_total = sum(x.mm_hi for x in days if not x.observed)

    wet = [x for x in days if x.mm_hi >= C.MEASURABLE_RAIN_MM]
    wettest = max(days, key=lambda x: x.mm_hi) if days else None
    if wettest and wettest.mm_hi >= C.HEAVY_DAY_MM:
        headline = (f"A wet stretch with a genuinely heavy day on "
                    f"{wettest.day:%A %d}.")
    elif len(wet) >= len(days) * 0.8:
        headline = ("Rain on almost every day of the window, but nothing "
                    "reaching the heavy category.")
    elif wet:
        headline = f"A mixed window — {len(wet)} of {len(days)} days with rain."
    else:
        headline = "A dry window."

    outlook = (
        f"Across {start:%d %b} to {end:%d %b}, "
        f"{obs_total:.0f} mm has already fallen and roughly {fc_total:.0f} mm "
        f"more is expected, on the wettest single day reaching "
        f"{wettest.mm_hi:.0f} mm ({wettest.label.lower()})."
        if wettest else "Insufficient data for the requested window."
    )

    return WeekSummary(
        start=start, end=end, site_name=site.name, days=days,
        headline=headline,
        meteorology=_meteorology(daily, weekly),
        geography=_geography(weekly),
        outlook=outlook,
        observed_total=obs_total, forecast_total=fc_total,
    )


def to_payload(ws: WeekSummary) -> dict:
    return {
        "start": ws.start.strftime("%d %b"),
        "end": ws.end.strftime("%d %b %Y"),
        "site": ws.site_name,
        "headline": ws.headline,
        "outlook": ws.outlook,
        "observedTotal": round(ws.observed_total, 1),
        "forecastTotal": round(ws.forecast_total, 1),
        "meteorology": ws.meteorology,
        "geography": ws.geography,
        "days": [
            {"day": d.day.strftime("%a %d %b"), "observed": d.observed,
             "lo": round(d.mm_lo, 1), "hi": round(d.mm_hi, 1),
             "band": d.label, "note": d.note}
            for d in ws.days
        ],
    }
