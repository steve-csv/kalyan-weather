"""
Large-scale climate drivers - currently the Indian Ocean Dipole.

The forecasters worth reading in this region cite the IOD as background
context alongside the offshore trough and the LPA position: a positive dipole
warms the western Indian Ocean relative to the east, shifting convection toward
the Indian side and tending to support above-normal rainfall over the west
coast. It is a seasonal signal, not a day-to-day one, and it belongs in a
weekly outlook rather than a daily bulletin.

HOW THIS IS COMPUTED, AND WHAT IT IS NOT
-----------------------------------------
The Dipole Mode Index is defined as the sea-surface-temperature anomaly
difference between two boxes:

    western pole (WTIO)   50E-70E, 10S-10N
    eastern pole (SETIO)  90E-110E, 10S-0N

    DMI = anomaly(WTIO) - anomaly(SETIO)

BoM and NOAA publish an official DMI against a long climatology. Those feeds
were not reachable, so this computes a PROXY from ERA5 near-surface temperature
over the two boxes, against a 10-year climatology for the same calendar window.

That means: the SIGN and the broad magnitude are meaningful; the exact value
will not match the official index, and it should never be quoted as if it did.
Every rendering below says so.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from . import config as C
from .sources import _get_json

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Box corners, sampled at 5 degrees.
WTIO_LATS = (-10.0, -5.0, 0.0, 5.0, 10.0)
WTIO_LONS = (50.0, 55.0, 60.0, 65.0, 70.0)
SETIO_LATS = (-10.0, -5.0, 0.0)
SETIO_LONS = (90.0, 95.0, 100.0, 105.0, 110.0)

CLIMATOLOGY_YEARS = 10
WINDOW_DAYS = 10          # averaging window around the target date

# Thresholds used by BoM for calling an event.
IOD_POSITIVE = 0.4
IOD_NEGATIVE = -0.4


def _box_mean(lats, lons, start: date, end: date) -> float | None:
    """Mean ERA5 near-surface temperature over a lat/lon box."""
    pairs = [(la, lo) for la in lats for lo in lons]
    params = {
        "latitude": ",".join(str(p[0]) for p in pairs),
        "longitude": ",".join(str(p[1]) for p in pairs),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_mean", "timezone": "UTC",
    }
    try:
        raw = _get_json(ARCHIVE_URL, params, timeout=90)
    except Exception:                             # noqa: BLE001
        return None
    if not isinstance(raw, list):
        raw = [raw]
    vals: list[float] = []
    for loc in raw:
        series = loc.get("daily", {}).get("temperature_2m_mean", []) or []
        vals.extend(v for v in series if v is not None)
    return statistics.mean(vals) if vals else None


@dataclass
class IODState:
    dmi: float | None
    west_anom: float | None
    east_anom: float | None
    phase: str
    interpretation: str
    valid_for: date
    proxy_note: str


# Bumped whenever the computation or the wording changes, so a stale cached
# result cannot outlive the text that explains it.
CACHE_VERSION = 2


def _cache_path(target: date):
    return C.CACHE_DIR / f"iod_v{CACHE_VERSION}_{target:%Y%m%d}.json"


def compute_iod(target: date | None = None, *,
                quiet: bool = True) -> IODState | None:
    """
    Proxy Dipole Mode Index for the window ending `target`.

    ERA5 lags a few days, so the window is stepped back accordingly. Cached per
    day - this is a slow-moving seasonal signal and each computation costs
    ~22 archive requests.
    """
    target = target or (date.today() - timedelta(days=6))
    cache = _cache_path(target)
    if cache.exists():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            return IODState(
                dmi=blob["dmi"], west_anom=blob["west"], east_anom=blob["east"],
                phase=blob["phase"], interpretation=blob["interp"],
                valid_for=date.fromisoformat(blob["valid"]),
                proxy_note=blob["proxy"],
            )
        except (json.JSONDecodeError, KeyError, OSError, ValueError):
            pass

    start = target - timedelta(days=WINDOW_DAYS)
    if not quiet:
        print(f"  computing IOD proxy for {start} to {target}...")

    west_now = _box_mean(WTIO_LATS, WTIO_LONS, start, target)
    east_now = _box_mean(SETIO_LATS, SETIO_LONS, start, target)
    if west_now is None or east_now is None:
        return None

    west_clim: list[float] = []
    east_clim: list[float] = []
    for back in range(1, CLIMATOLOGY_YEARS + 1):
        try:
            s = start.replace(year=start.year - back)
            e = target.replace(year=target.year - back)
        except ValueError:
            continue
        w = _box_mean(WTIO_LATS, WTIO_LONS, s, e)
        x = _box_mean(SETIO_LATS, SETIO_LONS, s, e)
        if w is not None:
            west_clim.append(w)
        if x is not None:
            east_clim.append(x)

    if not west_clim or not east_clim:
        return None

    west_anom = west_now - statistics.mean(west_clim)
    east_anom = east_now - statistics.mean(east_clim)
    dmi = west_anom - east_anom

    if dmi >= IOD_POSITIVE:
        phase = "positive"
        interp = (
            "A positive dipole warms the western Indian Ocean relative to the "
            "east, shifting convection toward the Indian side. This tends to "
            "support above-normal rainfall over the west coast and can help "
            "keep the monsoon current active — a background tailwind, not a "
            "day-to-day driver."
        )
    elif dmi <= IOD_NEGATIVE:
        phase = "negative"
        interp = (
            "A negative dipole warms the eastern Indian Ocean relative to the "
            "west, drawing convection away from the Indian side. This leans "
            "against monsoon rainfall over the west coast — a background "
            "headwind that makes break phases more likely to persist."
        )
    else:
        phase = "neutral"
        interp = (
            "The dipole is near neutral, so it is neither helping nor hindering "
            "the monsoon. Week-to-week rainfall will be set by the trough "
            "position, the offshore trough and any low pressure areas rather "
            "than by this background signal."
        )

    proxy = (
        f"Proxy DMI from ERA5 near-surface temperature over the standard IOD "
        f"boxes (WTIO 50–70°E/10°S–10°N minus SETIO 90–110°E/10°S–0°N), "
        f"against a {CLIMATOLOGY_YEARS}-year climatology for the same calendar "
        "window. This is **not** BoM's or NOAA's official DMI. Two differences "
        "matter: it uses near-surface air temperature rather than true SST, "
        f"and a {CLIMATOLOGY_YEARS}-year baseline rather than a multi-decadal "
        "one. Because recent years are warm, a short baseline **damps the "
        "anomalies on both poles** and biases this proxy toward neutral — so "
        "where a published index says weakly positive, expect this to read "
        "closer to zero. Trust the sign and the direction of travel; take the "
        "official index over this one whenever you have it."
    )

    state = IODState(dmi=dmi, west_anom=west_anom, east_anom=east_anom,
                     phase=phase, interpretation=interp, valid_for=target,
                     proxy_note=proxy)
    try:
        cache.write_text(json.dumps({
            "dmi": dmi, "west": west_anom, "east": east_anom, "phase": phase,
            "interp": interp, "valid": target.isoformat(), "proxy": proxy,
        }), encoding="utf-8")
    except OSError:
        pass
    return state


# --------------------------------------------------------------------------
# ENSO - Nino 3.4
# --------------------------------------------------------------------------
# The interannual driver. El Nino warms the central-eastern Pacific, shifts the
# Walker circulation east, and is associated with a weaker Indian monsoon;
# La Nina does the reverse. It sets the season's background odds - it does not
# forecast any individual day, and saying otherwise would be astrology.
#
# Taken from NOAA CPC's published monthly Nino 3.4 series rather than computed,
# because CPC's is the number everyone else quotes and a home-rolled version
# would disagree with every bulletin Steven reads.

NINO34_URL = ("https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/"
              "ensostuff/detrend.nino34.ascii.txt")

EL_NINO_WEAK = 0.5
EL_NINO_MODERATE = 1.0
EL_NINO_STRONG = 1.5


@dataclass
class ENSOState:
    year: int
    month: int
    sst: float
    climatology: float
    anomaly: float
    phase: str
    strength: str
    interpretation: str
    recent: list[tuple[int, int, float]]
    months_lag: int


def fetch_enso(*, today: date | None = None,
               quiet: bool = True) -> ENSOState | None:
    """Latest monthly Nino 3.4 anomaly from NOAA CPC."""
    today = today or date.today()
    cache = C.CACHE_DIR / f"enso_{today:%Y%m}.json"
    if cache.exists():
        try:
            b = json.loads(cache.read_text(encoding="utf-8"))
            return ENSOState(**{**b, "recent": [tuple(x) for x in b["recent"]]})
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            pass

    try:
        import urllib.request
        req = urllib.request.Request(
            NINO34_URL, headers={"User-Agent": "kalyan-wx-agent/1.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:                      # noqa: BLE001
        if not quiet:
            print(f"  ! ENSO unavailable: {exc}")
        return None

    rows: list[tuple[int, int, float, float, float]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].isdigit():
            try:
                rows.append((int(parts[0]), int(parts[1]), float(parts[2]),
                             float(parts[3]), float(parts[4])))
            except ValueError:
                continue
    if not rows:
        return None

    y, m, sst, clim, anom = rows[-1]
    lag = (today.year - y) * 12 + (today.month - m)

    if anom >= EL_NINO_WEAK:
        phase = "El Nino"
        strength = ("strong" if anom >= EL_NINO_STRONG else
                    "moderate" if anom >= EL_NINO_MODERATE else "weak")
        interp = (
            f"A {strength} El Nino is in place. Historically this tilts the "
            "Indian monsoon toward below-normal seasonal rainfall — the "
            "Walker circulation shifts east, subsidence increases over the "
            "Indian Ocean sector, and break spells tend to run longer. It is a "
            "seasonal tilt in the odds, not a forecast for any given day, and "
            "plenty of El Nino years have still delivered heavy individual "
            "spells on this coast."
        )
    elif anom <= -EL_NINO_WEAK:
        phase = "La Nina"
        strength = ("strong" if anom <= -EL_NINO_STRONG else
                    "moderate" if anom <= -EL_NINO_MODERATE else "weak")
        interp = (
            f"A {strength} La Nina is in place, which historically favours a "
            "stronger Indian monsoon — more active spells, shorter breaks. "
            "Again a seasonal tilt, not a daily signal."
        )
    else:
        phase = "Neutral"
        strength = "neutral"
        interp = (
            "ENSO is neutral, so the Pacific is neither helping nor hindering "
            "this season. Rainfall will be governed by what happens week to "
            "week — the trough position, the offshore trough and any low "
            "pressure areas — rather than by a background tilt."
        )

    state = ENSOState(
        year=y, month=m, sst=sst, climatology=clim, anomaly=anom,
        phase=phase, strength=strength, interpretation=interp,
        recent=[(r[0], r[1], r[4]) for r in rows[-6:]],
        months_lag=lag,
    )
    try:
        cache.write_text(json.dumps(state.__dict__), encoding="utf-8")
    except (OSError, TypeError):
        pass
    return state


def render_enso(s: ENSOState | None) -> str:
    if s is None:
        return ""
    trend = " → ".join(f"{a:+.2f}" for _y, _m, a in s.recent)
    out = (f"**ENSO:** **{s.phase}**"
           + (f" ({s.strength})" if s.phase != "Neutral" else "")
           + f" — Niño 3.4 anomaly **{s.anomaly:+.2f} °C** "
             f"({s.year}-{s.month:02d}).  \n"
           f"Last six months: {trend}\n\n{s.interpretation}\n\n")
    if s.months_lag >= 2:
        out += (f"> Note the lag: this is the {s.year}-{s.month:02d} monthly "
                f"value, about {s.months_lag} months behind today. ENSO is an "
                "interannual signal that moves slowly, so a two-month-old "
                "value is still meaningful — but it is not a current "
                "measurement, and a fast-developing event will show up here "
                "late.\n")
    return out


def render(state: IODState | None) -> str:
    if state is None:
        return ""
    sign = "+" if (state.dmi or 0) >= 0 else ""
    return (
        f"**Indian Ocean Dipole:** **{state.phase}** "
        f"(proxy DMI {sign}{state.dmi:.2f} °C — western pole "
        f"{state.west_anom:+.2f}, eastern pole {state.east_anom:+.2f}, "
        f"window ending {state.valid_for:%d %b}).  \n"
        f"{state.interpretation}\n\n"
        f"> {state.proxy_note}\n"
    )
