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

# WHAT THE GRID CAN AND CANNOT SAY ABOUT MOTION
# ---------------------------------------------
# Two degrees puts adjacent grid points about 210 km apart at these latitudes,
# so a detected centre's position is only known to roughly half that, and any
# displacement smaller than one grid step is not a measurement of movement -
# it is the grid. Every claim this module makes about whether a system is
# moving is gated on the two constants below.
#
# The alternative is what this module used to do: a low resolved on a single
# day reported "moves 0 km over the tracked period" and labelled
# "quasi-stationary", which asserts a physical fact out of an absence of
# evidence. On 9 Sep 2026 that turned one westward-marching Bay low into three
# unconnected stationary centres in the bulletin.
MOTION_FLOOR_KM = 200.0

# Least time a track must span before its displacement means anything. Below
# this the low has not been watched long enough to say whether it travels.
MOTION_MIN_HOURS = 18.0

# DIRECTNESS: net displacement divided by the length of the path walked.
#
# Net displacement alone cannot tell a system that travelled from one that
# wandered. Repairing the track linking made this bite immediately: on
# 12 Sep 2026 the page carried six near-identical alerts, one labelled
# "tracking SE" for a low that made a single hop and then sat at 16N 77E for
# 120 hours, and another labelled "tracking WSW" for a centre that went
# 26N -> 22N -> back to 26N -> 22N and then parked. First-to-last bearing
# described none of them.
#
# It also let the Pakistan/Rajasthan heat low through the seasonal filter:
# 162 hours of wandering around 26-28N 65-71E that happened to END 222 km
# from where it STARTED, which is just past the "this travels" threshold.
#
# A real travelling low goes roughly one way. Below this ratio the track is a
# wandering centre, and neither a direction nor systemhood is claimed from it.
MIN_DIRECTNESS = 0.55

# Slowest a centre can average and still be called "tracking" somewhere.
#
# Directness alone does not catch the other failure: a track that makes ONE
# hop and then sits still scores a perfect 1.0, because its single leg is also
# its net displacement. The 12 Sep 2026 bulletin labelled such a centre
# "tracking SE" when it had moved once and then parked at 16N 77E for 120
# hours - an average of 2.4 km/h.
#
# A monsoon low that is genuinely on the move manages 15-30 km/h. The floor
# here is well below that so only the parked cases fall through it.
MIN_TRACK_SPEED_KMH = 4.0

# Fastest a monsoon low is assumed to travel, used to widen the search radius
# across a gap in detection. Well above the 20-30 km/h these systems actually
# manage, because the cost of missing a link is a fragmented track and the
# cost of a slightly loose one is bounded by MAX_REACH_KM.
MAX_TRACK_SPEED_KMH = 35.0
MAX_REACH_KM = 1100.0

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
    def duration_hours(self) -> float:
        """Hours between the first and last frame this centre was resolved in."""
        return float(self.last.time_index - self.first.time_index)

    @property
    def path_km(self) -> float:
        """Total ground covered, summed leg by leg."""
        return sum(haversine(a.lat, a.lon, b.lat, b.lon)
                   for a, b in zip(self.positions, self.positions[1:]))

    @property
    def directness(self) -> float:
        """Net displacement as a fraction of the path walked. 1.0 is a straight
        line; a centre that returns to where it started approaches 0."""
        path = self.path_km
        if path <= 0:
            return 0.0
        return self.moved_km / path

    @property
    def mean_speed_kmh(self) -> float:
        """Net displacement over the whole tracked period."""
        if self.duration_hours <= 0:
            return 0.0
        return self.moved_km / self.duration_hours

    @property
    def motion(self) -> str:
        """
        How much can honestly be said about this track's displacement.

        'moving'     - went at least one grid step, one way, at a real pace
        'wandering'  - covered ground but ended near where it started
        'slow'       - went under one grid step, or averaged barely any speed
        'unresolved' - not watched long enough to say either way
        """
        if len(self.positions) < 2 or self.duration_hours < MOTION_MIN_HOURS:
            return "unresolved"
        if self.moved_km < MOTION_FLOOR_KM:
            return "slow"
        if self.directness < MIN_DIRECTNESS:
            return "wandering"
        if self.mean_speed_kmh < MIN_TRACK_SPEED_KMH:
            return "slow"
        return "moving"

    @property
    def motion_phrase(self) -> str:
        """Words for the motion state - never asserts more than `motion` knows."""
        m = self.motion
        if m == "moving":
            brg = self.track_bearing
            return f"tracking {compass16(brg)}" if brg is not None else "moving"
        if m == "wandering":
            return "drifting without a settled direction"
        if m == "slow":
            return "moving slowly"
        return "motion not yet resolved"

    @property
    def track_bearing(self) -> float | None:
        # Two gates, and both are needed. The floor is one grid step because
        # below that the displacement is quantisation. The directness test is
        # because a bearing drawn from first to last describes a wandering
        # centre no better than a coin toss would.
        if len(self.positions) < 2 or self.moved_km < MOTION_FLOOR_KM:
            return None
        if self.directness < MIN_DIRECTNESS:
            return None
        if self.mean_speed_kmh < MIN_TRACK_SPEED_KMH:
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

        Net displacement is not enough on its own. The heat low wanders a
        thousand kilometres over a week and can finish 222 km from where it
        began, which clears a bare distance test while being the very thing
        the test exists to exclude. A system that travels also goes one way.
        """
        return (self.moved_km >= MOTION_FLOOR_KM
                and self.directness >= MIN_DIRECTNESS
                and self.mean_speed_kmh >= MIN_TRACK_SPEED_KMH)

    @property
    def is_seasonal_feature(self) -> bool:
        """Quasi-stationary and sitting in the monsoon-trough / heat-low belt.

        The duration test matters as much as the distance one. A low resolved
        for a few hours has not been shown to be stationary, only unobserved,
        and filtering it out as "seasonal background" would silently drop a
        genuine new system that happened to form in the trough belt.
        """
        return (self.duration_hours >= 36.0
                and not self.is_transient
                and self.peak.lat >= 25.0)


def track_systems(field: PressureField, *, hours: Sequence[int] | None = None,
                  search_radius_km: float = 450.0,
                  max_gap_hours: float = 30.0) -> list[SystemTrack]:
    """
    Link low centres across time into tracks by nearest-neighbour matching.

    TWO THINGS THIS HAS TO GET RIGHT, AND ORIGINALLY DID NOT
    --------------------------------------------------------
    A low DROPS OUT of detection. `find_lows` needs a centre strictly lower
    than every neighbour on a 2 degree ring, and a real low pressure area
    embedded in the monsoon trough fails that intermittently - the trough's
    own gradient swallows the closure, particularly while the centre is
    crossing the coast and the land/sea pressure contrast distorts the field.
    The track goes quiet for one frame or several.

    Matching only against a fixed 450 km radius meant that by the time the low
    reappeared it had travelled beyond reach, so it started a NEW track. On
    9 Sep 2026 that reported a single westward-marching Bay low - genesis off
    the Odisha coast, then Vidarbha two days later - as three unconnected
    centres, two of them labelled "quasi-stationary", and left the basin card
    quoting a closest approach of 1,246 km for a system that came inland to
    within about 620 km.

    So the search radius GROWS WITH THE GAP: a system unobserved for 24 hours
    has had 24 hours to travel. A track is abandoned once the gap passes
    `max_gap_hours`, which stops that same generosity from welding two
    genuinely different systems into one.

    Matching is also done NEAREST PAIR FIRST rather than in depth order. The
    old loop walked `find_lows` output, which is sorted deepest-first, and let
    the deepest low of a frame claim a track that belonged to a closer,
    shallower one - swapping two systems' identities from that frame on.
    """
    if hours is None:
        hours = list(range(0, min(len(field.times), 168), 6))

    tracks: list[SystemTrack] = []
    for t in hours:
        lows = find_lows(field, t)
        if not lows:
            continue

        # Every (low, track) pair within reach, then resolved cheapest-first
        # so the best available match wins regardless of iteration order.
        cands: list[tuple[float, int, SystemTrack]] = []
        for li, low in enumerate(lows):
            for tr in tracks:
                gap = float(t - tr.last.time_index)
                if gap <= 0 or gap > max_gap_hours:
                    continue
                reach = max(search_radius_km,
                            min(MAX_REACH_KM, MAX_TRACK_SPEED_KMH * gap))
                d = haversine(tr.last.lat, tr.last.lon, low.lat, low.lon)
                if d <= reach:
                    cands.append((d, li, tr))
        cands.sort(key=lambda c: c[0])

        claimed_low: set[int] = set()
        claimed_track: set[int] = set()
        for _d, li, tr in cands:
            if li in claimed_low or id(tr) in claimed_track:
                continue
            tr.positions.append(lows[li])
            claimed_low.add(li)
            claimed_track.add(id(tr))

        for li, low in enumerate(lows):
            if li not in claimed_low:
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
    # Never "quasi-stationary" from a short track: see MOTION_FLOOR_KM.
    direction = track.motion_phrase

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

    # Classify on WHERE THE SYSTEM IS WHEN IT MATTERS, not where it is deepest.
    # Peak depth often happens at the far end of a long track: the 9 Sep 2026
    # system was deepest out over the Arabian Sea on its way to Oman, days
    # after its closest approach to Kalyan over Saurashtra. Headlining that as
    # "in the Arabian Sea, closest approach 307 km" would have put a system
    # offshore and upstream when it was actually inland and departing.
    origin = _origin_basin(track)
    if closest.basin == "Arabian Sea" and closest.distance_km < 500:
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
    elif origin == "Bay of Bengal":
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
    elif closest.basin == "Land" and (closest.distance_km < 700
                                      or (track.approaching
                                          and closest.distance_km < 1200)):
        # A low crossing the peninsula inside 400 km is not a "moderate"
        # curiosity - it puts its own ascent directly over Maharashtra.
        relevance = "high" if closest.distance_km < 400 else "moderate"
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
        f"about {depth:.1f} hPa lower than the air either side of it. "
        "A trough like this squeezes the moist sea air together and nudges it "
        "upward, which is what turns a damp wind into real rain clouds. It "
        "also keeps the wind blowing steadily onshore instead of slackening.")


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

    # Computed once in analyse(): clustering walks the pressure field, so a
    # property that re-ran it would repeat that work for every section that
    # asks. THIS IS THE LIST EVERY OTHER SECTION SHOULD USE - `significant` is
    # raw detector output, and one broad low routinely appears in it several
    # times over.
    events: list[SystemEvent] = field(default_factory=list)


# --------------------------------------------------------------------------
# Grouping detections into EVENTS
# --------------------------------------------------------------------------
#
# THE BUG THIS EXISTS FOR
# -----------------------
# On 12 Sep 2026 the alert list read:
#
#   [PREPARE] Well-marked low ... moving slowly          241 km, 1008 hPa
#   [PREPARE] Well-marked low ... drifting, no direction 307 km, 1006 hPa
#   [BE AWARE] Low ... tracking WNW                      506 km, 1005 hPa
#
# Three alerts, all "inland over the peninsula", all closest on the SAME DAY,
# separated by a couple of hundred kilometres. That is not three systems. It
# is one broad low-pressure area that a 2 degree grid resolves as several
# closed centres, which is the normal state of a monsoon trough: a synoptic
# LPA is 500-1000 km across and easily holds more than one local minimum.
#
# Reporting them separately is wrong twice over. It triples the apparent
# number of threats, and it hides the thing that actually matters - that the
# models disagree about WHERE THE SAME LOW SITS, by 265 km. That disagreement
# is a forecast uncertainty and belongs in the open, as the range of
# possibilities for one event.
#
# Two centres closer than this, seen at the same hour, are treated as parts of
# one area. It is deliberately near the low end of synoptic LPA width: the
# cost of splitting one system is the bug above, and the cost of merging two
# genuinely separate ones is a sub-point saying they might be distinct.
CLUSTER_KM = 600.0

# Linking is single-linkage, so A-B and B-C merge A and C even when A and C
# are far apart. Across a monsoon trough that is usually RIGHT - the trough
# genuinely is one continuous low-pressure axis - but an event spanning this
# much ground should be described as an axis rather than as "a low pressure
# area", because the reader would otherwise picture one compact centre.
AXIS_KM = 1300.0

# Coarse place names, checked in order, first box wins. The point is only to
# make two different events DISTINGUISHABLE in a headline: before this, two
# unrelated lows a thousand kilometres apart both read "inland over the
# peninsula" and looked like the same alert printed twice.
_PLACES: tuple[tuple[float, float, float, float, str], ...] = (
    (20.0, 26.0, 66.0, 72.0, "Kutch and Saurashtra"),
    (26.0, 33.0, 66.0, 72.0, "Sindh and the Thar"),
    (20.0, 25.0, 72.0, 75.0, "Gujarat"),
    (24.0, 30.0, 72.0, 78.0, "Rajasthan"),
    (21.0, 26.0, 74.0, 80.0, "Madhya Pradesh"),
    (21.0, 26.0, 80.0, 84.0, "east Madhya Pradesh and Chhattisgarh"),
    (18.5, 22.0, 76.0, 81.0, "Vidarbha"),
    (17.0, 21.0, 73.5, 76.0, "north Maharashtra"),
    (16.5, 20.0, 76.0, 79.0, "Marathwada and Telangana"),
    (13.0, 17.0, 74.0, 78.5, "interior Karnataka"),
    (13.0, 18.0, 78.5, 82.0, "Telangana and coastal Andhra"),
    (8.0, 13.5, 76.0, 81.0, "Tamil Nadu and south Karnataka"),
    (17.0, 23.0, 82.0, 87.0, "Odisha"),
    (23.0, 28.0, 84.0, 90.0, "Bihar and north Bengal"),
    (24.0, 29.0, 78.0, 84.0, "the Gangetic plain"),
    (21.0, 25.0, 87.0, 90.0, "Bengal and Bangladesh"),
    (22.0, 29.0, 90.0, 95.0, "the northeast"),
)


def _place_name(lat: float, lon: float) -> str:
    """Where a centre is, in words a reader can picture."""
    for la0, la1, lo0, lo1, name in _PLACES:
        if la0 <= lat < la1 and lo0 <= lon < lo1:
            return name
    b = _basin(lat, lon)
    if b == "Arabian Sea":
        return "the eastern Arabian Sea" if lon >= EASTERN_ARABIAN_SEA_LON \
            else "the western Arabian Sea"
    if b == "Bay of Bengal":
        return "the north Bay" if lat >= 18 else "the central Bay"
    return f"{lat:.0f}°N {lon:.0f}°E"


@dataclass
class SystemEvent:
    """One weather event, however many centres the detector resolved in it."""
    members: list[SystemAssessment]
    times: list[str] = field(default_factory=list)

    @property
    def lead(self) -> SystemAssessment:
        """The member that decides how the event is described and ranked.

        The one that comes CLOSEST, not the deepest: the question an alert
        answers is whether this reaches the reader, and the nearest centre is
        the one that settles it.
        """
        return min(self.members,
                   key=lambda a: a.track.closest_approach.distance_km)

    @property
    def relevance(self) -> str:
        for level in ("high", "moderate", "low"):
            if any(m.relevance == level for m in self.members):
                return level
        return "low"

    @property
    def closest(self) -> LowCentre:
        return self.lead.track.closest_approach

    @property
    def min_pressure(self) -> float:
        return min(m.track.peak.pressure for m in self.members)

    @property
    def max_pressure(self) -> float:
        return max(m.track.peak.pressure for m in self.members)

    @property
    def distance_span(self) -> tuple[float, float]:
        d = [m.track.closest_approach.distance_km for m in self.members]
        return (min(d), max(d))

    @property
    def split(self) -> bool:
        return len(self.members) > 1

    @property
    def extent_km(self) -> float:
        """How far apart the furthest two centres in this event sit."""
        pts = [m.track.closest_approach for m in self.members]
        return max((haversine(a.lat, a.lon, b.lat, b.lon)
                    for i, a in enumerate(pts) for b in pts[i + 1:]),
                   default=0.0)

    @property
    def place(self) -> str:
        return _place_name(self.closest.lat, self.closest.lon)

    @property
    def bearing_word(self) -> str:
        return compass16(bearing(REF_LAT, REF_LON,
                                 self.closest.lat, self.closest.lon))

    @property
    def headline(self) -> str:
        """A headline that says WHERE, so two different events cannot read as
        the same alert printed twice - which is what "low pressure area inland
        over the peninsula", emitted three times, looked like."""
        ca = self.closest
        if self.extent_km > AXIS_KM:
            far = max(self.members,
                      key=lambda m: m.track.closest_approach.distance_km)
            fp = _place_name(far.track.closest_approach.lat,
                             far.track.closest_approach.lon)
            return (f"Low pressure axis running from {fp} to {self.place} — "
                    f"nearest centre {ca.distance_km:,.0f} km to the "
                    f"{self.bearing_word}")
        return (f"{self.lead.track.peak.intensity.capitalize()} over "
                f"{self.place} — {ca.distance_km:,.0f} km to the "
                f"{self.bearing_word}")

    @property
    def reasoning(self) -> str:
        if self.extent_km > AXIS_KM:
            return (
                "This is the monsoon trough itself rather than one compact "
                "low: a continuous belt of low pressure with several "
                "circulations strung along it. Guide s12.3 still applies — "
                "what reaches this coast is the flow the belt sets up, not "
                "the distance to any single centre. " + self.lead.reasoning)
        return self.lead.reasoning

    def closest_day(self) -> str:
        idx = self.closest.time_index
        return _day_label(self.times, idx) if self.times else ""

    def possibilities(self) -> list[str]:
        """The sub-points: how this one event could resolve.

        Empty when the detector found a single centre - there is nothing
        uncertain to report and a lone bullet reading "the models agree" is
        noise.
        """
        if not self.split:
            return []
        lo, hi = self.distance_span
        out = [
            f"**The models do not agree where its centre sits.** They resolve "
            f"it as {len(self.members)} circulations between "
            f"**{lo:,.0f} km** and **{hi:,.0f} km** from Kalyan — one broad "
            f"area, not {len(self.members)} separate systems. A low pressure "
            f"area is several hundred kilometres across, so a grid this coarse "
            f"picks out more than one centre inside the same feature."
        ]
        for m in sorted(self.members,
                        key=lambda a: a.track.closest_approach.distance_km):
            ca = m.track.closest_approach
            when = _day_label(self.times, ca.time_index) if self.times else ""
            out.append(
                f"One centre **{ca.distance_km:,.0f} km** out, "
                f"{m.track.motion_phrase}, down to "
                f"{m.track.peak.pressure:.0f} hPa"
                + (f", closest on {when}" if when else "") + ".")
        if self.max_pressure - self.min_pressure >= 2.0:
            out.append(
                f"**Depth is unsettled too** — {self.min_pressure:.0f} to "
                f"{self.max_pressure:.0f} hPa across those centres. Take the "
                "deeper end as the reasonable worst case rather than the "
                "expected one.")
        out.append(
            "**What to watch.** If these centres consolidate into one, the "
            "system gets better organised and its rain becomes heavier and "
            "more concentrated. If they stay separate, expect rain spread "
            "more thinly over a wider area.")
        return out


# How much pressure may rise BETWEEN two centres before they count as two
# separate lows rather than two minima inside one area.
#
# Distance alone gets this wrong, and measurably so. Grouping purely on a
# 600 km radius merged pairs that had a 1.4-1.5 hPa ridge standing between
# them - larger than the depth of either low relative to its own surroundings,
# which is the textbook definition of two separate circulations separated by a
# col. It also merged pairs with only a 0.4 hPa rise between them, which
# genuinely are one broad area.
#
# So the test is the one a forecaster does by eye on a chart: walk the line
# from one centre to the other and see whether you have to climb over anything
# to get there.
COL_HPA = 1.0


def _col_between(field: PressureField, a: LowCentre, b: LowCentre,
                 t: int, samples: int = 9) -> float | None:
    """How far pressure rises between two centres, above the shallower one.

    Positive means a ridge stands between them. Near zero means the low
    pressure is continuous and they are two minima in one area.
    """
    vals: list[float] = []
    for i in range(samples):
        f = i / (samples - 1)
        lat = a.lat + (b.lat - a.lat) * f
        lon = a.lon + (b.lon - a.lon) * f
        la = min(field.lats, key=lambda x: abs(x - lat))
        lo = min(field.lons, key=lambda x: abs(x - lon))
        v = field.p(la, lo, t)
        if v is None:
            return None
        vals.append(v)
    return max(vals[1:-1]) - max(vals[0], vals[-1])


def cluster_events(assessments: Sequence[SystemAssessment],
                   times: Sequence[str],
                   field: PressureField | None = None) -> list[SystemEvent]:
    """Group detections that are really the same weather event.

    Two tracks join when all three hold:

      * they were BOTH resolved at the same hour - two lows that pass through
        the same place a week apart are two systems, and only comparing
        positions at the same moment can tell them apart;
      * their centres sit within CLUSTER_KM at that hour;
      * and no ridge worth the name stands between them (see COL_HPA). This
        is the test that actually decides it. Without it the grouping merged
        centres with 1.5 hPa of high pressure sitting in the gap.

    The col is taken as the MEDIAN across every qualifying hour, not the
    minimum: one hour where two lows briefly appear joined is noise on a
    2 degree grid, a persistent connection is a shared circulation.
    """
    items = list(assessments)
    n = len(items)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    pos = [{p.time_index: p for p in a.track.positions} for a in items]
    for i in range(n):
        for j in range(i + 1, n):
            shared = sorted(pos[i].keys() & pos[j].keys())
            near = [t for t in shared
                    if haversine(pos[i][t].lat, pos[i][t].lon,
                                 pos[j][t].lat, pos[j][t].lon) <= CLUSTER_KM]
            if not near:
                continue
            if field is None:
                # No field to walk: fall back to distance alone, which is what
                # this did before the col test and is better than nothing.
                union(i, j)
                continue
            cols = [c for c in (_col_between(field, pos[i][t], pos[j][t], t)
                                for t in near) if c is not None]
            if not cols:
                continue
            cols.sort()
            median = cols[len(cols) // 2] if len(cols) % 2 else \
                (cols[len(cols) // 2 - 1] + cols[len(cols) // 2]) / 2
            if median < COL_HPA:
                union(i, j)

    groups: dict[int, list[SystemAssessment]] = {}
    for i, a in enumerate(items):
        groups.setdefault(find(i), []).append(a)

    events = [SystemEvent(members=g, times=list(times))
              for g in groups.values()]
    events.sort(key=lambda e: e.closest.distance_km)
    return events


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

    kept = assessments[:8]
    significant = [a for a in kept if a.relevance in ("high", "moderate")]
    return SystemsPicture(
        assessments=kept,
        trough=offshore_trough_2d(trough_field, t_idx),
        cyclone_window=today.month in C.CYCLONE_WATCH_MONTHS,
        times=field.times,
        events=cluster_events(significant, field.times, field),
    )


def render(sp: SystemsPicture | None) -> str:
    if sp is None:
        return "_Synoptic field unavailable this run._\n"

    out = f"**Offshore trough.** {sp.trough.note}\n\n"

    # Events, not raw detections: the same grouping the alerts use, so the two
    # sections cannot disagree about how many systems there are.
    sig = sp.events
    if not sig:
        out += ("**Low pressure systems.** No significant low pressure area or "
                "depression is tracked within range over the next week. Rain, "
                "if any, will be driven by the broad monsoon flow and terrain "
                "rather than by an organised system.\n\n")
    else:
        out += "**Low pressure systems tracked.**\n\n"
        for a in sig:
            tr = a.lead.track
            first_t = datetime.fromisoformat(sp.times[tr.first.time_index])
            # Say what the displacement actually establishes. "moves 0 km"
            # reads as a finding; usually it means the low has been resolved
            # for too little time, or has not yet crossed a grid cell.
            if tr.motion == "unresolved":
                move = (f"resolved over {tr.duration_hours:.0f} h so far — too "
                        "short a window to say whether it is moving")
            elif tr.motion == "slow":
                move = (f"moves less than one grid cell in "
                        f"{tr.duration_hours:.0f} h, so it is genuinely slow")
            elif tr.motion == "wandering":
                move = (f"covers {tr.path_km:.0f} km of ground in "
                        f"{tr.duration_hours:.0f} h but ends only "
                        f"{tr.moved_km:.0f} km from where it began, so it is "
                        "milling about rather than travelling")
            else:
                move = (f"moves {tr.moved_km:.0f} km over "
                        f"{tr.duration_hours:.0f} h")
            # Closest approach is the number that decides whether a system
            # matters here, so it belongs in the text and not only in the
            # web card - the headline omits it whenever the system is near.
            ca = a.closest
            ca_day = a.closest_day()
            out += (f"- **{a.headline}** ({a.relevance} relevance)  \n"
                    f"  Nearest centre first resolved {first_t:%a %d %b} near "
                    f"{tr.first.lat:.0f}°N {tr.first.lon:.0f}°E, "
                    f"{tr.first.distance_km:.0f} km from Mumbai; "
                    f"{move}. Closest approach about "
                    f"**{ca.distance_km:,.0f} km**"
                    + (f" on {ca_day}" if ca_day else "")
                    + f". Minimum pressure {a.min_pressure:.0f} hPa.  \n"
                    f"  {a.reasoning}\n")
            # The centres inside this one event, and what their disagreement
            # means - never as separate list entries.
            for pt in a.possibilities():
                out += f"    - {pt}\n"
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

# IMD splits the Arabian Sea into east/west sub-basins near this longitude.
# East of it a low sits upstream of the Konkan; west of it the same low is
# closer to Oman than to Mumbai and does the opposite job.
EASTERN_ARABIAN_SEA_LON = 68.0

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
    return tr.motion_phrase



# How much of a track's opening counts as "where it formed". Beyond this the
# centre is somewhere it travelled to, not somewhere it came from.
GENESIS_WINDOW_HOURS = 24.0


def _origin_basin(tr: SystemTrack) -> str:
    """The sea a track formed over.

    Only the OPENING of the track counts. Scanning the whole track for the
    first marine basin it ever touches sounds equivalent and is not: a system
    that forms over land and exits to sea days later gets filed under the sea
    it left by, which reverses the direction of the story.

    That is not hypothetical. Once track linking was repaired on 9 Sep 2026,
    the dominant feature ran Bengal -> Vidarbha -> Saurashtra -> out over the
    Arabian Sea, and the whole-track scan filed it as an Arabian Sea system -
    i.e. as something arriving from the west, when it was in fact crossing the
    country from the east and leaving.

    A low first picked up a little inland near its genesis still belongs to
    the sea it came out of, which is what the window preserves.
    """
    t0 = tr.first.time_index
    for p in tr.positions:
        if p.time_index - t0 > GENESIS_WINDOW_HOURS:
            break
        if p.basin in BASINS:
            return p.basin
    return tr.first.basin


def basin_outlook(sp: SystemsPicture | None) -> list[BasinReport]:
    """One report per sea, describing what forms there over the week."""
    if sp is None:
        return []

    out: list[BasinReport] = []
    for basin in BASINS:
        # A track belongs to the sea it FORMED over, not the one it happens to
        # be deepest in.
        #
        # Classifying by the deepest moment was the previous attempt, and it
        # fails on the most important case there is: a Bay low that deepens as
        # it comes ashore. On 30 Aug 2026 a low sat at 22N 89E - correctly
        # labelled Bay of Bengal - and tracked west to 22N 87E, deepening from
        # 1.3 to 2.9 hPa on the way. Its deepest point was over land, so the
        # whole system was filed under "Land" and the page announced "Bay of
        # Bengal - nothing organised this week" while a 996 hPa low was the
        # main story of the week.
        #
        # Genesis is the honest label: a system that forms over the Bay is a
        # Bay system for its whole life, which is also how IMD names them.
        #
        # No is_transient test here, deliberately. That test exists to stop
        # the stationary heat low and the monsoon trough being reported as
        # systems, and now that _basin() follows the real coastline those sit
        # on Land and never reach this list. Keeping it would have hidden the
        # case this section is for: a low that forms over the sea and sits
        # there deepening for a day before it starts moving.
        mine = [a.track for a in sp.assessments
                if _origin_basin(a.track) == basin and a.track.peak.depth >= 1.0]
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
                # The west-northwest sentence is the mechanism, not a label -
                # it only applies to a system actually going that way. Printed
                # unconditionally it told readers a low "tracks west-northwest"
                # in the same breath as the line above said "tracking N".
                b = lead.track_bearing
                if b is not None and 240 <= b <= 330:
                    bits.append(
                        "A Bay system does its work on Konkan from a distance: "
                        "as it tracks west-northwest along the monsoon trough "
                        "it pulls the trough south and strengthens the "
                        "westerlies feeding this coast. Expect any rainfall "
                        "response here **two to four days after** it crosses "
                        "the east coast, not while it is still over water.")
                elif lead.motion == "unresolved":
                    bits.append(
                        "Its direction of travel is not yet resolved, so what "
                        "it means for this coast is still open. A Bay low only "
                        "reaches Konkan by running **west** across the "
                        "peninsula, dragging the monsoon trough south behind "
                        "it; one that stalls or turns away does nothing here. "
                        "Watch the track for a day before reading anything "
                        "into it.")
                else:
                    bits.append(
                        "It is **not** running west across the peninsula, and "
                        "that is the only route by which a Bay low reaches "
                        "this coast. Unless the track turns west-northwest it "
                        "will not drag the monsoon trough any further south, "
                        "and Konkan should feel little from it.")
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
                elif lead.peak.lon >= EASTERN_ARABIAN_SEA_LON:
                    bits.append(
                        "It stays in the eastern Arabian Sea, which is the "
                        "configuration that brings **direct** heavy rain to the "
                        "Konkan coast rather than the delayed response a Bay "
                        "system gives.")
                else:
                    # Said "eastern Arabian Sea" for a centre at 65E before
                    # this test existed - which is the far side of the basin,
                    # nearer Oman than Mumbai, and the opposite situation.
                    bits.append(
                        "This sits in the **western** half of the Arabian Sea, "
                        "the far side of the basin from us. A low out there "
                        "draws moisture toward Oman and Pakistan rather than "
                        "onto the Konkan, so despite being an Arabian Sea "
                        "system it is not an approaching one — treat it as "
                        "background unless the track turns east.")

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
