"""
What actually fell - recent rainfall across the MMR.

WHY THIS IS NOT GAUGE DATA, AND WHY THAT MATTERS
-------------------------------------------------
The station-by-station lists you see from the good Konkan forecasters
("Kaman 116, Bhayandar 107, Borivali 84...") come from real rain gauges - the
IMD AWS/ARG network and MCGM's civic stations. Those feeds are not public:
IMD's endpoints answer "Your IP/Domain needs to be whitelisted", which requires
a data agreement with IMD rather than an account, and MCGM's dashboard is a
single-page app with no open API behind it.

So this table is **model analysis**, not measurement. It is the same numerical
analysis that initialises the forecast, sampled at each MMR point. It is
genuinely useful for "which part of the region got hit" - the spatial pattern
is reliable - but it is NOT a gauge reading and every rendering below says so.
Where a real observation exists, it beats this.

The accumulation window follows IMD's convention - 0830 IST to 0830 IST - so
the totals line up with how rainfall is reported here, rather than a rolling
24 hours that would not match anyone else's figures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Sequence

from . import config as C
from .sources import _get_json

IMD_DAY_START = time(8, 30)


@dataclass
class ObservedSite:
    key: str
    name: str
    zone: str
    mm: float
    area: str = ""


@dataclass
class ObservedWindow:
    start: datetime
    end: datetime
    sites: list[ObservedSite]

    @property
    def wettest(self) -> ObservedSite | None:
        return max(self.sites, key=lambda s: s.mm) if self.sites else None

    @property
    def total_mean(self) -> float:
        return (sum(s.mm for s in self.sites) / len(self.sites)
                if self.sites else 0.0)


def imd_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """
    The most recently completed IMD rainfall day: 0830 IST to 0830 IST.

    Before 0830 today, the last complete window ended 0830 yesterday.
    """
    now = now or datetime.now()
    today_start = datetime.combine(now.date(), IMD_DAY_START)
    end = today_start if now >= today_start else today_start - timedelta(days=1)
    return end - timedelta(days=1), end


def fetch_observed(keys: Sequence[str] | None = None, *,
                   now: datetime | None = None,
                   quiet: bool = True) -> ObservedWindow | None:
    """Sample analysed rainfall over the last complete IMD day at MMR points."""
    from .diagnostics import window_indices  # noqa: F401 - kept for parity

    start, end = imd_window(now)
    keys = list(keys or [k for k in (
        "colaba", "santacruz", "borivali", "chembur", "vasai_virar", "palghar",
        "thane", "bhiwandi", "kalyan_west", "dombivli", "panvel", "alibag",
        "badlapur", "karjat", "igatpuri", "matheran", "malshej", "lonavala",
    ) if k in C.SITES_BY_KEY])

    area_of: dict[str, str] = {}
    for area in C.MMR_AREAS:
        for site_key in area.sites:
            area_of[site_key] = area.name

    sites = [C.SITES_BY_KEY[k] for k in keys if k in C.SITES_BY_KEY]
    if not sites:
        return None

    # One multi-point request for precipitation alone. Fetching these as
    # sixteen separate full diagnostic pulls took minutes and asked for
    # thirty-odd hourly fields per site to use exactly one of them.
    params = {
        "latitude": ",".join(f"{s.lat}" for s in sites),
        "longitude": ",".join(f"{s.lon}" for s in sites),
        "hourly": "precipitation",
        "models": C.MODELS[0].key,
        "past_days": 2,
        "forecast_days": 1,
        "timezone": C.TIMEZONE,
    }
    try:
        raw = _get_json(C.FORECAST_URL, params, timeout=90)
    except Exception as exc:                      # noqa: BLE001
        if not quiet:
            print(f"  ! observed rainfall unavailable: {exc}")
        return None
    if not isinstance(raw, list):
        raw = [raw]

    out: list[ObservedSite] = []
    for site, loc in zip(sites, raw):
        hourly = loc.get("hourly", {})
        times = hourly.get("time", []) or []
        series = hourly.get("precipitation", []) or []
        total, got = 0.0, False
        for t, v in zip(times, series):
            dt = datetime.fromisoformat(t)
            if start <= dt < end and v is not None:
                total += v
                got = True
        if got:
            out.append(ObservedSite(key=site.key, name=site.name,
                                    zone=site.zone, mm=round(total, 1),
                                    area=area_of.get(site.key, "")))

    if not out:
        return None
    out.sort(key=lambda s: -s.mm)
    return ObservedWindow(start=start, end=end, sites=out)


def render(ow: ObservedWindow | None) -> str:
    if ow is None:
        return ""

    out = (f"Rainfall over the last complete IMD day — "
           f"**{ow.start:%H%M} on {ow.start:%d %b}** to "
           f"**{ow.end:%H%M} on {ow.end:%d %b}**, ranked wettest first.\n\n")

    out += "| Place | Area | Rain |\n|---|---|---|\n"
    for s in ow.sites:
        marker = " ★" if s.key == C.HOME.key else ""
        out += f"| {s.name}{marker} | {s.area} | **{s.mm:.0f} mm** |\n"

    wettest = ow.wettest
    if wettest is not None:
        spread = wettest.mm - min(s.mm for s in ow.sites)
        out += (f"\nWettest **{wettest.name} {wettest.mm:.0f} mm**; regional "
                f"mean {ow.total_mean:.0f} mm; spread across the MMR "
                f"{spread:.0f} mm.\n")

    out += (
        "\n> **These are model-analysis figures, not rain-gauge readings, and "
        "they run substantially LOW on convective days.** The real station "
        "lists you see elsewhere come from IMD's AWS/ARG network and MCGM's "
        "civic gauges; IMD's API requires an IP whitelist (a data agreement, "
        "not an account) and MCGM publishes no open feed, so neither is "
        "reachable here.\n>\n"
        "> A measured check on 11 Aug 2026: gauges reported Borivali 84 mm and "
        "Bhayandar 107 mm in *12 hours*, while this analysis gave Borivali "
        "29 mm over the *full 24*. That is roughly a threefold under-read, and "
        "it is the expected failure mode — a grid cell averages away the "
        "intense cores that gauges sit inside.\n>\n"
        "> So: read the **ranking and the spatial pattern**, which hold up. Do "
        "not read the absolute millimetres as what fell on any street. Any "
        "real gauge reading beats this table outright.\n"
    )
    return out
