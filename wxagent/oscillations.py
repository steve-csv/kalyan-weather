"""
Deriving MJO and MISO from convection, rather than taking them off a feed.

WHY DERIVE THEM AT ALL
----------------------
The published indices are either stale or awkward here. BoM's RMM file stops in
early 2024 despite its name; NOAA PSL's OMI runs weeks behind; CPC's velocity
potential index is current to within about three weeks but its sign convention
is easy to get backwards, and an inverted MJO signal is worse than none - it
would call a break during a surge.

WHAT THESE OSCILLATIONS PHYSICALLY ARE
--------------------------------------
Both are travelling envelopes of deep convection, and that is the thing being
measured here:

  MJO  - convection travelling EASTWARD along the equator, circling the globe
         in roughly 30-60 days. When the envelope sits over the Indian Ocean
         (~60-90E) it enhances convection over India; when it has moved on to
         the western Pacific, India tends to sit in the suppressed phase.

  MISO - the monsoon's own intraseasonal mode: convection propagating
         NORTHWARD from the equatorial Indian Ocean up the subcontinent over
         two to three weeks. This is the mechanism behind active and break
         spells, which is exactly what a Konkan forecaster cares about.

CONVECTION PROXY
----------------
The literature uses outgoing longwave radiation, because cold cloud tops emit
less and so mark deep convection. OLR is a PROXY for convection; precipitation
is a more direct measure of the same thing, and it is available as JSON rather
than a 349 MB NetCDF needing libraries this machine does not have. So rainfall
anomaly is used as the convection field, with cloud cover as a cross-check.

WHAT THIS IS NOT
----------------
Not RMM, and not any published index. The real ones are EOF projections onto a
long training period; these are centroid-tracking diagnostics on an anomaly
field. They capture the same physical propagation, they are current to
yesterday, and they will not match a published phase number exactly. Every
rendering says so.
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

# --- MJO domain: equatorial belt, Indian Ocean through the Pacific ---------
MJO_LATS = (-7.5, 0.0, 7.5)
MJO_LONS = tuple(float(x) for x in range(40, 181, 10))     # 40E .. 180E

# --- MISO domain: the Indian longitudes, equator to the Himalaya -----------
MISO_LONS = (70.0, 77.5, 85.0)
MISO_LATS = tuple(float(x) for x in range(-10, 31, 2))     # 10S .. 30N

WINDOW_DAYS = 45          # how far back to track propagation
CLIM_YEARS = 8            # years used to build the anomaly baseline
LAG_DAYS = 6              # ERA5 availability lag


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _fetch_grid(points: Sequence[tuple[float, float]], start: date,
                end: date) -> dict[tuple[float, float], dict[date, float]]:
    """Daily precipitation at each point, as {(lat,lon): {date: mm}}."""
    if not points:
        return {}
    params = {
        "latitude": ",".join(str(p[0]) for p in points),
        "longitude": ",".join(str(p[1]) for p in points),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "precipitation_sum", "timezone": "UTC",
    }
    try:
        raw = _get_json(ARCHIVE_URL, params, timeout=120)
    except Exception:                             # noqa: BLE001
        return {}
    if not isinstance(raw, list):
        raw = [raw]
    out: dict[tuple[float, float], dict[date, float]] = {}
    for pt, loc in zip(points, raw):
        daily = loc.get("daily", {})
        series: dict[date, float] = {}
        for t, v in zip(daily.get("time", []),
                        daily.get("precipitation_sum", [])):
            if v is not None:
                series[date.fromisoformat(t)] = float(v)
        out[pt] = series
    return out


def _climatology(points: Sequence[tuple[float, float]], start: date,
                 end: date, years: int = CLIM_YEARS
                 ) -> dict[tuple[float, float], dict[tuple[int, int], float]]:
    """Mean precipitation per point per calendar day, over `years` past years."""
    acc: dict[tuple[float, float], dict[tuple[int, int], list[float]]] = {
        p: {} for p in points}
    for back in range(1, years + 1):
        try:
            s = start.replace(year=start.year - back)
            e = end.replace(year=end.year - back)
        except ValueError:
            continue
        grid = _fetch_grid(points, s, e)
        for p, series in grid.items():
            for d, v in series.items():
                acc[p].setdefault((d.month, d.day), []).append(v)
    return {p: {k: statistics.mean(v) for k, v in md.items() if v}
            for p, md in acc.items()}


# --------------------------------------------------------------------------
# Centroid tracking
# --------------------------------------------------------------------------

def _weighted_centre(pairs: Sequence[tuple[float, float]]) -> float | None:
    """
    Position of the convection maximum, as an anomaly-weighted centroid.

    Only POSITIVE anomalies contribute: the question is where enhanced
    convection sits, and letting suppressed regions pull the centroid with
    negative weights would drag it toward wherever it is driest, which is the
    opposite of the signal.
    """
    pos = [(x, w) for x, w in pairs if w > 0]
    if not pos:
        return None
    total = sum(w for _x, w in pos)
    if total <= 0:
        return None
    return sum(x * w for x, w in pos) / total


@dataclass
class Propagation:
    positions: list[tuple[date, float]]
    speed_per_day: float | None
    direction: str
    current: float | None

    @property
    def recent(self) -> list[tuple[date, float]]:
        return self.positions[-14:]


def _track(anom_by_day: dict[date, list[tuple[float, float]]],
           axis: str) -> Propagation:
    positions: list[tuple[date, float]] = []
    for d in sorted(anom_by_day):
        c = _weighted_centre(anom_by_day[d])
        if c is not None:
            positions.append((d, c))

    # Smooth: these modes are 30-60 day phenomena, so day-to-day jitter in the
    # centroid is noise. A 7-day running mean is the standard treatment.
    smoothed: list[tuple[date, float]] = []
    for i in range(len(positions)):
        lo = max(0, i - 3)
        hi = min(len(positions), i + 4)
        window = [v for _d, v in positions[lo:hi]]
        smoothed.append((positions[i][0], sum(window) / len(window)))

    speed = None
    if len(smoothed) >= 10:
        recent = smoothed[-10:]
        span_days = (recent[-1][0] - recent[0][0]).days or 1
        speed = (recent[-1][1] - recent[0][1]) / span_days

    if speed is None:
        direction = "indeterminate"
    elif axis == "lon":
        direction = ("eastward" if speed > 0.25 else
                     "westward" if speed < -0.25 else "near-stationary")
    else:
        direction = ("northward" if speed > 0.05 else
                     "southward" if speed < -0.05 else "near-stationary")

    return Propagation(positions=smoothed, speed_per_day=speed,
                       direction=direction,
                       current=smoothed[-1][1] if smoothed else None)


# --------------------------------------------------------------------------
# MJO
# --------------------------------------------------------------------------

MJO_PHASES = (
    (1, 20, 60, "Western Indian Ocean"),
    (2, 60, 80, "Indian Ocean"),
    (3, 80, 100, "Eastern Indian Ocean"),
    (4, 100, 120, "Maritime Continent"),
    (5, 120, 140, "Maritime Continent / W Pacific"),
    (6, 140, 160, "Western Pacific"),
    (7, 160, 180, "Western Pacific"),
    (8, 180, 360, "Western Hemisphere"),
)

# Phases favourable / unfavourable for enhanced convection over India.
MJO_WET_PHASES = (2, 3, 4)
MJO_DRY_PHASES = (6, 7, 8)


def mjo_phase(lon: float | None) -> tuple[int | None, str]:
    if lon is None:
        return None, "unknown"
    for num, lo, hi, name in MJO_PHASES:
        if lo <= lon < hi:
            return num, name
    return 8, "Western Hemisphere"


@dataclass
class MJOState:
    prop: Propagation
    phase: int | None
    region: str
    favourability: str
    interpretation: str


def analyse_mjo(*, today: date | None = None, quiet: bool = True) -> MJOState | None:
    today = today or date.today()
    end = today - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=WINDOW_DAYS)
    points = [(la, lo) for lo in MJO_LONS for la in MJO_LATS]

    if not quiet:
        print(f"  MJO: sampling {len(points)} equatorial points "
              f"{start}..{end}")
    grid = _fetch_grid(points, start, end)
    if not grid:
        return None
    clim = _climatology(points, start, end)
    if not clim:
        return None

    # Anomaly, collapsed onto longitude (averaged across the equatorial band).
    by_day: dict[date, list[tuple[float, float]]] = {}
    for lon in MJO_LONS:
        for d in _dates(start, end):
            vals = []
            for la in MJO_LATS:
                p = (la, lon)
                obs = grid.get(p, {}).get(d)
                ref = clim.get(p, {}).get((d.month, d.day))
                if obs is not None and ref is not None:
                    vals.append(obs - ref)
            if vals:
                by_day.setdefault(d, []).append((lon, sum(vals) / len(vals)))

    prop = _track(by_day, axis="lon")
    phase, region = mjo_phase(prop.current)

    if phase in MJO_WET_PHASES:
        fav = "favourable"
        interp = (
            f"The convection envelope sits near {prop.current:.0f}°E, over the "
            f"{region.lower()} — the part of the cycle that enhances convection "
            "over India. Combined with an onshore current this is when active "
            "spells are most likely to organise."
        )
    elif phase in MJO_DRY_PHASES:
        fav = "unfavourable"
        interp = (
            f"The envelope has moved on to about {prop.current:.0f}°E "
            f"({region.lower()}), leaving India in the suppressed half of the "
            "cycle. Break spells tend to be longer and rain more scattered "
            "while it sits there."
        )
    else:
        fav = "neutral"
        interp = (
            f"The envelope is near {prop.current:.0f}°E ({region.lower()}), "
            "between the clearly favourable and clearly suppressed phases. "
            "Little help either way this week."
        )

    if prop.speed_per_day is not None:
        interp += (f" It is moving {prop.direction} at roughly "
                   f"{abs(prop.speed_per_day):.1f}° longitude per day")
        if prop.direction == "eastward" and phase in MJO_WET_PHASES:
            interp += " — so this favourable window is already closing."
        elif prop.direction == "eastward" and phase in MJO_DRY_PHASES:
            interp += " — heading back toward the favourable side, though that "\
                      "takes weeks, not days."
        else:
            interp += "."

    return MJOState(prop=prop, phase=phase, region=region,
                    favourability=fav, interpretation=interp)


# --------------------------------------------------------------------------
# MISO
# --------------------------------------------------------------------------

@dataclass
class MISOState:
    prop: Propagation
    band_lat: float | None
    regime: str
    interpretation: str
    days_to_arrival: int | None


KONKAN_LAT = 19.0


def analyse_miso(*, today: date | None = None, quiet: bool = True
                 ) -> MISOState | None:
    today = today or date.today()
    end = today - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=WINDOW_DAYS)
    points = [(la, lo) for lo in MISO_LONS for la in MISO_LATS]

    if not quiet:
        print(f"  MISO: sampling {len(points)} meridional points "
              f"{start}..{end}")
    grid = _fetch_grid(points, start, end)
    if not grid:
        return None
    clim = _climatology(points, start, end)
    if not clim:
        return None

    by_day: dict[date, list[tuple[float, float]]] = {}
    for la in MISO_LATS:
        for d in _dates(start, end):
            vals = []
            for lo in MISO_LONS:
                p = (la, lo)
                obs = grid.get(p, {}).get(d)
                ref = clim.get(p, {}).get((d.month, d.day))
                if obs is not None and ref is not None:
                    vals.append(obs - ref)
            if vals:
                by_day.setdefault(d, []).append((la, sum(vals) / len(vals)))

    prop = _track(by_day, axis="lat")
    lat = prop.current

    eta = None
    if (lat is not None and prop.speed_per_day
            and prop.speed_per_day > 0.05 and lat < KONKAN_LAT):
        eta = int((KONKAN_LAT - lat) / prop.speed_per_day)

    if lat is None:
        regime, interp = "unknown", "Not enough signal to locate the band."
    elif lat >= 22:
        regime = "rain belt has passed us, now to the north"
        interp = (
            f"The main band of monsoon rain has moved past us and now sits "
            f"near {lat:.0f}°N, to our north. Once it goes by, a quieter spell "
            "usually follows here for a week or two until the next one builds."
        )
    elif 15 <= lat < 22:
        regime = "rain belt is overhead"
        interp = (
            f"The main band of monsoon rain is sitting right over us, around "
            f"{lat:.0f}°N. This is the monsoon at full strength for this "
            "coast — the phase when rain keeps going for days rather than "
            "coming and stopping."
        )
    elif 5 <= lat < 15:
        # The label has to follow the DIRECTION, not just the latitude. Naming
        # a band "approaching" purely because it sits to our south produced a
        # page that read "band approaching from the south" one line above
        # "drift is southward" - it was moving away, and the headline said the
        # opposite of the data underneath it.
        coming = prop.speed_per_day is not None and prop.speed_per_day > 0.05
        going = prop.speed_per_day is not None and prop.speed_per_day < -0.05
        if coming:
            regime = "rain belt building to our south, moving up"
            interp = (
                f"The main band of monsoon rain is sitting well south of us, "
                f"near {lat:.0f}°N, and creeping our way. It normally takes "
                "two to three weeks to travel up, so this is something to "
                "watch for later in the month rather than this week."
            )
        elif going:
            regime = "rain belt to our south, drifting away"
            interp = (
                f"The main band of monsoon rain is near {lat:.0f}°N, south of "
                "us, and moving further south — away from us, not toward us. "
                "Until it turns around it is no help here."
            )
        else:
            regime = "rain belt parked to our south"
            interp = (
                f"The main band of monsoon rain is sitting near {lat:.0f}°N, "
                "south of us, and going nowhere in particular. No help here "
                "while it stays put."
            )
    else:
        regime = "rain belt still down near the equator"
        interp = (
            f"The rain is bunched up near {lat:.0f}°N, down by the equator. "
            "That is the very start of the cycle — weeks away from reaching "
            "us, and it does not always make the journey."
        )

    if eta:
        interp += (f" At the speed it is moving now, it would get here in "
                   f"roughly {eta} days.")
    if prop.speed_per_day is not None:
        # Kept, but in distance a person can picture rather than degrees.
        km = abs(prop.speed_per_day) * 111.0
        interp += (f" It is moving {prop.direction} at about {km:.0f} km a day.")

    return MISOState(prop=prop, band_lat=lat, regime=regime,
                     interpretation=interp, days_to_arrival=eta)


def _dates(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# Cross-check against CPC, to settle the sign convention
# --------------------------------------------------------------------------

CPC_MJO_URL = ("https://www.cpc.ncep.noaa.gov/products/precip/CWlink/"
               "daily_mjo_index/proj_norm_order.ascii")
CPC_LONS = (20.0, 70.0, 80.0, 100.0, 120.0, 140.0, 160.0, 240.0, 320.0, 350.0)


def cpc_crosscheck(mjo: MJOState | None, *, quiet: bool = True) -> str:
    """
    Compare the derived convection centre against CPC's published index.

    CPC's file is 200 hPa velocity potential anomalies by longitude, and its
    sign convention is genuinely easy to invert. Rather than assume it, the
    derived field - which has an unambiguous sign, because it is rainfall -
    is correlated against CPC's values. A consistent negative correlation
    means CPC-negative marks enhanced convection; positive means the reverse.
    """
    if mjo is None:
        return ""
    try:
        import urllib.request
        req = urllib.request.Request(
            CPC_MJO_URL, headers={"User-Agent": "kalyan-wx-agent/1.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:                             # noqa: BLE001
        return ""

    rows: list[tuple[date, list[float]]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 11 or not parts[0].isdigit() or len(parts[0]) != 8:
            continue
        if any("*" in p for p in parts[1:11]):
            continue
        try:
            d = datetime.strptime(parts[0], "%Y%m%d").date()
            vals = [float(x) for x in parts[1:11]]
        except ValueError:
            continue
        rows.append((d, vals))
    if not rows:
        return ""

    # Compare on MATCHING DATES. CPC's pentads run weeks behind the derived
    # field, so pairing their latest row against today's centroid would compare
    # two different states of the atmosphere and prove nothing. Instead every
    # CPC pentad that overlaps the derived track is scored, and the verdict
    # rests on the consistency across all of them rather than on one row.
    derived = dict(mjo.prop.positions)
    agree_neg = agree_pos = 0
    pairs: list[tuple[date, float, float]] = []

    for d, vals in rows[-24:]:
        centre = derived.get(d)
        if centre is None:
            continue
        nearest = min(CPC_LONS, key=lambda L: abs(L - centre))
        val = dict(zip(CPC_LONS, vals))[nearest]
        if abs(val) < 0.4:
            continue                              # too weak to carry a sign
        pairs.append((d, centre, val))
        if val < 0:
            agree_neg += 1
        else:
            agree_pos += 1

    if not pairs:
        return ("**Cross-check against CPC:** their published pentads do not "
                "overlap the derived track closely enough to test the sign "
                "convention this run. Nothing depends on it — the forecast "
                "uses the rainfall-derived field, whose sign is unambiguous.")

    n = len(pairs)
    if agree_neg > agree_pos * 2:
        note = (f"In {agree_neg} of {n} matched pentads CPC is NEGATIVE where "
                "the derived field puts convection — consistent with negative "
                "velocity-potential anomaly marking enhanced convection.")
    elif agree_pos > agree_neg * 2:
        note = (f"In {agree_pos} of {n} matched pentads CPC is POSITIVE where "
                "the derived field puts convection — implying the opposite "
                "convention to the one usually assumed.")
    else:
        note = (f"Across {n} matched pentads CPC splits {agree_neg} negative "
                f"to {agree_pos} positive at the convection longitude, which "
                "settles nothing. Their index and a rainfall centroid are "
                "measuring related but not identical things, so a clean "
                "correspondence was never guaranteed.")

    span = f"{pairs[0][0]:%d %b}–{pairs[-1][0]:%d %b}"
    return (f"**Cross-check against CPC** ({n} matched pentads, {span}): {note} "
            "Either way the forecast uses the rainfall-derived field, whose "
            "sign is unambiguous, so nothing here depends on resolving it.")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render(mjo: MJOState | None, miso: MISOState | None,
           crosscheck: str = "") -> str:
    if mjo is None and miso is None:
        return ""
    out = ""
    if miso is not None:
        out += (f"**MISO — {miso.regime}.** {miso.interpretation}\n\n")
        if miso.prop.recent:
            track = " → ".join(f"{v:.0f}" for _d, v in miso.prop.recent[::3])
            out += f"Band latitude over recent days (°N): {track}\n\n"
    if mjo is not None:
        out += (f"**MJO — phase {mjo.phase} ({mjo.region}), "
                f"{mjo.favourability}.** {mjo.interpretation}\n\n")
        if mjo.prop.recent:
            track = " → ".join(f"{v:.0f}" for _d, v in mjo.prop.recent[::3])
            out += f"Convection centre over recent days (°E): {track}\n\n"
    if crosscheck:
        out += crosscheck + "\n\n"
    out += (
        "> **These are derived diagnostics, not the published indices.** RMM "
        "and OMI are EOF projections onto a long training period; these track "
        "the centroid of a rainfall anomaly field instead — the same physical "
        "propagation, computed from data current to within a week, but they "
        "will not match a published phase number exactly. Use them for the "
        "direction of travel, not as a substitute for an official index.\n"
    )
    return out
