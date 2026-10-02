"""
Morning fog, mist and haze for the Konkan lowlands.

WHY THIS EXISTS
---------------
From November to February the thing that actually disrupts a Kalyan morning is
not rain. It is a white-out on the Kalyan-Shil road at 06:30, a 40-minute hole
in the Central line timetable, and a drive to Pune where the Khopoli-Khandala
stretch is invisible. The agent forecast rain in detail and said nothing about
any of it: the only mentions of fog in this repository were two lines of prose.

THE MECHANISM (Handbook Ch.2, radiation fog)
--------------------------------------------
Clear sky lets the ground radiate heat away after sunset. The surface cools,
cools the air touching it, and when that air reaches its dew point the moisture
condenses in place. So it needs three things at once, and the absence of any
one of them is why most cold nights produce nothing:

  1. A SMALL DEW-POINT DEPRESSION. T - Td is the whole forecast. Two degrees
     is a night that might just manage mist; under one degree the air is
     already nearly saturated and only has to cool a fraction further.
  2. LIGHT WIND. Dead calm gives dew on the grass and at most a shallow ground
     layer, because nothing stirs the moisture up to eye level. Above about
     3 m/s the mixing is too vigorous and you get low stratus instead. The
     sweet spot is a drifting 0.5-2 m/s, which is why fog mornings feel still
     rather than frozen.
  3. CLEAR SKIES. Cloud radiates back down and the surface never cools enough.
     An overcast winter night is a fog-free winter night.

Kalyan fogs more readily than Colaba or Santacruz: it sits inland on the Ulhas,
where the sea's thermal inertia no longer holds the night temperature up and
the river keeps the lowest layer damp. A coastal station reporting clear can be
an hour's drive from a valley under a metre of it.

FOG IS NOT HAZE, AND THE DIFFERENCE IS THE HUMIDITY
---------------------------------------------------
Both wreck visibility and the city calls both "fog". Fog and mist are water -
high humidity, small depression, and they burn off within an hour or two of
sunrise. Winter haze is dry: particulates in a shallow inversion with the
humidity nowhere near saturation, and it does not burn off, it disperses when
the wind picks up. Telling a reader "haze, not fog" tells them it will still be
there at eleven.

WHAT THIS CAN AND CANNOT SEE
----------------------------
Visibility itself is only published by GFS here - ECMWF and ICON return nulls
for the field - so it is carried as one model's corroboration and never as the
basis for the call. The call is made from the ingredients, which all three
models provide, and it is made SEPARATELY FOR EACH MODEL because the models
disagree about dew point by up to four degrees, and the dew-point depression is
the entire forecast. Averaging that spread would manufacture a confidence none
of them has.

No calendar gate. Withdrawal taught this repository that a date is not a
diagnosis; if the ingredients line up in October the page says so, and if a
December night is windy it stays quiet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Sequence

from . import config as C

# The hours radiation fog forms and sits in. It deepens through the second half
# of the night and is thickest around and just after sunrise, which in a Mumbai
# winter is about 06:50.
DAWN_START, DAWN_END = 0, 10

# Dew-point depression, degrees. The single most important number here.
DEP_FOG = 1.0
DEP_MIST = 2.2

# 10 m wind, m/s. Above STIR the layer mixes into stratus instead of fogging.
WIND_FOG = 2.0
WIND_MIST = 3.2

# Low cloud, percent. Cloud overhead radiates back down and the ground never
# cools to its dew point.
CLOUD_FOG = 40.0
CLOUD_MIST = 55.0

# Overnight cooling: yesterday afternoon's maximum minus this dawn's minimum.
#
# THIS IS THE TEST THAT MAKES THE REST WORK. Without it, every monsoon morning
# qualifies - checked against mid-September 2026, where all ten mornings came
# back "mist likely" because saturated air at first light is simply what the
# monsoon is. Nothing was condensing out of it; the air was damp, the wind was
# slack, and the visibility was fine.
#
# Radiation fog is not made by damp air, it is made by a surface that has
# radiated its heat away, and the fingerprint of that is a big drop from the
# afternoon. A Kalyan January night falls 14-16C from the afternoon peak; a
# September night under monsoon cloud falls four or five. The threshold sits
# in the gap, and it is the difference between a forecast and a hygrometer.
COOL_FOG = 7.0
COOL_MIST = 6.0

# Fog is condensation out of still air. If it is raining, poor visibility is
# the rain, and calling it fog would promise the wrong thing - rain does not
# burn off at nine.
WET_WINDOW_MM = 0.3

# Haze: visibility is poor but the air is nowhere near saturated, so whatever
# is in the way is not water.
HAZE_VIS_M = 5000.0
HAZE_MAX_RH = 75.0

# GFS visibility bands, metres. IMD's own fog categories, Handbook Ch.2.
IMD_FOG_BANDS = (
    (50.0, "very dense", "near zero — do not drive in it"),
    (200.0, "dense", "under 200 m — trains and flights are affected"),
    (500.0, "moderate", "200-500 m — slow, and keep lights on"),
    (1000.0, "shallow", "500 m-1 km — the usual Kalyan winter morning"),
)

LEVELS = ("none", "haze", "mist", "fog", "dense")
LEVEL_RANK = {k: i for i, k in enumerate(LEVELS)}


@dataclass
class ModelFog:
    """One model's read on one morning."""
    model: str
    depression: float | None       # minimum T - Td through the window
    wind: float | None             # mean 10 m wind
    low_cloud: float | None        # mean low cloud cover
    rh_max: float | None
    visibility_m: float | None     # GFS only; None elsewhere
    cooling: float | None = None   # yesterday's afternoon max - dawn min
    wet_mm: float = 0.0            # rain in the window
    level: str = "none"


@dataclass
class FogMorning:
    day: date
    models: list[ModelFog] = field(default_factory=list)
    level: str = "none"            # the agreed call
    agree: int = 0                 # how many models reached that level
    total: int = 0
    sentence: str = ""

    @property
    def is_fog(self) -> bool:
        return LEVEL_RANK[self.level] >= LEVEL_RANK["mist"]


def _band(vis_m: float | None) -> tuple[str, str] | None:
    if vis_m is None:
        return None
    for limit, name, meaning in IMD_FOG_BANDS:
        if vis_m < limit:
            return name, meaning
    return None


def _mean(xs: Sequence[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _window(times: Sequence[str], day: date) -> list[int]:
    out = []
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if dt.date() == day and DAWN_START <= dt.hour < DAWN_END:
            out.append(i)
    return out


def _afternoon(times: Sequence[str], day: date) -> list[int]:
    """Indices of the PREVIOUS afternoon — the heat that has to be lost."""
    before = day - timedelta(days=1)
    out = []
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if dt.date() == before and 12 <= dt.hour < 17:
            out.append(i)
    return out


def _classify(m: ModelFog) -> str:
    """One model's ingredients to one word."""
    dep, wind, cloud = m.depression, m.wind, m.low_cloud
    if dep is None:
        return "none"
    wind = 99.0 if wind is None else wind
    cloud = 100.0 if cloud is None else cloud
    cool = -99.0 if m.cooling is None else m.cooling

    # Rain in the window is rain, not fog.
    if m.wet_mm >= WET_WINDOW_MM:
        return "none"

    # Haze first: poor visibility without the moisture to explain it is not
    # fog, and saying so tells the reader it will not burn off at nine.
    if (m.visibility_m is not None and m.visibility_m < HAZE_VIS_M
            and (m.rh_max is None or m.rh_max < HAZE_MAX_RH)):
        return "haze"

    if (dep <= DEP_FOG and wind <= WIND_FOG and cloud <= CLOUD_FOG
            and cool >= COOL_FOG):
        # Only GFS can distinguish dense, and only where it published a value.
        band = _band(m.visibility_m)
        if band and band[0] in ("dense", "very dense"):
            return "dense"
        return "fog"
    if (dep <= DEP_MIST and wind <= WIND_MIST and cloud <= CLOUD_MIST
            and cool >= COOL_MIST):
        return "mist"
    return "none"


def assess_morning(pf, day: date) -> FogMorning:
    """Read every model's dawn window for one day."""
    out = FogMorning(day=day)
    for key, ms in pf.models.items():
        idx = _window(pf.times, day)
        if not idx:
            continue
        temp = ms.get("temperature_2m")
        dew = ms.get("dew_point_2m")
        deps = [temp[i] - dew[i] for i in idx
                if i < len(temp) and i < len(dew)
                and temp[i] is not None and dew[i] is not None]
        vis = [ms.get("visibility")[i] for i in idx
               if i < len(ms.get("visibility"))
               and ms.get("visibility")[i] is not None]
        rh = [ms.get("relative_humidity_2m")[i] for i in idx
              if i < len(ms.get("relative_humidity_2m"))
              and ms.get("relative_humidity_2m")[i] is not None]
        # Yesterday afternoon's maximum, against this dawn's minimum: the
        # amount of heat the surface actually radiated away overnight.
        prev = _afternoon(pf.times, day)
        hot = [temp[i] for i in prev
               if i < len(temp) and temp[i] is not None]
        cold = [temp[i] for i in idx
                if i < len(temp) and temp[i] is not None]
        cooling = (max(hot) - min(cold)) if hot and cold else None

        rain = ms.get("precipitation")
        wet = sum(rain[i] for i in idx
                  if i < len(rain) and rain[i] is not None)

        m = ModelFog(
            model=ms.label,
            cooling=cooling,
            wet_mm=wet,
            depression=min(deps) if deps else None,
            wind=_mean([ms.get("wind_speed_10m")[i] for i in idx
                        if i < len(ms.get("wind_speed_10m"))]),
            low_cloud=_mean([ms.get("cloud_cover_low")[i] for i in idx
                             if i < len(ms.get("cloud_cover_low"))]),
            rh_max=max(rh) if rh else None,
            visibility_m=min(vis) if vis else None,
        )
        m.level = _classify(m)
        out.models.append(m)

    if not out.models:
        return out

    out.total = len(out.models)
    # The agreed call is the one at least half the models reach - not the
    # worst case, which would cry fog every time one model ran damp, and not
    # an average of the ingredients, which is a forecast no model made.
    for lv in reversed(LEVELS):
        n = sum(1 for m in out.models if LEVEL_RANK[m.level] >= LEVEL_RANK[lv])
        if lv != "none" and n * 2 >= out.total:
            out.level, out.agree = lv, n
            break
    out.sentence = _sentence(out)
    return out


def _sentence(f: FogMorning) -> str:
    if f.level == "none":
        return ""
    worst = min((m for m in f.models if m.depression is not None),
                key=lambda m: m.depression, default=None)
    if worst is None:
        return ""

    spread = [m.depression for m in f.models if m.depression is not None]
    disagree = (max(spread) - min(spread)) >= 2.0 if len(spread) > 1 else False

    if f.level == "haze":
        return ("Poor morning visibility, but the air is too dry for fog — "
                "this is haze held under a shallow inversion. It will not "
                "burn off the way fog does; it clears when the wind picks up.")

    what = {"mist": "Mist or shallow ground fog",
            "fog": "Fog", "dense": "Dense fog"}[f.level]
    body = (f"{what} likely around dawn. The air cools to within "
            f"{worst.depression:.1f}°C of its dew point with the wind at "
            f"{worst.wind:.1f} m/s"
            + (f" under {worst.low_cloud:.0f}% low cloud"
               if worst.low_cloud is not None else "")
            + f" — {f.agree} of {f.total} models agree.")

    band = next((_band(m.visibility_m) for m in f.models
                 if m.visibility_m is not None and _band(m.visibility_m)), None)
    if band:
        body += (f" GFS, the only model here that publishes visibility, puts "
                 f"it in IMD's **{band[0]}** band: {band[1]}.")
    else:
        # The awkward case, and the usual one. Checked over January 2026:
        # every single flagged morning was ECMWF and ICON agreeing while GFS
        # ran 4-10C drier at dawn and reported 24 km. One model contradicting
        # the other two outright is not a rounding difference, and the only
        # model that publishes visibility being the dissenter is exactly the
        # thing a reader deserves to know before trusting the call.
        clear = [m for m in f.models
                 if m.visibility_m is not None and m.visibility_m >= 10000]
        if clear:
            c = clear[0]
            body += (f" Against this, {c.model} — the only model here that "
                     f"publishes visibility — shows it clear, and runs "
                     f"{c.depression:.1f}°C from its dew point at dawn where "
                     "the others are near saturation. One model saying nothing "
                     "while two say fog is a genuine split, not a detail.")

    if disagree:
        body += (" On a fog night a dew-point spread this wide is the "
                 "difference between a white-out and an ordinary morning, so "
                 "treat this as a possibility to check at the window rather "
                 "than a forecast to plan around.")

    body += (" Thickest between 05:00 and 08:00, clearing within an hour or "
             "two of sunrise.")
    return body


def outlook(pf, days: Sequence[date]) -> list[FogMorning]:
    out = [assess_morning(pf, d) for d in days]
    return [f for f in out if f.level != "none"]


def render(mornings: Sequence[FogMorning]) -> str:
    """Markdown block for the bulletin."""
    hits = [f for f in mornings if f.level != "none"]
    if not hits:
        return ""
    out = "## Morning visibility\n"
    for f in hits:
        out += f"**{f.day:%A %d %b}** — {f.sentence}\n\n"
    out += ("> Fog is diagnosed from the ingredients, not read off a "
            "visibility field: only GFS publishes one for this point. IMD's "
            "fog warnings are the official ones.\n\n")
    return out
