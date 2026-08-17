"""
Data acquisition.

Windy (windy.com) does not run its own model - Handbook Ch.20: "it doesn't run
its own physics, it takes the output of models like ECMWF, GFS and ICON and
renders it as an interactive, animated map."

This module fetches that same underlying ECMWF / GFS / ICON output directly, at
the pressure levels the guides actually use (925 / 850 / 700 hPa), so the agent
can run the Ch.21 three-way model comparison unattended. Every bulletin also
emits Windy deep-links (see windy.py) so the map can be inspected by eye in the
way the guides teach.

If a Windy Point Forecast API key is configured it is used as a supplementary
source, but note that Windy's free tier serves only GFS over India and so
cannot by itself support the three-model comparison.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import config as C

USER_AGENT = "kalyan-wx-agent/1.0 (personal forecasting; contact: local)"

# Hourly fields requested per model. These map one-to-one onto the diagnostic
# checklist in Guide Appendix B.
HOURLY_FIELDS: tuple[str, ...] = (
    "temperature_2m",
    "dew_point_2m",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "showers",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "pressure_msl",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "cape",
    "convective_inhibition",
    "total_column_integrated_water_vapour",
    "freezing_level_height",
    # Pressure levels - Guide s1.2 "weather is three-dimensional"
    "relative_humidity_925hPa",
    "relative_humidity_850hPa",
    "relative_humidity_700hPa",
    "relative_humidity_500hPa",
    "wind_speed_925hPa",
    "wind_direction_925hPa",
    "wind_speed_850hPa",
    "wind_direction_850hPa",
    "wind_speed_700hPa",
    "wind_direction_700hPa",
    "wind_speed_500hPa",
    "wind_direction_500hPa",
    "temperature_850hPa",
    "temperature_700hPa",
    "geopotential_height_850hPa",
    "geopotential_height_500hPa",
)


class FetchError(RuntimeError):
    """Raised when a data source could not be reached after retries."""


class QuotaExhausted(FetchError):
    """
    The provider's DAILY request budget is gone - retrying cannot help today.

    Distinct from a transient 429. A per-minute burst limit clears in seconds
    and is worth backing off for; a daily quota does not clear until the reset,
    so backing off just burns 35 seconds per call and turns one clear failure
    into a very slow confusing one.
    """


def _get_json(url: str, params: dict[str, Any], *, timeout: int = 45,
              retries: int = 4) -> Any:
    """
    GET a JSON endpoint with back-off.

    HTTP 429 gets exponential back-off rather than the linear retry used for
    transient network errors: the free Open-Meteo tier throttles by request
    rate, and hammering it on a fixed short delay just burns the remaining
    budget. A run that fetches the synoptic grid, the coastal strip and a dozen
    sites can brush the limit, so this needs to actually wait.
    """
    query = urllib.parse.urlencode(params, safe=",")
    full = f"{url}?{query}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                # Separate the daily quota from a burst limit: the body says
                # which, and only one of them is worth waiting out.
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:400]
                except Exception:                 # noqa: BLE001
                    pass
                if "daily" in body.lower() or "try again tomorrow" in body.lower():
                    raise QuotaExhausted(
                        "Open-Meteo daily request limit exhausted — it resets "
                        "at 00:00 UTC (05:30 IST). Cached results still work; "
                        "live fetches will not until then."
                    ) from exc
                if attempt < retries - 1:
                    time.sleep(5 * (2 ** attempt))   # 5s, 10s, 20s
                    continue
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise FetchError(f"could not fetch {url}: {last}") from last


# --------------------------------------------------------------------------
# Core multi-model point forecast
# --------------------------------------------------------------------------

@dataclass
class ModelSeries:
    """One model's hourly time series at one point."""
    model: str            # Open-Meteo id
    label: str            # human label, e.g. "ECMWF"
    times: list[str]
    data: dict[str, list[float | None]] = field(default_factory=dict)

    def get(self, field_name: str) -> list[float | None]:
        return self.data.get(field_name, [None] * len(self.times))

    def at(self, field_name: str, idx: int) -> float | None:
        series = self.data.get(field_name)
        if not series or idx >= len(series):
            return None
        return series[idx]


@dataclass
class PointForecast:
    """All models' hourly series for a single site."""
    site_key: str
    site_name: str
    lat: float
    lon: float
    times: list[str]
    models: dict[str, ModelSeries] = field(default_factory=dict)
    elevation_m: float | None = None

    def model_labels(self) -> list[str]:
        return [m.label for m in self.models.values()]


def _split_model_key(key: str, model_ids: Sequence[str]) -> tuple[str, str] | None:
    """
    Open-Meteo suffixes every field with the model id when several models are
    requested, e.g. 'precipitation_ecmwf_ifs025'. Split it back apart.
    Longest suffix first so 'gfs_seamless' does not shadow 'gfs_seamless_x'.
    """
    for mid in sorted(model_ids, key=len, reverse=True):
        suffix = "_" + mid
        if key.endswith(suffix):
            return key[: -len(suffix)], mid
    return None


def fetch_point(site, *, days: int = 7,
                models: Sequence[C.Model] | None = None,
                past_days: int = 0) -> PointForecast:
    """Fetch the full multi-model diagnostic stack for one site."""
    models = list(models or C.MODELS)
    model_ids = [m.key for m in models]
    labels = {m.key: m.label for m in models}

    params = {
        "latitude": site.lat,
        "longitude": site.lon,
        "hourly": ",".join(HOURLY_FIELDS),
        "models": ",".join(model_ids),
        "forecast_days": days,
        "timezone": C.TIMEZONE,
        "wind_speed_unit": "ms",
    }
    if past_days:
        params["past_days"] = past_days

    raw = _get_json(C.FORECAST_URL, params)
    if isinstance(raw, list):
        raw = raw[0]

    hourly = raw.get("hourly", {})
    times: list[str] = hourly.get("time", [])

    pf = PointForecast(
        site_key=site.key, site_name=site.name,
        lat=site.lat, lon=site.lon, times=times,
        elevation_m=raw.get("elevation"),
    )
    for mid in model_ids:
        pf.models[mid] = ModelSeries(model=mid, label=labels[mid], times=times)

    single = len(model_ids) == 1
    for key, series in hourly.items():
        if key == "time":
            continue
        if single:
            pf.models[model_ids[0]].data[key] = series
            continue
        split = _split_model_key(key, model_ids)
        if split is None:
            continue
        base, mid = split
        if mid in pf.models:
            pf.models[mid].data[base] = series

    # Drop models that returned nothing usable (e.g. a level a model lacks).
    pf.models = {
        mid: ms for mid, ms in pf.models.items()
        if any(v is not None for v in ms.get("precipitation"))
    }
    if not pf.models:
        raise FetchError(f"no model returned usable data for {site.name}")
    return pf


def fetch_sites(sites: Iterable, *, days: int = 7,
                models: Sequence[C.Model] | None = None) -> dict[str, PointForecast]:
    """Fetch several sites. Sequential and deliberately polite to the API."""
    out: dict[str, PointForecast] = {}
    for site in sites:
        try:
            out[site.key] = fetch_point(site, days=days, models=models)
        except FetchError as exc:
            print(f"  ! {site.name}: {exc}")
        time.sleep(0.4)
    return out


# --------------------------------------------------------------------------
# Ensemble - true probability of exceedance (Guide s13.2)
# --------------------------------------------------------------------------

@dataclass
class EnsembleForecast:
    times: list[str]
    members: list[list[float | None]]   # one list per member

    @property
    def n_members(self) -> int:
        return len(self.members)


def fetch_ensemble(site, *, days: int = 7) -> EnsembleForecast | None:
    """
    Fetch the GFS ensemble precipitation for one point.

    Guide s13.2: "If most ensemble members support rain, confidence in rain
    occurrence is higher. If only a few members show an extreme total, the
    extreme is possible but uncertain." This is strictly better than inferring
    uncertainty from three deterministic runs.
    """
    params = {
        "latitude": site.lat,
        "longitude": site.lon,
        "hourly": "precipitation",
        "models": C.ENSEMBLE_MODEL,
        "forecast_days": days,
        "timezone": C.TIMEZONE,
    }
    try:
        raw = _get_json(C.ENSEMBLE_URL, params)
    except FetchError as exc:
        print(f"  ! ensemble unavailable for {site.name}: {exc}")
        return None

    if isinstance(raw, list):
        raw = raw[0]
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])
    members = [v for k, v in hourly.items()
               if k.startswith("precipitation") and isinstance(v, list)]
    if not members:
        return None
    return EnsembleForecast(times=times, members=members)


# --------------------------------------------------------------------------
# Synoptic pressure field (monsoon trough / offshore trough)
# --------------------------------------------------------------------------

@dataclass
class PressureTransect:
    """MSL pressure sampled along a line of points, per forecast hour."""
    points: list[tuple[float, float]]
    times: list[str]
    pressures: list[list[float | None]]   # [point][hour]


def fetch_pressure_transect(points: Sequence[tuple[float, float]], *,
                            days: int = 7,
                            model: str = "ecmwf_ifs025") -> PressureTransect | None:
    """
    Sample MSL pressure along a transect.

    Used to locate the monsoon trough axis and the offshore trough - the two
    features Handbook Ch.13 and Guide s12 identify as setting the week's
    character. A single point cannot show either.
    """
    lats = ",".join(str(p[0]) for p in points)
    lons = ",".join(str(p[1]) for p in points)
    params = {
        "latitude": lats,
        "longitude": lons,
        "hourly": "pressure_msl",
        "models": model,
        "forecast_days": days,
        "timezone": C.TIMEZONE,
    }
    try:
        raw = _get_json(C.FORECAST_URL, params)
    except FetchError as exc:
        print(f"  ! pressure transect unavailable: {exc}")
        return None

    if not isinstance(raw, list):
        raw = [raw]
    times = raw[0].get("hourly", {}).get("time", [])
    pressures = [loc.get("hourly", {}).get("pressure_msl", []) for loc in raw]
    return PressureTransect(points=list(points), times=times, pressures=pressures)


# --------------------------------------------------------------------------
# Optional Windy Point Forecast API
# --------------------------------------------------------------------------

# Windy returns a `warning` field on responses served from its testing tier.
# That tier's data is, in Windy's own words, "randomly shuffled and slightly
# modified" - i.e. plausible-looking fiction. A forecast built on it would be
# confidently wrong with no outward sign, which is exactly the failure mode
# Handbook Ch.27 warns against. We refuse it rather than use it.
_WINDY_TEST_MARKERS = ("testing api", "randomly shuffled", "development purposes")


class WindyTestDataError(RuntimeError):
    """Windy served scrambled testing-tier data instead of a real forecast."""


def windy_data_is_real(payload: dict) -> tuple[bool, str]:
    """Inspect a Windy response for the testing-tier warning."""
    warning = str(payload.get("warning", "") or "")
    low = warning.lower()
    if any(marker in low for marker in _WINDY_TEST_MARKERS):
        return False, warning
    return True, warning


def fetch_windy_point(site, model: str = "gfs", *,
                      strict: bool = True) -> dict | None:
    """
    Supplementary read from Windy's own Point Forecast API.

    Returns None when no key is configured, when the call fails, or - by
    default - when Windy served testing-tier data.

    Windy's free tier exposes only GFS over India, so this can never replace
    the Ch.21 three-model comparison. Its value is that it reports the exact
    numbers windy.com itself would display for a point, which is useful when
    reconciling a bulletin against what you see on the map.

    Set strict=False only to inspect the testing payload deliberately; never
    for forecasting.
    """
    if not C.WINDY_API_KEY:
        return None
    payload = json.dumps({
        "lat": site.lat,
        "lon": site.lon,
        "model": model,
        "parameters": ["precip", "wind", "rh", "temp", "dewpoint", "cape",
                       "pressure"],
        "levels": ["surface", "925h", "850h", "700h"],
        "key": C.WINDY_API_KEY,
    }).encode("utf-8")
    req = urllib.request.Request(
        C.WINDY_POINT_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                      # noqa: BLE001 - supplementary
        print(f"  ! Windy API call failed ({exc}); continuing without it.")
        return None

    real, warning = windy_data_is_real(data)
    if not real:
        msg = (f"  ! Windy served TESTING-tier data and was discarded: "
               f"{warning.strip()}\n"
               f"    Fix: add a Project identification (and ideally a domain "
               f"restriction) to the key at https://api.windy.com/keys, then "
               f"re-run `python -m wxagent windy-check`.")
        if strict:
            print(msg)
            return None
        print(msg)
    return data


def windy_units_note() -> str:
    """Windy reports precipitation in metres; everything else is SI."""
    return ("Windy Point Forecast returns past-3h precipitation in metres "
            "(multiply by 1000 for mm), winds as u/v components in m/s, RH in "
            "%, CAPE in J/kg.")
