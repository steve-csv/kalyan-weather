"""
Diagnostics - the meteorology of the two guides, expressed as computation.

The organising principle is Guide's "learning rule" (s0):

    "Never begin with the rain layer. First identify the weather mechanism.
     Ask: Where is the moisture? What is lifting the air? Is the atmosphere
     stable or unstable? Is the rain organised enough to persist? Only then
     examine the model rainfall output."

Every function below answers one of those questions. The rainfall numbers are
computed last, in `daily_rain_spread`, and are deliberately never reduced to a
single mechanically-averaged figure (Guide s24, Case Study F).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Sequence

from . import config as C
from .sources import EnsembleForecast, ModelSeries, PointForecast, PressureTransect


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _clean(values: Sequence[float | None]) -> list[float]:
    return [v for v in values if v is not None and not math.isnan(v)]


def _mean(values: Sequence[float | None]) -> float | None:
    vals = _clean(values)
    return sum(vals) / len(vals) if vals else None


def _maxi(values: Sequence[float | None]) -> float | None:
    vals = _clean(values)
    return max(vals) if vals else None


def wind_components(speed: float | None, direction: float | None
                    ) -> tuple[float, float] | None:
    """
    Meteorological direction ('from') -> (u eastward, v northward), m/s.
    """
    if speed is None or direction is None:
        return None
    rad = math.radians(direction)
    return (-speed * math.sin(rad), -speed * math.cos(rad))


def orographic_component(speed: float | None, direction: float | None,
                         normal_deg: float = C.GHAT_UPSLOPE_NORMAL_DEG
                         ) -> float | None:
    """
    Component of the wind directed straight at the Ghat face, in m/s.

    Guide s11.1: "A west or west-southwest wind aimed directly at the Ghats
    maximises upslope lifting... a wind nearly parallel to the mountain chain
    produces less direct lift at a given location. Therefore, 'strong wind' is
    not enough; examine its direction relative to the terrain."

    Positive = upslope forcing. Zero = flow parallel to the ridge. Negative =
    offshore / downslope (subsidence, the Foehn side of Handbook Ch.14).
    """
    if speed is None or direction is None:
        return None
    return speed * math.cos(math.radians(direction - normal_deg))


def compass(direction: float | None) -> str:
    if direction is None:
        return "--"
    points = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return points[int((direction % 360) / 22.5 + 0.5) % 16]


def band_lookup(value: float | None,
                bands: Sequence[tuple[float, float, str, str]]
                ) -> tuple[str, str]:
    if value is None:
        return ("unknown", "No data.")
    for low, high, label, meaning in bands:
        if low <= value < high:
            return (label, meaning)
    return (bands[-1][2], bands[-1][3])


def imd_category(mm: float | None) -> tuple[str, str]:
    return band_lookup(mm, C.IMD_BANDS)


def cape_category(j_per_kg: float | None) -> tuple[str, str]:
    return band_lookup(j_per_kg, C.CAPE_BANDS)


def dewpoint_feel(td: float | None) -> str:
    if td is None:
        return "unknown"
    for low, high, label in C.DEWPOINT_BANDS:
        if low <= td < high:
            return label
    return C.DEWPOINT_BANDS[-1][2]


def convective_cloud_base_m(temp: float | None, dew: float | None) -> float | None:
    """
    Guide s4.3: cloud base (m) ~ 125 x (Temperature - Dew point).

    A rough rule for surface-based convective cloud only; monsoon layer cloud
    and forced orographic cloud behave differently.
    """
    if temp is None or dew is None:
        return None
    return max(0.0, 125.0 * (temp - dew))


# --------------------------------------------------------------------------
# Time indexing
# --------------------------------------------------------------------------

def parse_times(times: Sequence[str]) -> list[datetime]:
    return [datetime.fromisoformat(t) for t in times]


def day_slices(times: Sequence[str]) -> dict[date, list[int]]:
    """Group hourly indices by local calendar day."""
    out: dict[date, list[int]] = {}
    for i, t in enumerate(times):
        d = datetime.fromisoformat(t).date()
        out.setdefault(d, []).append(i)
    return out


def window_indices(times: Sequence[str], day: date,
                   start_hour: int, end_hour: int) -> list[int]:
    """Indices falling inside [start_hour, end_hour) on a given local day."""
    return [
        i for i, t in enumerate(times)
        if (dt := datetime.fromisoformat(t)).date() == day
        and start_hour <= dt.hour < end_hour
    ]


# --------------------------------------------------------------------------
# 1. MOISTURE - "Where is the moisture?"
# --------------------------------------------------------------------------

@dataclass
class MoistureProfile:
    rh_925: float | None
    rh_850: float | None
    rh_700: float | None
    rh_500: float | None
    pwat: float | None
    dew_point: float | None
    temperature: float | None
    depth_class: str = ""
    interpretation: str = ""

    @property
    def spread(self) -> float | None:
        if self.temperature is None or self.dew_point is None:
            return None
        return self.temperature - self.dew_point

    @property
    def cloud_base_m(self) -> float | None:
        return convective_cloud_base_m(self.temperature, self.dew_point)


def moisture_profile(ms: ModelSeries, idx: Sequence[int]) -> MoistureProfile:
    """
    Guide s4.4: "compare relative humidity at 925, 850 and 700 hPa. If the air
    is humid near the surface but dry at 700 hPa, clouds may remain shallow or
    entrain dry air and weaken. If humidity is high through 700 hPa, widespread
    and persistent rain becomes more plausible."
    """
    pick = lambda f: _mean([ms.at(f, i) for i in idx])  # noqa: E731

    mp = MoistureProfile(
        rh_925=pick("relative_humidity_925hPa"),
        rh_850=pick("relative_humidity_850hPa"),
        rh_700=pick("relative_humidity_700hPa"),
        rh_500=pick("relative_humidity_500hPa"),
        pwat=pick("total_column_integrated_water_vapour"),
        dew_point=pick("dew_point_2m"),
        temperature=pick("temperature_2m"),
    )

    r700, r850 = mp.rh_700, mp.rh_850
    if r700 is None or r850 is None:
        mp.depth_class = "unknown"
        mp.interpretation = "Insufficient pressure-level humidity data."
        return mp

    if r700 >= C.RH_DEEP_700 and r850 >= C.RH_MOIST_850:
        mp.depth_class = "deep"
        mp.interpretation = (
            "Moisture is deep through 700 hPa. This supports widespread and "
            "persistent rain wherever lift exists, not just shallow showers."
        )
    elif r700 >= C.RH_MODERATE_700:
        mp.depth_class = "moderate"
        mp.interpretation = (
            "Moisture depth is moderate. Cloud can grow reasonably deep but "
            "some dry-air entrainment aloft is likely to limit the heaviest "
            "totals."
        )
    elif r850 >= C.RH_MOIST_850:
        mp.depth_class = "shallow"
        mp.interpretation = (
            "Moist near the surface but dry at 700 hPa. Guide s4.4: clouds are "
            "likely to stay shallow or entrain dry air and weaken - expect low "
            "cloud, drizzle or brief showers rather than sustained rain."
        )
    else:
        mp.depth_class = "dry"
        mp.interpretation = (
            "The column is dry through the mid levels. Rain is unlikely "
            "without a substantial change in the airmass."
        )
    return mp


# --------------------------------------------------------------------------
# 2. LIFT - "What is lifting the air?"
# --------------------------------------------------------------------------

@dataclass
class LiftProfile:
    wind_850_speed: float | None
    wind_850_dir: float | None
    wind_925_speed: float | None
    wind_925_dir: float | None
    orographic_850: float | None
    orographic_925: float | None
    forcing_class: str = ""
    interpretation: str = ""

    @property
    def dir_label(self) -> str:
        return compass(self.wind_850_dir)


def lift_profile(ms: ModelSeries, idx: Sequence[int]) -> LiftProfile:
    """
    The orographic engine of Handbook Ch.14 / Guide s7.1, scored numerically.

    Handbook Ch.22 Step 4: "A steady 15-20 knot onshore (westerly-to-
    southwesterly) flow at both levels confirms the orographic engine from
    Chapter 14 is loaded."
    """
    pick = lambda f: _mean([ms.at(f, i) for i in idx])  # noqa: E731

    lp = LiftProfile(
        wind_850_speed=pick("wind_speed_850hPa"),
        wind_850_dir=_circular_mean([ms.at("wind_direction_850hPa", i) for i in idx]),
        wind_925_speed=pick("wind_speed_925hPa"),
        wind_925_dir=_circular_mean([ms.at("wind_direction_925hPa", i) for i in idx]),
        orographic_850=None,
        orographic_925=None,
    )
    lp.orographic_850 = orographic_component(lp.wind_850_speed, lp.wind_850_dir)
    lp.orographic_925 = orographic_component(lp.wind_925_speed, lp.wind_925_dir)

    comp = lp.orographic_850
    if comp is None:
        lp.forcing_class = "unknown"
        lp.interpretation = "No 850 hPa wind data."
        return lp

    if comp >= C.OROG_VERY_STRONG:
        lp.forcing_class = "very strong"
        lp.interpretation = (
            "The orographic engine is running hard. A deep, fast westerly "
            "current is being forced directly up the Ghat face - persistent "
            "forced ascent, heavy windward totals, strong coastal enhancement."
        )
    elif comp >= C.OROG_STRONG:
        lp.forcing_class = "strong"
        lp.interpretation = (
            "Strong onshore flow aimed close to square-on at the Ghats. The "
            "orographic engine is loaded; expect repeated spells rather than "
            "isolated showers."
        )
    elif comp >= C.OROG_MODERATE:
        lp.forcing_class = "moderate"
        lp.interpretation = (
            "Moderate onshore component - the Handbook Ch.22 '15-20 knot' "
            "confirmation is roughly met. Terrain lift is contributing but is "
            "not the dominant driver on its own."
        )
    elif comp >= C.OROG_WEAK:
        lp.forcing_class = "weak"
        lp.interpretation = (
            "Weak onshore component. Terrain lift alone is unlikely to sustain "
            "rain; any significant rain will need convergence, heating or a "
            "synoptic system to do the lifting."
        )
    elif comp >= 0:
        lp.forcing_class = "negligible"
        lp.interpretation = (
            "Flow is near-parallel to the ridge. Guide s11.1: a wind parallel "
            "to the mountain chain produces little direct lift regardless of "
            "its speed."
        )
    else:
        lp.forcing_class = "offshore"
        lp.interpretation = (
            "Offshore / downslope component. Air is descending and warming "
            "(the Foehn process of Handbook Ch.14) - actively rain-suppressing."
        )
    return lp


def orographic_reading(component: float | None, zone: str) -> tuple[str, str]:
    """
    Interpret the terrain-normal component according to which side of the
    crest the site sits on.

    This distinction is the whole of Handbook Ch.14. The same westerly that is
    forced UP the windward slope at Matheran is the air DESCENDING the leeward
    slope at Pune, "warming and drying at the faster DALR... the Foehn effect
    operating at the scale of an entire mountain range". A raw positive
    component therefore means opposite things at the two sites, and reporting
    it unqualified would invert the rain-shadow logic.
    """
    if component is None:
        return ("--", "No wind data.")

    if zone == "leeward":
        if component >= C.OROG_MODERATE:
            return (f"{component:+.1f} (descent)",
                    "Strong westerly crossing the crest and descending here - "
                    "warming, drying, actively rain-suppressing. Rain shadow "
                    "intact.")
        if component >= C.OROG_WEAK:
            return (f"{component:+.1f} (descent)",
                    "Moderate downslope flow. Rain shadow operating, though "
                    "spillover is possible if moisture is deep.")
        if component >= 0:
            return (f"{component:+.1f} (weak)",
                    "Little cross-crest flow - the rain shadow is weakly "
                    "enforced. Local convection can dominate instead.")
        return (f"{component:+.1f} (easterly)",
                "Flow reversed - air arriving from the east rather than "
                "crossing the Ghats. The usual rain-shadow logic does not "
                "apply today.")

    if zone == "ghat":
        if component >= C.OROG_STRONG:
            return (f"{component:+.1f} (upslope)",
                    "Sustained forced ascent on the windward face - cloud "
                    "immersion and heavy, persistent totals likely.")
        if component >= C.OROG_WEAK:
            return (f"{component:+.1f} (upslope)",
                    "Moderate forced ascent on the crest.")
        return (f"{component:+.1f} (weak)",
                "Little direct upslope forcing despite the elevation.")

    # coastal / transition - the windward approach
    if component >= C.OROG_STRONG:
        return (f"{component:+.1f} (onshore)",
                "Strong onshore flow feeding the orographic engine.")
    if component >= C.OROG_WEAK:
        return (f"{component:+.1f} (onshore)",
                "Moderate onshore flow.")
    if component >= 0:
        return (f"{component:+.1f} (parallel)",
                "Flow near-parallel to the coast/ridge - little direct lift.")
    return (f"{component:+.1f} (offshore)",
            "Offshore component - subsidence, rain-suppressing.")


def _circular_mean(directions: Sequence[float | None]) -> float | None:
    """Vector-average wind directions; a plain mean is wrong across 360/0."""
    vals = _clean(directions)
    if not vals:
        return None
    sin_sum = sum(math.sin(math.radians(d)) for d in vals)
    cos_sum = sum(math.cos(math.radians(d)) for d in vals)
    if abs(sin_sum) < 1e-9 and abs(cos_sum) < 1e-9:
        return None
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def coastal_convergence(sea: PointForecast | None, coast: PointForecast | None,
                        model_id: str, idx: Sequence[int]) -> float | None:
    """
    Crude convergence proxy: the drop in the terrain-normal wind component
    between an offshore point and the coast, in m/s.

    Guide s7.3: "Convergence means horizontal winds move toward the same area.
    Air cannot accumulate near the surface indefinitely, so it rises."
    A positive value means the onshore flow is decelerating as it makes
    landfall - i.e. mass is piling up and must ascend.
    """
    if sea is None or coast is None:
        return None
    if model_id not in sea.models or model_id not in coast.models:
        return None
    sea_lp = lift_profile(sea.models[model_id], idx)
    coast_lp = lift_profile(coast.models[model_id], idx)
    if sea_lp.orographic_925 is None or coast_lp.orographic_925 is None:
        return None
    return sea_lp.orographic_925 - coast_lp.orographic_925


# --------------------------------------------------------------------------
# 3. INSTABILITY - "Is the atmosphere stable or unstable?"
# --------------------------------------------------------------------------

@dataclass
class StabilityProfile:
    cape_mean: float | None
    cape_peak: float | None
    cin_mean: float | None
    shear_925_500: float | None
    cape_class: str = ""
    cape_meaning: str = ""
    shear_class: str = ""
    storm_mode: str = ""


def stability_profile(ms: ModelSeries, idx: Sequence[int]) -> StabilityProfile:
    """
    Handbook Ch.24: CAPE says how much energy is available if something
    triggers it; shear says what kind of storm you get if it fires.
    """
    cape_vals = [ms.at("cape", i) for i in idx]
    sp = StabilityProfile(
        cape_mean=_mean(cape_vals),
        cape_peak=_maxi(cape_vals),
        cin_mean=_mean([ms.at("convective_inhibition", i) for i in idx]),
        shear_925_500=_bulk_shear(ms, idx),
    )
    sp.cape_class, sp.cape_meaning = cape_category(sp.cape_peak)

    shear = sp.shear_925_500
    if shear is None:
        sp.shear_class = "unknown"
        sp.storm_mode = "Storm mode cannot be assessed without upper wind data."
    elif shear < C.SHEAR_LOW:
        sp.shear_class = "low"
        sp.storm_mode = (
            "Low shear. Any storm is likely to be a short-lived 'pulse' cell - "
            "intense but disorganised, collapsing on its own outflow within an "
            "hour or two and not travelling far."
        )
    elif shear < C.SHEAR_MODERATE:
        sp.shear_class = "moderate"
        sp.storm_mode = (
            "Moderate shear. Storms can sustain themselves somewhat longer and "
            "may travel; watch for cells regenerating along outflow boundaries."
        )
    else:
        sp.shear_class = "strong"
        sp.storm_mode = (
            "Strong shear. Favours longer-lived, organised, faster-moving "
            "storms or squall lines - less common here but more dangerous when "
            "it happens."
        )
    return sp


def _bulk_shear(ms: ModelSeries, idx: Sequence[int]) -> float | None:
    """Magnitude of the 925 -> 500 hPa wind vector difference, m/s."""
    us_lo, vs_lo, us_hi, vs_hi = [], [], [], []
    for i in idx:
        lo = wind_components(ms.at("wind_speed_925hPa", i),
                             ms.at("wind_direction_925hPa", i))
        hi = wind_components(ms.at("wind_speed_500hPa", i),
                             ms.at("wind_direction_500hPa", i))
        if lo and hi:
            us_lo.append(lo[0]); vs_lo.append(lo[1])
            us_hi.append(hi[0]); vs_hi.append(hi[1])
    if not us_lo:
        return None
    du = sum(us_hi) / len(us_hi) - sum(us_lo) / len(us_lo)
    dv = sum(vs_hi) / len(vs_hi) - sum(vs_lo) / len(vs_lo)
    return math.hypot(du, dv)


# --------------------------------------------------------------------------
# 4. ORGANISATION - "Is the rain organised enough to persist?"
# --------------------------------------------------------------------------

@dataclass
class OrganisationProfile:
    wet_hours: int
    max_hourly: float | None
    total: float | None
    longest_run_h: int
    cloud_low: float | None
    cloud_mid: float | None
    character: str = ""
    note: str = ""


def organisation_profile(ms: ModelSeries, idx: Sequence[int]) -> OrganisationProfile:
    """
    Guide s8.1: showers begin and end suddenly and come from convective cloud;
    intermittent rain falls from layer cloud with breaks; continuous rain
    persists without a significant break.

    "Mumbai monsoon rain is often mixed. A broad area of layered cloud may
    produce steady rain, while embedded convective cells create short periods
    of much higher intensity."
    """
    precip = [ms.at("precipitation", i) or 0.0 for i in idx]
    wet = [p >= 0.2 for p in precip]

    longest = run = 0
    for w in wet:
        run = run + 1 if w else 0
        longest = max(longest, run)

    op = OrganisationProfile(
        wet_hours=sum(wet),
        max_hourly=max(precip) if precip else None,
        total=sum(precip),
        longest_run_h=longest,
        cloud_low=_mean([ms.at("cloud_cover_low", i) for i in idx]),
        cloud_mid=_mean([ms.at("cloud_cover_mid", i) for i in idx]),
    )

    total = op.total or 0.0
    peak = op.max_hourly or 0.0
    if total < 1.0:
        op.character = "essentially dry"
        op.note = "No meaningful precipitation signal."
    elif longest >= 6 and peak < C.HEAVY_SPELL_MM_PER_H:
        op.character = "continuous / steady"
        op.note = (
            "Long unbroken run of modest hourly rates - the signature of deep "
            "layered cloud under broad ascent rather than convective cells."
        )
    elif longest >= 4 and peak >= C.HEAVY_SPELL_MM_PER_H:
        op.character = "organised with embedded heavy bursts"
        op.note = (
            "A sustained rain area with intense cores inside it. Guide s8.1: "
            "the daily total will likely be dominated by a few intense bursts "
            "even though rain occurs for many hours."
        )
    elif op.wet_hours >= 3:
        op.character = "intermittent spells"
        op.note = "Repeated spells with breaks between them."
    else:
        op.character = "isolated showers"
        op.note = (
            "Short, sharply-bounded signal - showery, spatially uneven, and "
            "the least predictable at suburb level."
        )
    return op


# --------------------------------------------------------------------------
# 5. RAINFALL - computed last, on purpose
# --------------------------------------------------------------------------

@dataclass
class RainSpread:
    """Per-model totals for one day. Deliberately not collapsed to one number."""
    per_model: dict[str, float]
    lo: float
    hi: float
    median: float
    agree_on_occurrence: int
    n_models: int
    category_lo: str
    category_hi: str
    scenario_note: str = ""

    @property
    def occurrence_fraction(self) -> float:
        return self.agree_on_occurrence / self.n_models if self.n_models else 0.0

    @property
    def categories_agree(self) -> bool:
        return self.category_lo == self.category_hi


def daily_rain_spread(pf: PointForecast, idx: Sequence[int],
                      threshold: float = C.MEASURABLE_RAIN_MM) -> RainSpread:
    """
    Guide s24 / Case Study F, the beginner trap stated explicitly:

        "Avoid this: Publishing the arithmetic mean of the three model totals
         as though it were a probability-weighted forecast."

    So we report the RANGE and the median, we report occurrence agreement
    separately from amount agreement, and we never present a single mean as
    "the forecast".
    """
    per_model: dict[str, float] = {}
    for ms in pf.models.values():
        per_model[ms.label] = sum(ms.at("precipitation", i) or 0.0 for i in idx)

    vals = list(per_model.values()) or [0.0]
    lo, hi = min(vals), max(vals)
    med = statistics.median(vals)
    agree = sum(1 for v in vals if v >= threshold)

    rs = RainSpread(
        per_model=per_model, lo=lo, hi=hi, median=med,
        agree_on_occurrence=agree, n_models=len(vals),
        category_lo=imd_category(lo)[0], category_hi=imd_category(hi)[0],
    )

    if rs.n_models > 1 and hi > 0 and (hi - lo) > max(10.0, 0.6 * hi):
        rs.scenario_note = (
            "The models disagree materially on amount. Guide Case Study F: "
            "this disagreement is usually physically meaningful - it means the "
            "outcome hinges on something small and badly-resolved, such as the "
            "track of an offshore vortex or where exactly convection fires. "
            "The honest response is to separate the robust part of the forecast "
            "(does it rain) from the uncertain part (how much)."
        )
    return rs


@dataclass
class EnsembleProbability:
    p_measurable: float | None
    p_heavy: float | None
    median_mm: float | None
    p90_mm: float | None
    n_members: int


def ensemble_probability(ens: EnsembleForecast | None, times: Sequence[str],
                         day: date) -> EnsembleProbability | None:
    """
    True probability of exceedance from the ensemble (Guide s13.2).
    Falls back to None so callers can degrade to model-count agreement.
    """
    if ens is None:
        return None
    idx = [i for i, t in enumerate(ens.times)
           if datetime.fromisoformat(t).date() == day]
    if not idx:
        return None

    totals: list[float] = []
    for member in ens.members:
        totals.append(sum((member[i] or 0.0) for i in idx if i < len(member)))
    if not totals:
        return None

    n = len(totals)
    totals_sorted = sorted(totals)
    return EnsembleProbability(
        p_measurable=sum(1 for t in totals if t >= C.MEASURABLE_RAIN_MM) / n,
        p_heavy=sum(1 for t in totals if t >= C.HEAVY_DAY_MM) / n,
        median_mm=statistics.median(totals),
        p90_mm=totals_sorted[min(n - 1, int(0.9 * n))],
        n_members=n,
    )


# --------------------------------------------------------------------------
# 6. SYNOPTIC - trough diagnosis from the pressure field
# --------------------------------------------------------------------------

@dataclass
class TroughDiagnosis:
    axis_lat: float | None
    min_pressure: float | None
    phase: str
    interpretation: str


def monsoon_trough(transect: PressureTransect | None, hour_idx: int = 12
                   ) -> TroughDiagnosis:
    """
    Locate the monsoon trough axis as the latitude of minimum MSL pressure
    along a north-south transect over the Indian landmass.

    Handbook Ch.13: "A trough hugging the foothills for several consecutive
    days is your break-monsoon signal for Mumbai and Pune... a trough sitting
    back over the plains, especially with a depression visible forming over the
    Bay of Bengal, is your active-spell signal."
    """
    if transect is None or not transect.pressures:
        return TroughDiagnosis(None, None, "unknown",
                               "Pressure transect unavailable.")

    best_lat = best_p = None
    for (lat, _lon), series in zip(transect.points, transect.pressures):
        if hour_idx >= len(series):
            continue
        p = series[hour_idx]
        if p is None:
            continue
        if best_p is None or p < best_p:
            best_p, best_lat = p, lat

    if best_lat is None:
        return TroughDiagnosis(None, None, "unknown",
                               "No usable pressure values in transect.")

    if best_lat >= 28.0:
        phase = "break-leaning"
        interp = (
            "The trough axis is displaced north toward the Himalayan foothills. "
            "Handbook Ch.13: this is the break-monsoon signal - rain "
            "concentrates in the sub-Himalayan belt while the west coast, "
            "Mumbai and Pune can turn surprisingly dry, sometimes for a week."
        )
    elif best_lat >= 25.0:
        phase = "transitional"
        interp = (
            "The trough sits between its normal and foothill positions. "
            "Expect a mixed regime - neither a clean active surge nor a clean "
            "break. Guide s10.3: models often disagree most on timing during "
            "these transitions, so observations matter more than usual."
        )
    else:
        phase = "active-leaning"
        interp = (
            "The trough is in or south of its normal position over the plains. "
            "Handbook Ch.13: this is the active-spell configuration - "
            "cross-equatorial flow strong, rain widespread and often heavy "
            "across the west coast."
        )
    return TroughDiagnosis(best_lat, best_p, phase, interp)


@dataclass
class OffshoreTroughDiagnosis:
    present: bool
    strength_hpa: float | None
    axis_lat: float | None
    cross_coast_gradient: float | None
    interpretation: str


def offshore_trough(offshore: PressureTransect | None,
                    inland: PressureTransect | None,
                    hour_idx: int = 12) -> OffshoreTroughDiagnosis:
    """
    Cross-coast pressure gradient only. NOT trough detection.

    An earlier version of this function searched a single north-south line off
    the coast for its pressure minimum and reported that as the offshore trough
    axis. That is structurally wrong. Through the monsoon, pressure decreases
    monotonically northward along that line toward the monsoon trough, so the
    minimum is always the northernmost sample, and the "depth" it computed
    (edge mean minus minimum) was just half the north-south monsoon gradient.
    It therefore reported an offshore trough on essentially every monsoon day,
    with a spurious axis pinned to the end of the line.

    A genuine offshore trough is an INTERIOR minimum in longitude - higher
    pressure to its west over the open sea AND to its east over land. That
    needs a 2D field at finer spacing than a single meridian, and it now lives
    in systems.offshore_trough_2d().

    What this function still legitimately provides is the cross-coast pressure
    gradient of Guide s2.3, which is a real and useful quantity.
    """
    if offshore is None or inland is None:
        return OffshoreTroughDiagnosis(
            False, None, None, None,
            "Cross-coast pressure sampling unavailable this run.")

    sea, land = [], []
    for series in offshore.pressures:
        if hour_idx < len(series) and series[hour_idx] is not None:
            sea.append(series[hour_idx])
    for series in inland.pressures:
        if hour_idx < len(series) and series[hour_idx] is not None:
            land.append(series[hour_idx])
    if not sea or not land:
        return OffshoreTroughDiagnosis(
            False, None, None, None,
            "Too few pressure samples for the cross-coast gradient.")

    gradient = (sum(sea) / len(sea)) - (sum(land) / len(land))
    if gradient > 0:
        note = (
            f"Cross-coast pressure gradient (sea minus inland) is "
            f"{gradient:+.1f} hPa — pressure falls inland, which strengthens "
            "the westerly moisture transport toward the Konkan (Guide s2.3). "
            "Trough detection itself is handled on a finer 2D grid; see the "
            "systems section."
        )
    else:
        note = (
            f"Cross-coast pressure gradient (sea minus inland) is "
            f"{gradient:+.1f} hPa — the inland heat low is not pulling flow "
            "onshore, weakening moisture transport (Guide s2.3)."
        )
    return OffshoreTroughDiagnosis(False, None, None, gradient, note)


# --------------------------------------------------------------------------
# 7. Regime classification - the synthesis
# --------------------------------------------------------------------------

@dataclass
class DayDiagnosis:
    day: date
    lead_hours: int
    moisture: MoistureProfile
    lift: LiftProfile
    stability: StabilityProfile
    organisation: OrganisationProfile
    rain: RainSpread
    ensemble: EnsembleProbability | None
    regime: str = ""
    regime_note: str = ""
    mechanism: str = ""


def classify_regime(moist: MoistureProfile, lift: LiftProfile,
                    stab: StabilityProfile, org: OrganisationProfile,
                    season: str) -> tuple[str, str, str]:
    """
    Name the mechanism before naming the outcome - Guide s12 'system-thinking
    rule': system -> wind response -> moisture transport -> lifting zone ->
    expected rain footprint.

    Returns (regime, note, mechanism).
    """
    orog = lift.orographic_850 or 0.0
    deep = moist.depth_class == "deep"
    moderate_moist = moist.depth_class in ("deep", "moderate")
    cape_peak = stab.cape_peak or 0.0

    if season == "monsoon":
        if orog >= C.OROG_STRONG and deep:
            return (
                "ACTIVE MONSOON SURGE",
                "Strong terrain-normal westerlies meeting deep moisture. This "
                "is the fully-loaded orographic engine of Handbook Ch.14.",
                "Deep moist WSW current -> forced ascent on the windward Ghats "
                "and coastal convergence -> widespread, repeated, persistent "
                "rain. Ghat crest heavily enhanced; Kalyan sits in the "
                "transition belt and picks up both coastal bands and terrain "
                "enhancement.",
            )
        if orog >= C.OROG_MODERATE and moderate_moist:
            # Name the factor that is actually limiting, rather than asserting
            # a generic cause that the ingredients may contradict.
            if not deep:
                limiter = (
                    "Moisture depth is the limiting factor - the column is not "
                    "humid enough at 700 hPa to sustain the deepest cloud "
                    "(Guide s4.4)."
                )
            elif orog < C.OROG_STRONG * 0.98:
                limiter = (
                    "The wind angle is the limiting factor - the flow is not "
                    "quite square-on to the Ghats, so some of its speed is "
                    "wasted running along the ridge rather than up it "
                    "(Guide s11.1)."
                )
            else:
                limiter = (
                    "Ingredients sit just below the surge thresholds; this is a "
                    "borderline case that could tip either way. Watch radar "
                    "and the 850 hPa wind angle through the day."
                )
            return (
                "MODERATE ONSHORE FLOW",
                "The orographic engine is turning over but not running hard. "
                "Ordinary wet-season rhythm.",
                f"Onshore flow supplies steady lift. {limiter} Expect repeated "
                "spells rather than a soaking, with the windward Ghats still "
                "taking noticeably more than the coast.",
            )
        if orog < C.OROG_WEAK and cape_peak >= 800:
            return (
                "BREAK-PHASE CONVECTIVE",
                "Weak onshore flow with meaningful instability - the "
                "break-monsoon pattern of Handbook Ch.13 and Ch.16.",
                "With the monsoon engine idling, sunshine returns and local "
                "heating takes over as the trigger. Rain becomes isolated, "
                "afternoon-weighted and spatially patchy - a Pune-like rhythm "
                "imposed on the Konkan.",
            )
        if orog < C.OROG_WEAK:
            return (
                "MONSOON BREAK / LULL",
                "Weak forcing and limited instability. Guide s10.3 'subdued' "
                "phase.",
                "Neither terrain nor heating is doing much lifting. Humid, "
                "cloudy, largely rainless - the frustrating kind of monsoon day "
                "where high humidity produces nothing.",
            )
        return (
            "MIXED MONSOON",
            "Ingredients are present but none dominant.",
            "Rain is plausible from a combination of modest terrain lift and "
            "embedded convection, without a single clean driver. Timing "
            "confidence is lower than in a clean surge.",
        )

    # Non-monsoon seasons run on conditional instability plus a trigger.
    if cape_peak >= 1500 and moderate_moist:
        return (
            "CONVECTIVE / THUNDERSTORM RISK",
            "Strong instability with adequate moisture - Handbook Ch.4's "
            "conditional instability waiting on a trigger.",
            "The atmosphere holds substantial buoyant energy. A sea-breeze "
            "front, foothill convergence or strong local heating can switch it "
            "on within half an hour. Where exactly it fires is genuinely "
            "unpredictable; that it can fire is not.",
        )
    if cape_peak >= 500 and moderate_moist:
        return (
            "ISOLATED CONVECTION POSSIBLE",
            "Moderate instability. Garden-variety storms plausible given a "
            "trigger.",
            "Ordinary afternoon convection. Coverage will be patchy - it is "
            "normal and correct for one part of the area to get a downpour "
            "while a few kilometres away stays dry.",
        )
    if org.total and org.total > 2:
        return (
            "SYNOPTIC / NON-SEASONAL RAIN",
            "Rain signal outside the usual seasonal mechanism.",
            "Rain here is unusual for the season and is most likely driven by a "
            "passing system aloft rather than local forcing. Worth checking the "
            "IMD bulletin for what system is responsible.",
        )
    return (
        "DRY / SETTLED",
        "No meaningful moisture, lift or instability.",
        "Subsidence or simply a dry airmass. Expect settled conditions; in "
        "winter watch instead for inversions, haze and poor morning visibility "
        "(Handbook Ch.2).",
    )



# --------------------------------------------------------------------------
# Convective burst risk - when the model's own numbers should not be believed
# --------------------------------------------------------------------------
#
# WHY THIS EXISTS
# ---------------
# On 25 August 2026 Kalyan had heavy convective bursts while every model on
# the page showed 0.3 mm/hr and ECMWF had cloud cover falling through the
# afternoon. The models were not missing the SETUP - the agent already held
# all four numbers that mattered:
#
#     CAPE ~950 J/kg    real convective energy
#     RH 850  ~90%      very moist below
#     RH 700  ~35%      very dry mid-levels
#     850 wind 9 m/s from 260 deg   dead-on upslope into the Ghats
#
# Moist below, dry aloft, decent CAPE and strong upslope flow is the classic
# recipe for short, violent, downdraught-driven cells. What the models cannot
# do is PLACE them: a cell that drops 20 mm on one town in fifteen minutes
# averages out to drizzle across a 25 km grid box, so the grid-box mean is
# honest about the total and useless about the experience.
#
# So this does not try to out-forecast the model. It flags the case where the
# model's quantitative output is known to under-describe what a person on the
# ground will feel, and says so in those terms.

BURST_CAPE_MIN = 700.0      # J/kg - enough energy for a deep cell
BURST_RH850_MIN = 80.0      # % - moist enough below to feed one
BURST_RH700_MAX = 45.0      # % - dry aloft: entrainment, downdraughts, bursts
BURST_UPSLOPE_MIN = 4.0     # m/s terrain-normal - the trigger that lifts it
BURST_QPF_MAX = 2.5         # mm/hr - the model is showing little or nothing


def burst_risk(stab, moist, lift, zone: str,
               model_peak_mm_h: float | None) -> tuple[str, str]:
    """
    Is this a day when the model's rain rate will understate the experience?

    Returns (level, note) where level is "none", "possible" or "likely".
    Only meaningful for zones the westerly actually lifts - in the rain
    shadow the same profile means subsidence, not bursts.
    """
    if zone == "leeward":
        return "none", ""

    cape = stab.cape_peak if stab else None
    rh850 = moist.rh_850 if moist else None
    rh700 = moist.rh_700 if moist else None
    upslope = lift.orographic_850 if lift else None
    if None in (cape, rh850, rh700) or upslope is None:
        return "none", ""

    ingredients = (
        cape >= BURST_CAPE_MIN
        and rh850 >= BURST_RH850_MIN
        and rh700 <= BURST_RH700_MAX
        and upslope >= BURST_UPSLOPE_MIN
    )
    if not ingredients:
        return "none", ""

    quiet_model = (model_peak_mm_h is None
                   or model_peak_mm_h <= BURST_QPF_MAX)
    level = "likely" if quiet_model else "possible"

    note = (
        f"**Burst risk.** The ingredients for short, heavy downpours are all "
        f"present: CAPE around {cape:.0f} J/kg, {rh850:.0f}% humidity at 850 "
        f"hPa with only {rh700:.0f}% at 700 hPa, and {upslope:.0f} m/s of "
        f"upslope flow into the Ghats. Moist below and dry aloft is the recipe "
        f"for cells that go up hard, rain out fast and collapse — heavy bursts "
        f"rather than steady rain."
    )
    if quiet_model:
        note += (
            " **The hourly rates on this page will understate that.** A cell "
            "that drops 20 mm on one town in fifteen minutes averages to "
            "drizzle across a 25 km grid box, so the models show a smear where "
            "you may get a soaking. Treat the numbers as the day's total, not "
            "as what any given hour will feel like, and watch the radar."
        )
    return level, note

def season_for(day: date) -> str:
    return C.SEASONS[day.month]


def lead_time_guidance(lead_hours: int) -> tuple[str, str]:
    for lo, hi, label, guidance in C.LEAD_TIME_GUIDANCE:
        if lo <= lead_hours < hi:
            return label, guidance
    return C.LEAD_TIME_GUIDANCE[-1][2], C.LEAD_TIME_GUIDANCE[-1][3]


def diagnose_day(pf: PointForecast, day: date, *,
                 ens: EnsembleForecast | None = None,
                 primary_model: str = "ecmwf_ifs025",
                 start_hour: int = 0, end_hour: int = 24,
                 now: datetime | None = None) -> DayDiagnosis | None:
    """Run the full ingredient checklist for one site on one day."""
    idx = window_indices(pf.times, day, start_hour, end_hour)
    if not idx:
        return None

    ms = pf.models.get(primary_model) or next(iter(pf.models.values()))
    now = now or datetime.now()
    lead = max(0, int((datetime.combine(day, datetime.min.time())
                       - now).total_seconds() // 3600))

    moist = moisture_profile(ms, idx)
    lift = lift_profile(ms, idx)
    stab = stability_profile(ms, idx)
    org = organisation_profile(ms, idx)
    rain = daily_rain_spread(pf, idx)
    ep = ensemble_probability(ens, pf.times, day)

    dd = DayDiagnosis(day=day, lead_hours=lead, moisture=moist, lift=lift,
                      stability=stab, organisation=org, rain=rain, ensemble=ep)
    dd.regime, dd.regime_note, dd.mechanism = classify_regime(
        moist, lift, stab, org, season_for(day))
    return dd
