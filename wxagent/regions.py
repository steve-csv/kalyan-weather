"""
Where a system is going, and which regions get wet on which days.

WHY THIS EXISTS
---------------
Everything else in this agent answers "what happens at Kalyan", and a couple
of sections widen that to the MMR. Neither answers the question the regional
forecasters on X and WhatsApp actually answer, which is the one people share:

    a low is crossing the country - who gets rain, how much, and on what dates

Abhijit Modak's 11 Sep 2026 post is the shape of it. A fresh Bay low, expected
to run west; Vidarbha 11-13 Sep; Marathwada and north Madhya Maharashtra
12-14 Sep; north Konkan including Mumbai 13-15 Sep; south Konkan largely
missing out because the system is compact and quick; Saurashtra dependent on
whether the circulation holds together past west Madhya Pradesh.

That is a sequence of ARRIVALS ACROSS REGIONS, driven by one track. This
module produces the same thing from the models rather than from judgement.

WHAT IT DOES NOT DO
-------------------
It does not predict the track. The track comes from `systems`, which reads it
off the ECMWF pressure field, and everything here is downstream of that. When
the models are wrong about where the low goes, every date below moves with it,
which is why the rendered section says so rather than implying the dates are
firm.

It also does not average the three models into one number. Each region gets a
median and the full per-model spread on its peak day, because a region where
ECMWF says 20 mm and GFS says 60 mm has not been forecast at all - it has been
bracketed, and the reader needs to see that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from . import config as C
from .diagnostics import imd_category
from .sources import FetchError, QuotaExhausted, _get_json

_MODEL_LABEL = {m.key: m.label for m in C.MODELS}

# A day counts as part of a region's wet window at or above this. Below it the
# rain is not what anyone would plan around, and stringing such days into a
# "window" would make every region look permanently wet through a monsoon.
WET_DAY_MM = 5.0

# And this is the bar for calling a day the region's active spell rather than
# ordinary monsoon drizzle.
ACTIVE_DAY_MM = 15.6          # the IMD moderate-rain threshold


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    short: str
    points: tuple[tuple[str, float, float], ...]
    note: str = ""


# Deliberately named the way the regional forecasters name them, so a reader
# holding one of their posts next to this page can compare like with like.
# Two sample points each: one is a point forecast dressed up as a region, and
# these areas are large enough that a single gauge-equivalent misleads.
REGIONS: tuple[Region, ...] = (
    Region(
        "north_konkan", "North Konkan — Mumbai, Thane, Palghar", "N Konkan",
        (("Mumbai", 19.09, 72.87), ("Palghar", 19.70, 72.77)),
        "The home coast. Takes both coastal bands and Ghat enhancement.",
    ),
    Region(
        "south_konkan", "South Konkan — Ratnagiri, Sindhudurg", "S Konkan",
        (("Ratnagiri", 16.99, 73.31), ("Sindhudurg", 16.13, 73.68)),
        "Further from most west-tracking systems; often left out when one "
        "is compact.",
    ),
    Region(
        "north_madhya", "North Madhya Maharashtra — Nashik, Jalgaon", "N Mad MH",
        (("Nashik", 20.00, 73.79), ("Jalgaon", 21.01, 75.57)),
        "The corridor a west-tracking low crosses on its way to Gujarat.",
    ),
    Region(
        "south_madhya", "South Madhya Maharashtra — Pune, Solapur", "S Mad MH",
        (("Pune", 18.52, 73.86), ("Solapur", 17.66, 75.91)),
        "Rain-shadow country. Needs the system itself, not just onshore flow.",
    ),
    Region(
        "marathwada", "Marathwada — Chh. Sambhajinagar, Latur", "Marathwada",
        (("Chh. Sambhajinagar", 19.88, 75.34), ("Latur", 18.40, 76.58)),
        "Agricultural belt that depends on system rain rather than terrain.",
    ),
    Region(
        "vidarbha", "Vidarbha — Nagpur, Akola", "Vidarbha",
        (("Nagpur", 21.15, 79.09), ("Akola", 20.71, 77.00)),
        "First part of Maharashtra a Bay system reaches.",
    ),
    Region(
        "saurashtra", "Saurashtra — Rajkot, Dwarka", "Saurashtra",
        (("Rajkot", 22.30, 70.80), ("Dwarka", 22.24, 68.97)),
        "Only gets wet when a system holds together all the way west.",
    ),
)


@dataclass
class RegionDay:
    day: date
    per_model: dict[str, float]

    @property
    def median(self) -> float:
        v = sorted(self.per_model.values())
        if not v:
            return 0.0
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

    @property
    def lo(self) -> float:
        return min(self.per_model.values()) if self.per_model else 0.0

    @property
    def hi(self) -> float:
        return max(self.per_model.values()) if self.per_model else 0.0


@dataclass
class RegionOutlook:
    region: Region
    days: list[RegionDay] = field(default_factory=list)

    @property
    def peak(self) -> RegionDay | None:
        return max(self.days, key=lambda d: d.median) if self.days else None

    @property
    def total_median(self) -> float:
        return sum(d.median for d in self.days)

    @property
    def window(self) -> tuple[date, date] | None:
        """The contiguous run of wet days containing the peak.

        Containing the PEAK, not merely the longest run: a region with a
        three-day scatter early in the week and its real spell at the weekend
        should report the weekend, which is what a reader is planning around.
        """
        pk = self.peak
        if pk is None or pk.median < WET_DAY_MM:
            return None
        idx = self.days.index(pk)
        i = idx
        while i > 0 and self.days[i - 1].median >= WET_DAY_MM:
            i -= 1
        j = idx
        while j < len(self.days) - 1 and self.days[j + 1].median >= WET_DAY_MM:
            j += 1
        return (self.days[i].day, self.days[j].day)

    @property
    def band(self) -> str:
        pk = self.peak
        return imd_category(pk.median)[0] if pk else "No / trace rain"

    @property
    def agreement(self) -> str:
        """How far apart the models are on the peak day."""
        pk = self.peak
        if pk is None or pk.median <= 0:
            return "n/a"
        span = pk.hi - pk.lo
        if span <= max(4.0, pk.median * 0.4):
            return "close"
        if span <= max(12.0, pk.median * 1.2):
            return "loose"
        return "wide"


def fetch(days: int = 7, *, quiet: bool = True) -> list[RegionOutlook]:
    """Daily rainfall per region, per model, for the coming week.

    ONE request for all fourteen points, asking for `precipitation_sum` rather
    than the full hourly block. The obvious implementation - reuse
    `fetch_sites` - costs three requests of every hourly field for data that
    is then immediately summed to daily, and on 12 Sep 2026 those three extra
    requests pushed the weekly run into an Open-Meteo 429 that knocked out the
    upstream-drivers section further down the same run. A new section must not
    starve an existing one.
    """
    pts = [(f"{r.key}::{nm}", la, lo)
           for r in REGIONS for nm, la, lo in r.points]
    ids = [m.key for m in C.MODELS]
    try:
        raw = _get_json(C.FORECAST_URL, {
            "latitude": ",".join(f"{p[1]:.4f}" for p in pts),
            "longitude": ",".join(f"{p[2]:.4f}" for p in pts),
            "daily": "precipitation_sum",
            "models": ",".join(ids),
            "forecast_days": days,
            "timezone": C.TIMEZONE,
        }, timeout=90)
    except QuotaExhausted:
        # Let this one through. It means every later fetch in this run will
        # fail too, and the central handler aborts rather than publishing a
        # bulletin with half its sections quietly missing.
        raise
    except FetchError as exc:
        if not quiet:
            print(f"  ! regional outlook unavailable: {exc}")
        return []
    if not isinstance(raw, list):
        raw = [raw]
    if len(raw) < len(pts):
        if not quiet:
            print(f"  ! regional outlook: got {len(raw)} of {len(pts)} points")
        return []

    # key -> model -> {day: mm}
    by_key: dict[str, dict[str, dict[date, float]]] = {}
    for (key, _la, _lo), loc in zip(pts, raw):
        daily = loc.get("daily", {}) or {}
        times = daily.get("time", []) or []
        for mid in ids:
            series = (daily.get(f"precipitation_sum_{mid}")
                      or (daily.get("precipitation_sum") if len(ids) == 1
                          else None))
            if not series:
                continue
            for t, v in zip(times, series):
                if v is None:
                    continue
                by_key.setdefault(key, {}).setdefault(mid, {})[
                    date.fromisoformat(t)] = float(v)

    out: list[RegionOutlook] = []
    for r in REGIONS:
        keys = [f"{r.key}::{nm}" for nm, _la, _lo in r.points]
        # day -> model -> the region's points, averaged
        acc: dict[date, dict[str, list[float]]] = {}
        for k in keys:
            for mid, series in by_key.get(k, {}).items():
                for d, v in series.items():
                    acc.setdefault(d, {}).setdefault(mid, []).append(v)
        rd = [RegionDay(day=d,
                        per_model={m: sum(v) / len(v) for m, v in models.items()})
              for d, models in sorted(acc.items())]
        out.append(RegionOutlook(region=r, days=rd))
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _span(w: tuple[date, date] | None) -> str:
    if w is None:
        return "no clear spell"
    a, b = w
    if a == b:
        return f"{a:%a %d %b}"
    if a.month == b.month:
        return f"{a:%a %d}–{b:%a %d %b}"
    return f"{a:%a %d %b}–{b:%a %d %b}"


AGREE_WORDS = {
    "close": "the three models are close on this",
    "loose": "the models differ enough that the total is a bracket, not a figure",
    "wide": "the models disagree badly here — treat the dates as the forecast "
            "and the millimetres as barely forecast at all",
}


def render(outlooks: Sequence[RegionOutlook], *, lead=None) -> str:
    """The regional section for the weekly bulletin."""
    if not outlooks:
        return ""

    out = "## Who gets this system, and when\n\n"
    out += (
        "The sections above answer *what happens at Kalyan*. This one answers "
        "the question the regional forecasters answer: a system is crossing "
        "the country, so **which regions get rain, how much, and on which "
        "days**. The order below is by how much rain each region is due over "
        "the week, wettest first.\n\n"
    )

    if lead is not None:
        # `lead` is a systems.SystemEvent - one weather event, however many
        # closed centres the detector resolved inside it. Reading .track off
        # it would be reading one centre and calling it the system.
        ca = lead.closest
        press = (f"**{lead.min_pressure:.0f}–{lead.max_pressure:.0f} hPa**"
                 if lead.max_pressure - lead.min_pressure >= 1
                 else f"**{lead.min_pressure:.0f} hPa**")
        spread = ""
        if lead.split:
            lo, hi = lead.distance_span
            spread = (f" The models put its centre anywhere between "
                      f"{lo:,.0f} km and {hi:,.0f} km from Kalyan, so the "
                      "regional dates below carry that same uncertainty.")
        out += (
            f"**The system driving it.** {lead.headline}. Minimum pressure "
            f"{press}, coming closest to Kalyan at about "
            f"**{ca.distance_km:,.0f} km**"
            + (f" on {lead.closest_day()}" if lead.closest_day() else "")
            + ". Everything in the table below is downstream of that track — "
            "the dates are the model's answer to *where will this low be*, so "
            "if it runs further north, further south, or falls apart early, "
            "every window moves with it." + spread + "\n\n"
        )

    ranked = sorted(outlooks, key=lambda o: -o.total_median)

    out += "| Region | Rain window | Peak day | Peak total | Models |\n"
    out += "|---|---|---|---|---|\n"
    for o in ranked:
        pk = o.peak
        if pk is None or pk.median < WET_DAY_MM:
            out += (f"| **{o.region.short}** | no meaningful spell this week "
                    f"| — | — | — |\n")
            continue
        per = " · ".join(f"{_MODEL_LABEL.get(m, m)} {v:.0f}"
                         for m, v in sorted(pk.per_model.items()))
        out += (f"| **{o.region.short}** | {_span(o.window)} "
                f"| {pk.day:%a %d %b} "
                f"| {pk.lo:.0f}–{pk.hi:.0f} mm "
                f"| {per} |\n")
    out += "\n"

    # The regions that actually matter get a sentence each, in plain words.
    for o in ranked[:4]:
        pk = o.peak
        if pk is None or pk.median < WET_DAY_MM:
            continue
        out += (
            f"**{o.region.name}.** {o.band}, heaviest on "
            f"**{pk.day:%A %d %b}**. The active spell runs **{_span(o.window)}**, "
            f"with guidance on the peak day spanning {pk.lo:.0f}–{pk.hi:.0f} mm — "
            f"{AGREE_WORDS.get(o.agreement, '')}. {o.region.note}\n\n"
        )

    dry = [o for o in ranked
           if o.peak is None or o.peak.median < WET_DAY_MM]
    if dry:
        out += ("**Left out this week:** "
                + ", ".join(o.region.short for o in dry)
                + ". A compact system rains close to its own centre, so the "
                "regions its track misses can stay dry while a neighbouring "
                "one is soaked.\n\n")

    out += (
        "> These are seven-day model totals over large areas, so they carry "
        "every limit the daily forecast carries and one more: a region is not "
        "a place. Nagpur and Akola are 200 km apart and share a row here. Use "
        "this for the **sequence and the dates** — who gets it first, who gets "
        "it worst, who misses out — and use the Kalyan sections above for what "
        "to actually do tomorrow.\n"
    )
    return out
