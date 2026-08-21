"""
Synoptic system tracking - low pressure areas, troughs and storms.

Guide s12 lists the systems that actually drive Mumbai rainfall: the offshore
trough, offshore vortices, Bay of Bengal lows and depressions, cyclonic
circulations and shear zones. Its "system-thinking rule" is the reason this
module exists at all:

    system -> wind response -> moisture transport -> lifting zone ->
    expected rain footprint

Everything before this module started at the wind. This starts one step
earlier, at the system producing the wind.

A single point cannot see a low. So this samples a 2D grid of mean-sea-level
pressure across the Arabian Sea, the peninsula and the Bay of Bengal, finds
closed lows as local minima, and follows them forward through the forecast to
get a track, a speed and a bearing relative to Mumbai.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Sequence

from . import config as C
from .sources import _get_json

# --------------------------------------------------------------------------
# Grid definition
# --------------------------------------------------------------------------
# Wide enough to hold a Bay of Bengal low tracking west-northwest across
# central India (Guide s12.3) and an Arabian Sea system south of 15N
# (Handbook Ch.17), at a spacing that still resolves a synoptic-scale low.

GRID_LAT = tuple(float(v) for v in range(6, 29, 2))      # 6N .. 28N
GRID_LON = tuple(float(v) for v in range(65, 95, 2))     # 65E .. 94E
GRID_STEP = 2.0

# Reference point for "how far away is this system". Kalyan is the home
# location, and at synoptic scale the ~30 km offset from the Mumbai gauge is
# immaterial - but the labels should say the place the forecast is actually for.
MUMBAI_LAT, MUMBAI_LON = 19.09, 72.87
KALYAN_LAT, KALYAN_LON = 19.2437, 73.1305
REF_LAT, REF_LON = KALYAN_LAT, KALYAN_LON
REF_NAME = "Kalyan"
EARTH_R_KM = 6371.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass16(deg: float) -> str:
    pts = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return pts[int((deg % 360) / 22.5 + 0.5) % 16]


# --------------------------------------------------------------------------
# Fetching the field
# --------------------------------------------------------------------------

@dataclass
class PressureField:
    """MSL pressure and 850 hPa wind on a lat/lon grid, per forecast hour."""
    lats: list[float]
    lons: list[float]
    times: list[str]
    pressure: dict[tuple[float, float], list[float | None]]
    wind: dict[tuple[float, float], list[float | None]]

    def p(self, lat: float, lon: float, t: int) -> float | None:
        series = self.pressure.get((lat, lon))
        if not series or t >= len(series):
            return None
        return series[t]

    def w(self, lat: float, lon: float, t: int) -> float | None:
        series = self.wind.get((lat, lon))
        if not series or t >= len(series):
            return None
        return series[t]


def fetch_pressure_field(days: int = 7, *, model: str = "ecmwf_ifs025",
                         quiet: bool = True) -> PressureField | None:
    """
    Fetch the 2D pressure + 850 hPa wind field.

    Sent in latitude bands to keep each request a sane size; Open-Meteo accepts
    comma-separated coordinate lists and returns one object per point.
    """
    lats_out: list[float] = []
    lons_out: list[float] = []
    times: list[str] = []
    pressure: dict[tuple[float, float], list[float | None]] = {}
    wind: dict[tuple[float, float], list[float | None]] = {}

    for lat in GRID_LAT:
        params = {
            "latitude": ",".join(str(lat) for _ in GRID_LON),
            "longitude": ",".join(str(lon) for lon in GRID_LON),
            "hourly": "pressure_msl,wind_speed_850hPa",
            "models": model,
            "forecast_days": days,
            "timezone": C.TIMEZONE,
            "wind_speed_unit": "ms",
        }
        try:
            raw = _get_json(C.FORECAST_URL, params, timeout=90)
        except Exception as exc:                  # noqa: BLE001
            if not quiet:
                print(f"  ! pressure field row {lat}N failed: {exc}")
            continue
        if not isinstance(raw, list):
            raw = [raw]
        for lon, loc in zip(GRID_LON, raw):
            hourly = loc.get("hourly", {})
            if not times:
                times = hourly.get("time", [])
            pressure[(lat, lon)] = hourly.get("pressure_msl", [])
            wind[(lat, lon)] = hourly.get("wind_speed_850hPa", [])
        lats_out.append(lat)

    if not times:
        return None
    return PressureField(lats=list(GRID_LAT), lons=list(GRID_LON),
                         times=times, pressure=pressure, wind=wind)


# --------------------------------------------------------------------------
# Low detection
# --------------------------------------------------------------------------

@dataclass
class LowCentre:
    lat: float
    lon: float
    pressure: float
    depth: float          # hPa below the mean of its neighbours
    max_wind: float       # 850 hPa wind in the surrounding ring, m/s
    time_index: int
    basin: str            # "Arabian Sea" | "Bay of Bengal" | "Land"

    @property
    def distance_km(self) -> float:
        return haversine(REF_LAT, REF_LON, self.lat, self.lon)

    @property
    def distance_mumbai_km(self) -> float:
        return haversine(MUMBAI_LAT, MUMBAI_LON, self.lat, self.lon)

    @property
    def bearing_from_ref(self) -> float:
        return bearing(REF_LAT, REF_LON, self.lat, self.lon)

    @property
    def intensity(self) -> str:
        """
        IMD-style intensity ladder, approximated from the 850 hPa wind in the
        low's surrounding ring. Deliberately conservative in its labelling:
        the official classification uses surface wind and is IMD's to make
        (Handbook Ch.17 is explicit that cyclone calls are not ours).
        """
        kt = self.max_wind * 1.94384
        if kt >= 64:
            return "cyclone-strength circulation"
        if kt >= 48:
            return "deep depression-strength circulation"
        if kt >= 33:
            return "depression-strength circulation"
        if self.depth >= 2.0:
            return "well-marked low pressure area"
        return "low pressure area"

    @property
    def is_significant(self) -> bool:
        return self.depth >= 1.0


# The Indian coastline, as longitude against latitude. Crude boxes are not
# good enough for the one question this feeds - "Arabian Sea or Bay of
# Bengal?" - because the subcontinent is wedge-shaped: a box of
# `lon > 80 and lat < 23` calls Chhattisgarh the Bay of Bengal, and a low
# sitting over central India then gets reported as a marine system forming in
# the Bay, which is exactly the wrong answer to give a reader.
#
# These are the coast longitudes at each latitude, west and east, read off the
# outline. Between the listed latitudes they are interpolated linearly, which
# is far more accurate than a rectangle and costs nothing.
_WEST_COAST = ((23.0, 68.3), (21.0, 72.6), (19.0, 72.8), (16.0, 73.5),
               (13.0, 74.8), (10.0, 76.0), (8.0, 77.1))
_EAST_COAST = ((23.0, 89.0), (21.0, 87.5), (18.0, 84.2), (16.0, 81.3),
               (13.0, 80.3), (10.0, 79.9), (8.0, 78.2))


def _coast_lon(lat: float, table: Sequence[tuple[float, float]]) -> float:
    """Coast longitude at this latitude, linearly interpolated."""
    if lat >= table[0][0]:
        return table[0][1]
    if lat <= table[-1][0]:
        return table[-1][1]
    for (la1, lo1), (la2, lo2) in zip(table, table[1:]):
        if la2 <= lat <= la1:
            f = (lat - la2) / (la1 - la2) if la1 != la2 else 0.0
            return lo2 + f * (lo1 - lo2)
    return table[-1][1]


def _basin(lat: float, lon: float) -> str:
    """Which sea a point sits in - or Land, which is most of this grid."""
    # North of Kutch there is no Indian sea at these longitudes, only Pakistan
    # and the Rann; south of the tip both seas merge into the Indian Ocean.
    if lat > 24.5:
        return "Land"
    if lat < 8.0:
        return "Arabian Sea" if lon < 77.0 else "Bay of Bengal"
    if lon < _coast_lon(lat, _WEST_COAST):
        return "Arabian Sea"
    if lon > _coast_lon(lat, _EAST_COAST):
        return "Bay of Bengal"
    return "Land"


def find_lows(field: PressureField, t: int, *,
              min_depth: float = 1.0) -> list[LowCentre]:
    """
    Closed lows as grid minima.

    A point qualifies when it is lower than all eight neighbours and at least
    `min_depth` hPa below their mean. Guide s2.1 is the reason for the depth
    test rather than a bare minimum: "A low-pressure area is usually associated
    with convergence... The low is therefore a sign of an organised
    circulation, not a magical rain-producing object." A dimple of 0.2 hPa is
    not an organised circulation.
    """
    lows: list[LowCentre] = []
    lats, lons = field.lats, field.lons

    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            centre = field.p(lat, lon, t)
            if centre is None:
                continue
            neigh: list[float] = []
            winds: list[float] = []
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    if di == 0 and dj == 0:
                        continue
                    ii, jj = i + di, j + dj
                    if not (0 <= ii < len(lats) and 0 <= jj < len(lons)):
                        continue
                    v = field.p(lats[ii], lons[jj], t)
                    if v is not None:
                        neigh.append(v)
                    wv = field.w(lats[ii], lons[jj], t)
                    if wv is not None:
                        winds.append(wv)
            # Need a reasonably complete ring to call it closed.
            if len(neigh) < 5 or any(v <= centre for v in neigh):
                continue
            depth = sum(neigh) / len(neigh) - centre
            if depth < min_depth:
                continue
            lows.append(LowCentre(
                lat=lat, lon=lon, pressure=centre, depth=depth,
                max_wind=max(winds) if winds else 0.0,
                time_index=t, basin=_basin(lat, lon),
            ))
    lows.sort(key=lambda l: -l.depth)
    return lows


# --------------------------------------------------------------------------
# Tracking
# --------------------------------------------------------------------------

@dataclass
class SystemTrack:
    positions: list[LowCentre] = field(default_factory=list)

    @property
    def first(self) -> LowCentre:
        return self.positions[0]

    @property
    def last(self) -> LowCentre:
        return self.positions[-1]

    @property
    def peak(self) -> LowCentre:
        return max(self.positions, key=lambda p: p.depth)

    @property
    def moved_km(self) -> float:
        return haversine(self.first.lat, self.first.lon,
                         self.last.lat, self.last.lon)

    @property
    def track_bearing(self) -> float | None:
        if len(self.positions) < 2 or self.moved_km < 50:
            return None
        return bearing(self.first.lat, self.first.lon,
                       self.last.lat, self.last.lon)

    @property
    def closest_approach(self) -> LowCentre:
        return min(self.positions, key=lambda p: p.distance_km)

    @property
    def approaching(self) -> bool:
        return self.last.distance_km < self.first.distance_km - 100

    @property
    def is_transient(self) -> bool:
        """
        A real low pressure area travels. A pressure minimum that sits still
        for days is a seasonal feature, not a system.

        This test exists because without it the detector confidently reports
        the Pakistan/Rajasthan heat low (parked near 28N 69E all summer) and
        the monsoon trough axis over the Gangetic plain as though they were
        newly-arrived depressions. They are the background state - the trough
        is already diagnosed separately - and calling them "systems" would be
        pure noise in every monsoon bulletin.
        """
        return self.moved_km >= 200.0

    @property
    def is_seasonal_feature(self) -> bool:
        """Quasi-stationary and sitting in the monsoon-trough / heat-low belt."""
        return (not self.is_transient) and self.peak.lat >= 25.0


def track_systems(field: PressureField, *, hours: Sequence[int] | None = None,
                  search_radius_km: float = 450.0) -> list[SystemTrack]:
    """
    Link low centres across time into tracks by nearest-neighbour matching.

    Deliberately simple: a synoptic low moves a few hundred km per day, so
    matching within ~450 km per 6-hour step is generous but not reckless.
    """
    if hours is None:
        hours = list(range(0, min(len(field.times), 168), 6))

    tracks: list[SystemTrack] = []
    for t in hours:
        for low in find_lows(field, t):
            best: SystemTrack | None = None
            best_d = search_radius_km
            for tr in tracks:
                if tr.last.time_index >= t:
                    continue
                d = haversine(tr.last.lat, tr.last.lon, low.lat, low.lon)
                if d < best_d:
                    best, best_d = tr, d
            if best is not None:
                best.positions.append(low)
            else:
                tracks.append(SystemTrack(positions=[low]))

    # Keep only systems that persist - a one-frame minimum is usually noise.
    tracks = [t for t in tracks if len(t.positions) >= 2]
    tracks.sort(key=lambda t: -t.peak.depth)
    return tracks


# --------------------------------------------------------------------------
# Relevance to Mumbai
# --------------------------------------------------------------------------

@dataclass
class SystemAssessment:
    track: SystemTrack
    relevance: str        # high | moderate | low
    headline: str
    reasoning: str


MONSOON_TROUGH_BELT_LAT = 25.0


def assess(track: SystemTrack, times: Sequence[str]) -> SystemAssessment:
    """
    Translate a track into what it means for Mumbai.

    Guide s12.3 is the key idea: "A Bay system can affect Mumbai even when its
    centre remains far away. As it moves inland, it can strengthen the
    cross-country pressure gradient and the Arabian Sea westerlies." So
    distance alone is the wrong test - the question is what the circulation
    does to the flow reaching the Konkan.
    """
    peak = track.peak
    closest = track.closest_approach
    when = ""
    if closest.time_index < len(times):
        when = datetime.fromisoformat(times[closest.time_index]).strftime("%a %d %b")

    brg = track.track_bearing
    direction = f"tracking {compass16(brg)}" if brg is not None else "quasi-stationary"

    # Seasonal background features are filtered out before anything else. The
    # monsoon trough is diagnosed on its own terms elsewhere; reporting its
    # axis as a "low pressure area" would double-count it and bury any real
    # system in noise.
    if track.is_seasonal_feature:
        return SystemAssessment(
            track=track, relevance="background",
            headline=(f"Quasi-stationary low near {peak.lat:.0f}°N "
                      f"{peak.lon:.0f}°E — seasonal, not a travelling system"),
            reasoning=(
                "This centre barely moves across the forecast period and sits "
                "in the monsoon-trough / heat-low belt. It is the background "
                "state of the season rather than an approaching system, and is "
                "excluded from the alerts. The trough's position is tracked "
                "separately."),
        )

    if peak.basin == "Arabian Sea" and closest.distance_km < 500:
        relevance = "high"
        headline = (f"{peak.intensity.capitalize()} in the Arabian Sea, closest "
                    f"approach ~{closest.distance_km:.0f} km from {REF_NAME} "
                    f"on {when}")
        reasoning = (
            "An Arabian Sea system this close sits directly upstream of the "
            "whole MMR. Guide s12.2: a slow-moving offshore circulation can "
            "repeatedly steer rain bands onto the same stretch of coast, and "
            "global models often place the small centre badly even when they "
            "have the broad environment right - so radar and satellite matter "
            "more than usual here. Which part of the MMR takes the worst of it "
            "depends on where the bands come ashore: a track 30 km north or "
            "south moves the maximum between Palghar, the suburbs and Alibag."
        )
    elif peak.basin == "Bay of Bengal":
        westward = brg is not None and (240 <= brg <= 330)
        relevance = "moderate" if westward else "low"
        headline = (f"{peak.intensity.capitalize()} over the Bay of Bengal, "
                    f"{direction}")
        reasoning = (
            "Guide s12.3: a Bay system can affect Mumbai without its centre "
            "coming close. As it moves inland it strengthens the cross-country "
            "pressure gradient and the Arabian Sea westerlies, deepens the "
            "moisture and produces broad ascent over central India — which can "
            "weaken the usual Pune rain shadow and give widespread rain rather "
            "than a purely coastal orographic event."
            if westward else
            "This system is not tracking toward the peninsula, so its effect "
            "on the Konkan flow should stay limited. Worth watching only if "
            "the track turns west-northwest."
        )
    elif peak.basin == "Land" and (closest.distance_km < 700
                                   or (track.approaching
                                       and closest.distance_km < 1200)):
        relevance = "moderate"
        near = closest.distance_km < 700
        headline = (f"{peak.intensity.capitalize()} inland over the peninsula, "
                    f"{direction}" if near else
                    f"{peak.intensity.capitalize()} inland, {direction} and "
                    f"closing to ~{closest.distance_km:.0f} km")
        reasoning = (
            "An inland circulation this close can produce broad ascent over "
            "Maharashtra directly. Guide Case Study E: when ascent and moisture "
            "are both deep, rain becomes widespread and longer-lasting than a "
            "coastal orographic event, and Pune can get caught up in it too."
            if near else
            "The centre stays some distance away, but it is closing — and "
            "Guide s12.3 warns against dismissing a system on distance alone: "
            "a low tracking west-northwest across the peninsula strengthens "
            "the cross-country gradient and the Arabian Sea westerlies behind "
            "it, dragging a renewed surge of moisture up the coast. Examine "
            "the circulation footprint, not the centre."
        )
    else:
        relevance = "low"
        headline = f"{peak.intensity.capitalize()} over {peak.basin}, {direction}"
        reasoning = ("Too far away, and not tracking toward the region, to "
                     "change the Konkan flow materially.")

    return SystemAssessment(track=track, relevance=relevance,
                            headline=headline, reasoning=reasoning)


# --------------------------------------------------------------------------
# Offshore trough, from the 2D field
# --------------------------------------------------------------------------

@dataclass
class OffshoreTrough2D:
    present: bool
    axis_lat: float | None
    depth_hpa: float | None
    length_deg: float
    note: str


# Dedicated fine grid for the offshore trough. The feature is a narrow
# north-south low line parallel to the coast; the 2-degree synoptic grid cannot
# resolve it, so it gets its own 0.5-degree strip.
# 1-degree in latitude, 0.5 in longitude. The trough is a narrow north-south
# feature, so longitude is where the resolution has to be spent; sampling
# latitude at 0.5 doubled the request count for no extra detection power and
# was enough on its own to trip the API rate limit.
COAST_LATS = tuple(12.0 + 1.0 * i for i in range(13))     # 12N .. 24N
COAST_LONS = tuple(68.0 + 0.5 * i for i in range(15))     # 68E .. 75E


def fetch_coastal_strip(days: int = 3, *, model: str = "ecmwf_ifs025",
                        quiet: bool = True) -> PressureField | None:
    """Higher-resolution MSL pressure over the Konkan offshore strip."""
    times: list[str] = []
    pressure: dict[tuple[float, float], list[float | None]] = {}
    wind: dict[tuple[float, float], list[float | None]] = {}

    for lat in COAST_LATS:
        params = {
            "latitude": ",".join(str(lat) for _ in COAST_LONS),
            "longitude": ",".join(str(lon) for lon in COAST_LONS),
            "hourly": "pressure_msl",
            "models": model, "forecast_days": days,
            "timezone": C.TIMEZONE,
        }
        try:
            raw = _get_json(C.FORECAST_URL, params, timeout=90)
        except Exception as exc:                  # noqa: BLE001
            if not quiet:
                print(f"  ! coastal strip {lat}N failed: {exc}")
            continue
        if not isinstance(raw, list):
            raw = [raw]
        for lon, loc in zip(COAST_LONS, raw):
            hourly = loc.get("hourly", {})
            if not times:
                times = hourly.get("time", [])
            pressure[(lat, lon)] = hourly.get("pressure_msl", [])
            wind[(lat, lon)] = []

    if not times:
        return None
    return PressureField(lats=list(COAST_LATS), lons=list(COAST_LONS),
                         times=times, pressure=pressure, wind=wind)


def offshore_trough_2d(field: PressureField, t: int) -> OffshoreTrough2D:
    """
    Detect the offshore trough as a north-south pressure minimum line running
    parallel to the west coast (Guide s12.1, Handbook Ch.13).

    The test that matters is an INTERIOR minimum in longitude: a genuine
    trough has higher pressure both to its west over the open sea and to its
    east over land. A field that merely slopes down toward the coast is the
    monsoon pressure gradient, not a trough - and mistaking one for the other
    manufactures an offshore trough on every single monsoon day.
    """
    coast_lats = [l for l in field.lats if 12.0 <= l <= 24.0]
    sea_lons = [l for l in field.lons if 66.0 <= l <= 75.0]
    if len(coast_lats) < 3 or len(sea_lons) < 3:
        return OffshoreTrough2D(False, None, None, 0.0,
                                "Grid does not cover the offshore strip.")

    depths: list[tuple[float, float]] = []
    for lat in coast_lats:
        row = [(lon, field.p(lat, lon, t)) for lon in sea_lons]
        row = [(lon, p) for lon, p in row if p is not None]
        if len(row) < 3:
            continue
        min_lon, min_p = min(row, key=lambda kv: kv[1])
        # interior minimum only - an edge minimum is just the field sloping
        if min_lon in (row[0][0], row[-1][0]):
            continue
        surround = sum(p for _, p in row) / len(row)
        depths.append((lat, surround - min_p))

    strong = [(lat, d) for lat, d in depths if d >= 0.4]
    if len(strong) < 3:
        return OffshoreTrough2D(
            False, None, None, 0.0,
            "No coherent offshore trough along the Konkan line. Coastal "
            "convergence forcing is correspondingly weaker, and rain will "
            "depend more on the broad monsoon flow than on a local trough.")

    axis_lat = sum(lat for lat, _ in strong) / len(strong)
    depth = max(d for _, d in strong)
    length = max(lat for lat, _ in strong) - min(lat for lat, _ in strong)
    return OffshoreTrough2D(
        True, axis_lat, depth, length,
        f"Offshore trough running roughly {min(l for l, _ in strong):.0f}–"
        f"{max(l for l, _ in strong):.0f}°N ({length:.0f}° of latitude), "
        f"deepest {depth:.1f} hPa below the surrounding field. Guide s12.1: "
        "this promotes convergence, organises coastal convection and helps "
        "maintain the onshore flow.")


# --------------------------------------------------------------------------
# Top-level
# --------------------------------------------------------------------------

@dataclass
class SystemsPicture:
    assessments: list[SystemAssessment]
    trough: OffshoreTrough2D
    cyclone_window: bool
    times: list[str]

    @property
    def significant(self) -> list[SystemAssessment]:
        return [a for a in self.assessments if a.relevance in ("high", "moderate")]


def analyse(days: int = 7, *, today: date | None = None,
            quiet: bool = True) -> SystemsPicture | None:
    today = today or date.today()
    field = fetch_pressure_field(days=days, quiet=quiet)
    if field is None:
        return None

    tracks = track_systems(field)
    assessments = [assess(t, field.times) for t in tracks]
    order = {"high": 0, "moderate": 1, "low": 2, "background": 3}
    assessments.sort(key=lambda a: (order[a.relevance], -a.track.peak.depth))

    # The offshore trough gets its own finer grid; the synoptic grid is too
    # coarse to resolve it and falling back to it produces false positives.
    strip = fetch_coastal_strip(days=min(days, 3), quiet=quiet)
    trough_field = strip or field
    t_idx = min(12, len(trough_field.times) - 1)

    return SystemsPicture(
        assessments=assessments[:8],
        trough=offshore_trough_2d(trough_field, t_idx),
        cyclone_window=today.month in C.CYCLONE_WATCH_MONTHS,
        times=field.times,
    )


def render(sp: SystemsPicture | None) -> str:
    if sp is None:
        return "_Synoptic field unavailable this run._\n"

    out = f"**Offshore trough.** {sp.trough.note}\n\n"

    sig = sp.significant
    if not sig:
        out += ("**Low pressure systems.** No significant low pressure area or "
                "depression is tracked within range over the next week. Rain, "
                "if any, will be driven by the broad monsoon flow and terrain "
                "rather than by an organised system.\n\n")
    else:
        out += "**Low pressure systems tracked.**\n\n"
        for a in sig:
            tr = a.track
            first_t = datetime.fromisoformat(sp.times[tr.first.time_index])
            out += (f"- **{a.headline}** ({a.relevance} relevance)  \n"
                    f"  Centre first resolved {first_t:%a %d %b} near "
                    f"{tr.first.lat:.0f}°N {tr.first.lon:.0f}°E, "
                    f"{tr.first.distance_km:.0f} km from Mumbai; "
                    f"moves {tr.moved_km:.0f} km over the tracked period. "
                    f"Minimum pressure {tr.peak.pressure:.0f} hPa.  \n"
                    f"  {a.reasoning}\n")
        out += "\n"

    if sp.cyclone_window:
        out += ("> **Cyclone season.** This month falls in an Arabian Sea "
                "cyclone window (Handbook Ch.17). Handbook is unambiguous here: "
                "for anything beyond casual tracking, IMD's official cyclone "
                "bulletins are authoritative and this tool is not. Lives and "
                "evacuation decisions depend on those bulletins.\n\n")
    return out


# --------------------------------------------------------------------------
# Basin outlook - where the week's lows form, and which sea they belong to
# --------------------------------------------------------------------------
#
# WHY THE BASIN MATTERS MORE THAN THE DISTANCE
# --------------------------------------------
# The instinct is that a low in the Arabian Sea, being the near one, matters
# most to Kalyan. For monsoon rainfall that instinct is usually wrong, and
# Guide s12.3 says so plainly: a Bay system can soak Mumbai while its centre
# is still a thousand kilometres away over Chhattisgarh.
#
#   BAY OF BENGAL. The workhorse. Lows form in the north Bay, come ashore
#   near Odisha and track west-northwest along the monsoon trough. As one
#   crosses central India it drags the trough south and strengthens the
#   westerly flow feeding Konkan, so the heaviest Kalyan spells often arrive
#   two to four days AFTER the low has made landfall on the other coast.
#
#   ARABIAN SEA. Two-faced. A low sitting just off the Konkan is direct heavy
#   rain. But one that forms and pulls away west-northwest toward Oman does
#   the opposite - it takes the moisture with it and can leave the coast
#   drier than before. Which of the two is happening is a question about the
#   track, not the depth, which is why the direction of travel is reported
#   here even when the system is weak.
#
# Genesis timing is taken from the first frame a track appears in. A system
# already present at hour zero is reported as present rather than forming,
# because "a low forms on Monday" reads very differently from "the low that
# is already there is still there on Monday".

BASINS = ("Arabian Sea", "Bay of Bengal")

# Windy views for checking the model against the live map. Windy's own free
# point API returns deliberately scrambled data, so these are links for the
# eye, not a data source - the numbers here come from Open-Meteo.
BASIN_WINDY = {
    "Arabian Sea": ("https://www.windy.com/?ecmwf,pressure,15.000,66.000,5",
                    "https://www.windy.com/?ecmwf,wind,850h,15.000,66.000,5"),
    "Bay of Bengal": ("https://www.windy.com/?ecmwf,pressure,17.000,88.000,5",
                      "https://www.windy.com/?ecmwf,wind,850h,17.000,88.000,5"),
}


@dataclass
class BasinReport:
    basin: str
    status: str               # quiet | watch | developing | active
    headline: str
    detail: str
    tracks: list[SystemTrack]
    genesis_day: str = ""     # e.g. "Sun 23 Aug", blank if already present
    peak_depth: float = 0.0
    peak_label: str = ""
    closest_km: int | None = None
    windy_pressure: str = ""
    windy_wind: str = ""


def _day_label(times: Sequence[str], idx: int) -> str:
    """Turn a time index into 'Sat 23 Aug'."""
    if idx < 0 or idx >= len(times):
        return ""
    try:
        return datetime.fromisoformat(times[idx]).strftime("%a %d %b")
    except ValueError:
        return ""


def _track_direction(tr: SystemTrack) -> str:
    b = tr.track_bearing
    if b is None:
        return "barely moving"
    return f"tracking {compass16(b)}"


def basin_outlook(sp: SystemsPicture | None) -> list[BasinReport]:
    """One report per sea, describing what forms there over the week."""
    if sp is None:
        return []

    out: list[BasinReport] = []
    for basin in BASINS:
        # A track belongs to the basin it spends its deepest moment in - a
        # Bay low that later crosses India is still a Bay system, and
        # classifying it by where it ends would file it under "Land".
        #
        # No is_transient test here, deliberately. That test exists to stop
        # the stationary heat low and the monsoon trough being reported as
        # systems, and now that _basin() follows the real coastline those sit
        # on Land and never reach this list. Keeping it would have hidden the
        # case this section is for: a low that forms over the sea and sits
        # there deepening for a day before it starts moving.
        mine = [a.track for a in sp.assessments
                if a.track.peak.basin == basin and a.track.peak.depth >= 1.0]
        mine.sort(key=lambda t: -t.peak.depth)

        rep = BasinReport(
            basin=basin, status="quiet", tracks=mine,
            headline=f"**{basin} — nothing organised this week.**",
            detail=("No closed low forms here in the current run. That is the "
                    "ordinary state; most weeks have none."),
            windy_pressure=BASIN_WINDY[basin][0],
            windy_wind=BASIN_WINDY[basin][1],
        )

        if mine:
            lead = mine[0]
            formed_at = lead.first.time_index
            already = formed_at <= 6
            rep.genesis_day = "" if already else _day_label(sp.times, formed_at)
            rep.peak_depth = lead.peak.depth
            rep.peak_label = lead.peak.intensity
            rep.closest_km = round(lead.closest_approach.distance_km)

            if lead.peak.depth >= 3.0:
                rep.status = "active"
            elif lead.peak.depth >= 1.5:
                rep.status = "developing"
            else:
                rep.status = "watch"

            # No inner ** here: the whole headline is already bold, and
            # nesting emphasis inside emphasis renders as literal asterisks
            # rather than as bold-inside-bold.
            when = ("already present" if already
                    else f"forming around {rep.genesis_day}")
            rep.headline = f"**{basin} — {rep.peak_label}, {when}.**"

            bits = [f"Deepest about **{lead.peak.depth:.1f} hPa** below its "
                    f"surroundings near {lead.peak.lat:.0f}°N "
                    f"{lead.peak.lon:.0f}°E, {_track_direction(lead)}, "
                    f"closest approach to Kalyan about "
                    f"**{rep.closest_km:,} km**."]

            if basin == "Bay of Bengal":
                bits.append(
                    "A Bay system does its work on Konkan from a distance: as "
                    "it tracks west-northwest along the monsoon trough it pulls "
                    "the trough south and strengthens the westerlies feeding "
                    "this coast. Expect any rainfall response here **two to "
                    "four days after** it crosses the east coast, not while it "
                    "is still over water.")
            else:
                b = lead.track_bearing
                away = b is not None and (b >= 260 or b <= 20)
                if away:
                    bits.append(
                        "It is heading away from the coast. An Arabian Sea low "
                        "that pulls off to the west-northwest takes the "
                        "moisture with it, so this can leave Konkan **drier**, "
                        "not wetter — the opposite of what its nearness "
                        "suggests.")
                else:
                    bits.append(
                        "It stays in the eastern Arabian Sea, which is the "
                        "configuration that brings **direct** heavy rain to the "
                        "Konkan coast rather than the delayed response a Bay "
                        "system gives.")

            if len(mine) > 1:
                bits.append(f"{len(mine) - 1} weaker circulation"
                            f"{'s' if len(mine) > 2 else ''} also appear"
                            f"{'' if len(mine) > 2 else 's'} in this basin "
                            "during the week.")
            rep.detail = " ".join(bits)

        out.append(rep)
    return out


def render_basins(reports: Sequence[BasinReport], *,
                  cyclone_window: bool = False) -> str:
    """Markdown for the weekly bulletin."""
    if not reports:
        return ""
    out = ("**Where this week's lows form — Arabian Sea or Bay of Bengal**\n\n")
    for r in reports:
        out += f"{r.headline}\n\n{r.detail}\n\n"
        out += (f"> Check it live on Windy: "
                f"[pressure]({r.windy_pressure}) · "
                f"[850 hPa wind]({r.windy_wind}). The map is the eye check; "
                f"the numbers above come from ECMWF via Open-Meteo.\n\n")
    if cyclone_window:
        out += ("> This is one of the two Arabian Sea cyclone windows "
                "(Handbook Ch.17). Anything organising here deserves IMD's "
                "own bulletin, not this page — cyclone calls are theirs to "
                "make.\n\n")
    return out
