"""
WHAT KIND of rain is coming, not just how much.

WHY THIS EXISTS
---------------
Everything else here answers "how many millimetres". That is the wrong first
question. Twelve millimetres from the southwest monsoon and twelve from an
easterly thunderstorm are not the same weather and do not call for the same
decision:

  * MONSOON rain arrives on a westerly, falls for hours, and lands hardest on
    the WINDWARD side of the Ghats. Kalyan gets soaked, Pune stays dry.

  * EASTERLY rain arrives on the opposite wind. That reverses the terrain
    sign - the Ghats' east face becomes the windward one and the Konkan sits
    in the lee - so the rain-shadow logic that governs the whole monsoon runs
    BACKWARDS. It also tends to come as afternoon and evening thunderstorms
    rather than as a steady fall: short, violent, very local, with lightning.

  * SYSTEM rain - a low pressure area or depression - is widespread and long,
    and it weakens the rain shadow entirely, so Pune and Nashik get caught up
    in it too.

  * A WESTERN DISTURBANCE is a winter visitor from the northwest and almost
    never reaches this coast; naming it matters mainly so the page can say
    when something is NOT one.

So a reader who only sees "12 mm" cannot tell whether to expect a grey
drizzly day or one sharp thundery hour at five o'clock. This module names the
driver and says what that driver feels like.

HOW IT DECIDES
--------------
From fields the agent already fetches, so this costs no extra requests:

  * 850 hPa WIND DIRECTION - the layer that carries the moisture. Westerly
    quadrant is monsoon, easterly quadrant is not, and the boundary between
    them is the single most informative number here.
  * THE SHAPE OF THE RAINFALL - how much falls in the busiest hour against
    how long it rains at all. Showers dump and stop; layer cloud drizzles for
    hours. This is derived from the hourly series rather than from the
    models' own "showers" field, which is unusable here: ECMWF reports zero
    showers always, while GFS and ICON put ESSENTIALLY EVERYTHING into it.
    A convective fraction built on that would read 0% or 100% depending only
    on which model was asked, which is worse than no signal at all.
  * CAPE, CIN and SHEAR - whether a thunderstorm has fuel, a lid, and the
    organisation to last.
  * THE TRACKED SYSTEMS - if a low is close enough AND its circulation is
    felt here as real wind, it owns the day.
  * 500 hPa WIND in the cold half of the year, for the western-disturbance
    case.

WHAT IT CANNOT DO
-----------------
It cannot see a western disturbance directly. The synoptic grid stops at 28N
and a WD trough usually sits north of that, so what this detects is the
RESPONSE over us, not the disturbance itself - and it says so rather than
claiming the diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .diagnostics import compass, lift_profile, moisture_profile, stability_profile

# Wind-direction quadrants at 850 hPa, in degrees the wind blows FROM.
# The monsoon window is deliberately wide - anything with a westerly
# component carries sea air onto this coast - and the easterly window is the
# mirror of it. The gaps between them are the transition cases, where the
# driver genuinely is unclear and the page should say so.
SW_MONSOON_FROM = (200.0, 300.0)      # SSW through WNW
EASTERLY_FROM = (45.0, 145.0)         # NE through SSE

# Northerly / north-westerly: dry continental air off the land. In September
# and October this is the signature of the monsoon WITHDRAWING - the flow
# stops coming off the sea and starts coming down from the interior, which is
# why the air turns muggy but the rain stops. Wraps through 360, so it is
# tested as two ranges rather than one.
NORTHERLY_FROM = ((315.0, 360.0), (0.0, 45.0))

# Below this the 850 hPa flow is too weak to be called a driver at all.
MIN_DRIVER_MS = 3.0

# A system this close CAN own the day's rain - but only if its circulation is
# actually felt here, i.e. the 850 hPa wind clears MIN_DRIVER_MS. Distance
# alone let a 1011 hPa low 546 km away claim a day with 2.8 m/s of wind
# (21 Sep 2026) and describe it as widespread, long-lasting system rain.
SYSTEM_OWNS_KM = 700.0

# ...except when the centre is this close. Within about one step of the 2
# degree pressure grid the low is effectively overhead at the resolution we
# have, and light wind there is the calm near its centre, not its absence.
SYSTEM_CORE_KM = 250.0

# BURSTINESS: the busiest hour's share of the day's total. A day that drops
# half its rain in one hour is showery; one that spreads it over twelve is
# layer cloud. This replaces the models' own showers/rain split, which cannot
# be used - see the module docstring.
BURST_HIGH = 0.45
BURST_SOME = 0.28

# Thunderstorm fuel. CAPE is potential, never certainty - Handbook Ch.24 - so
# these gate a RISK word, never a forecast of storms.
CAPE_LIKELY = 1000.0
CAPE_POSSIBLE = 600.0

LABELS = {
    "system": "Low pressure system",
    "sw_monsoon": "Southwest monsoon flow",
    "easterly": "Easterly winds",
    "thunderstorm": "Local thunderstorms",
    "western_disturbance": "Western disturbance",
    "northerly": "Dry northerly flow",
    "weak": "No single driver",
}


@dataclass
class RainSource:
    key: str
    label: str
    thunder_risk: str = "none"          # none | possible | likely
    burst_share: float | None = None   # busiest hour's share of the day
    wind_from: float | None = None
    wind_ms: float | None = None
    system_km: float | None = None
    system_name: str = ""
    headline: str = ""
    detail: str = ""
    terrain_note: str = ""
    contributors: list[str] = field(default_factory=list)

    @property
    def is_thundery(self) -> bool:
        return self.thunder_risk in ("possible", "likely")


def _in(deg: float | None, window: tuple[float, float]) -> bool:
    if deg is None:
        return False
    lo, hi = window
    return lo <= (deg % 360.0) <= hi


def _burstiness(ms, idx: Sequence[int]) -> tuple[float, float | None]:
    """Day total, and the share of it that falls in the single busiest hour.

    The share is the convective signal: near 1.0 means everything arrived at
    once, which is a shower; near 1/24 means it drizzled all day, which is
    layer cloud. None when there is too little rain for the ratio to mean
    anything - dividing a trace by a trace produces confident nonsense.
    """
    hourly = [ms.at("precipitation", i) or 0.0 for i in idx]
    total = sum(hourly)
    if total < 1.0 or not hourly:
        return total, None
    return total, max(hourly) / total


def classify(ms, idx: Sequence[int], *, season: str = "monsoon",
             zone: str = "transition", nearest_system=None,
             day=None) -> RainSource:
    """Name the driver behind a day's rain.

    `nearest_system` is a systems.SystemEvent or None. It is checked FIRST:
    when a low is close enough, the wind at 850 hPa is a symptom of that low
    rather than an independent driver, and reporting the two separately would
    double-count one cause.
    """
    lift = lift_profile(ms, idx)
    stab = stability_profile(ms, idx)
    moist = moisture_profile(ms, idx)
    total, burst = _burstiness(ms, idx)

    # Thunder needs BOTH fuel and a showery shape. CAPE alone is potential,
    # never certainty (Handbook Ch.24) - a big number with the rain spread
    # evenly over twelve hours is a wet day, not a stormy one.
    cape = stab.cape_peak or 0.0
    if cape >= CAPE_LIKELY and (burst or 0) >= BURST_HIGH:
        thunder = "likely"
    elif cape >= CAPE_POSSIBLE and (burst or 0) >= BURST_SOME:
        thunder = "possible"
    else:
        thunder = "none"

    src = RainSource(
        key="weak", label=LABELS["weak"], thunder_risk=thunder,
        burst_share=burst, wind_from=lift.wind_850_dir,
        wind_ms=lift.wind_850_speed,
    )

    wind_ms = lift.wind_850_speed or 0.0
    from_deg = lift.wind_850_dir

    # ---- 1. A close system owns the day --------------------------------
    if nearest_system is not None:
        # The distance ON THIS DAY, not the track's overall closest approach.
        # The latter is the right number for an alert and the wrong one here:
        # using it made every day of the week read as system-driven because
        # the low passed close on exactly one of them.
        if day is not None and hasattr(nearest_system, "distance_on"):
            # Strict. A system not resolved on this day is not driving this
            # day, and falling back to its overall closest approach would put
            # it back in charge of the whole week - the bug this replaced.
            km = nearest_system.distance_on(day)
        else:
            km = getattr(getattr(nearest_system, "closest", None),
                         "distance_km", None)
        felt = wind_ms >= MIN_DRIVER_MS or (km is not None
                                              and km <= SYSTEM_CORE_KM)
        if km is not None and km <= SYSTEM_OWNS_KM and not felt:
            # Near enough to set the direction of a light wind, not to drive
            # rain. Named, so the reader is not left wondering why the alert
            # above mentions a low that this section then ignores.
            src.contributors.append(
                f"a low pressure area about {km:,.0f} km away — close enough to set "
                "the direction of the light wind here, too far to drive rain "
                "on its own")
        if km is not None and km <= SYSTEM_OWNS_KM and felt:
            src.key, src.label = "system", LABELS["system"]
            src.system_km, src.system_name = km, nearest_system.headline
            src.headline = "Rain from a low pressure system"
            src.detail = (
                f"The rain here is being driven by a low pressure area about "
                f"**{km:,.0f} km** away, not by the ordinary onshore wind. "
                "System rain behaves differently from monsoon rain in two ways "
                "worth planning around. It is **widespread and long-lasting** "
                "rather than a few hours of showers, because the whole column "
                "is being lifted over a large area instead of only where the "
                "hills force it up. And it **weakens the rain shadow** — the "
                "dry side behind the Ghats, which stays dry through most of "
                "the monsoon, gets caught up in this kind of rain too, so "
                "Pune and Nashik can be as wet as the coast.")
            src.terrain_note = (
                "Because the lifting comes from the system rather than the "
                "hills, the usual crest-versus-lee gap narrows.")
            if src.is_thundery:
                src.contributors.append(
                    "thunderstorms embedded in the system's rainbands")
            return src

    # ---- 2. Southwest monsoon flow --------------------------------------
    if _in(from_deg, SW_MONSOON_FROM) and wind_ms >= MIN_DRIVER_MS:
        deep = moist.depth_class == "deep"
        src.key, src.label = "sw_monsoon", LABELS["sw_monsoon"]
        src.headline = "Rain from the southwest monsoon wind"
        src.detail = (
            f"The wind about a kilometre and a half up is from the "
            f"**{compass(from_deg)}** at {wind_ms:.0f} m/s, which is the "
            "monsoon's own wind carrying sea air onto this coast. That gives "
            "the familiar kind of monsoon rain: it arrives **steadily and "
            "lasts for hours** rather than coming in one burst, and it falls "
            "hardest where the land forces the air upward — the windward face "
            "of the Ghats above Kalyan, Igatpuri and Matheran."
            + (" The air is humid all the way up through the cloud layer, so "
               "nothing dry aloft is left to choke the clouds — that is the "
               "setup for a properly wet day."
               if deep else
               " The air is humid near the ground but drier higher up, so "
               "expect the clouds to stay shallow and the rain to come in at "
               "the lower end of what the models say."))
        src.terrain_note = (
            "Westerly rain means the **rain shadow is working normally**: the "
            "crest is wet and Pune, behind it, stays comparatively dry.")
        if src.is_thundery:
            src.contributors.append(
                "embedded thunderstorms where the air is most unstable")
        return src

    # ---- 3. Easterly flow ------------------------------------------------
    if _in(from_deg, EASTERLY_FROM) and wind_ms >= MIN_DRIVER_MS:
        src.key, src.label = "easterly", LABELS["easterly"]
        src.headline = ("Thundery rain on an easterly wind"
                        if src.is_thundery else "Rain on an easterly wind")
        src.detail = (
            f"The wind a kilometre and a half up is from the "
            f"**{compass(from_deg)}** at {wind_ms:.0f} m/s — **the opposite "
            "direction to the monsoon**. This is not monsoon rain, and it does "
            "not behave like it.\n\n"
            "Easterly rain usually builds through the **afternoon and "
            "evening** rather than falling all day, and it comes as "
            "thunderstorms: short, heavy, very local, with lightning. One "
            "suburb can get twenty minutes of downpour while the next stays "
            "completely dry, so a small daily total can still mean a soaking "
            "where it lands.")
        src.terrain_note = (
            "**The terrain works backwards on an easterly.** The east face of "
            "the Ghats becomes the windward side, and the Konkan — Mumbai, "
            "Thane, Kalyan — sits in the lee. So the inland belts around "
            "Karjat, Badlapur and the plateau often see these storms first "
            "and worst, and the coast can stay dry while the hills light up. "
            "That is the reverse of the monsoon pattern this page describes "
            "the rest of the season.")
        if src.is_thundery:
            src.contributors.append("lightning risk with these storms")
        return src

    # ---- 3b. Dry continental northerly ----------------------------------
    if wind_ms >= MIN_DRIVER_MS and any(_in(from_deg, w) for w in NORTHERLY_FROM):
        src.key, src.label = "northerly", LABELS["northerly"]
        withdrawing = season in ("monsoon", "post_monsoon")
        src.headline = ("Dry northerly air — the monsoon pulling back"
                        if withdrawing else "Dry northerly air")
        src.detail = (
            f"The wind a kilometre and a half up is from the "
            f"**{compass(from_deg)}** at {wind_ms:.0f} m/s. That is air coming "
            "**down off the land**, not in off the sea"
            + (", which is what the monsoon withdrawing looks like from here. "
               "The moisture supply is cut off at source: the sky can still "
               "look heavy and the air can feel muggy near the ground, but "
               "there is nothing feeding the clouds from above."
               if withdrawing else
               ". Dry continental air like this suppresses rain.")
            + " Expect **little or no rain**, warmer afternoons, and any "
              "shower that does appear to be built locally by the heat rather "
              "than blown in.")
        src.terrain_note = (
            "With no onshore wind there is nothing for the Ghats to lift, so "
            "the usual crest-versus-coast difference largely disappears — "
            "everywhere is about equally dry.")
        if src.is_thundery:
            src.contributors.append(
                "isolated heat-driven storms despite the dry flow")
        return src

    # ---- 4. Western disturbance -----------------------------------------
    # Only in the cold half of the year, and only ever as an inference. The
    # synoptic grid stops at 28N; a WD trough normally sits north of that, so
    # what is visible here is the response over us, never the system itself.
    if season in ("winter", "pre_monsoon"):
        w500 = ms.at("wind_direction_500hPa", idx[len(idx) // 2]) if idx else None
        s500 = ms.at("wind_speed_500hPa", idx[len(idx) // 2]) if idx else None
        if (w500 is not None and s500 is not None and s500 >= 15.0
                and 240.0 <= (w500 % 360.0) <= 330.0):
            src.key, src.label = "western_disturbance", LABELS["western_disturbance"]
            src.headline = "Rain from a western disturbance"
            src.detail = (
                "The winds high up are strong and from the west-northwest, "
                "which is the signature of a **western disturbance** — a "
                "mid-latitude storm tracking east across north India in "
                "winter. These bring cloud, light rain and a sharp drop in "
                "temperature, and their main effect is far to our north. "
                "**This coast usually gets very little from one**, so treat "
                "this as an explanation for grey, cool, drizzly weather "
                "rather than as a rain warning.\n\n"
                "Read this as an inference, not a diagnosis: the pressure "
                "grid this agent samples stops at 28°N, and the disturbance "
                "itself sits north of that. What is being detected is our "
                "response to it.")
            return src

    # ---- 5. Thunderstorms with no larger driver --------------------------
    if src.is_thundery:
        src.key, src.label = "thunderstorm", LABELS["thunderstorm"]
        src.headline = "Local thunderstorms, no larger system behind them"
        src.detail = (
            "There is no strong wind or system driving rain onto this coast, "
            f"but the air holds real energy ({cape:.0f} J/kg) and the models "
            "drop much of the day's rain in a single burst rather than "
            "spreading it out. That means rain built **locally by daytime heating** rather than "
            "blown in: it fires in the afternoon, it is extremely patchy, and "
            "where it does land it can be brief and violent. Lightning is the "
            "real hazard with this kind of day, more than the rainfall.")
        src.terrain_note = (
            "Storms like these often anchor on the hills, so the Ghat belts "
            "fire first while the coast waits.")
        return src

    # ---- 6. Nothing in charge -------------------------------------------
    src.headline = "No clear driver"
    wind = (f"The wind about a kilometre and a half up is light "
            f"({wind_ms:.1f} m/s"
            + (f" from the {compass(from_deg)}" if from_deg is not None else "")
            + "), too weak to carry rain in from anywhere")
    if cape >= CAPE_POSSIBLE:
        # Thunder is "none" here only because the rain is modelled as spread
        # out. The fuel is real, and saying the air is stable would be false.
        src.detail = (
            wind + f". The air does hold energy for storms ({cape:.0f} J/kg), "
            "but the models spread the day's rain out rather than dropping it "
            "in one burst, so nothing is lining up to set that energy off. "
            "Rain on a day like this is **incidental rather than driven** — "
            "mostly light and scattered, with the outside chance of a local "
            "pop-up storm, likeliest over the hills in the afternoon, that no "
            "model can place in advance.")
    else:
        src.detail = (
            wind + ", and the air is not unstable enough for storms to build "
            "on their own. Rain on a day like this is **incidental rather "
            "than driven** — whatever falls will be light, scattered and hard "
            "to place in advance.")
    return src


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

THUNDER_WORDS = {
    "likely": "**Thunderstorms likely.**",
    "possible": "**Thunderstorms possible.**",
    "none": "",
}


def render(src: RainSource | None, *, day_label: str = "today") -> str:
    """The 'what kind of rain' section."""
    if src is None:
        return ""
    out = "## What kind of rain this is\n\n"
    out += (
        "Millimetres alone cannot tell you what a day will feel like. Twelve "
        "millimetres of monsoon rain is a grey, wet, hours-long day; twelve "
        "from a thunderstorm is one violent quarter of an hour and sunshine "
        "either side. This names the driver.\n\n"
    )
    out += f"**{day_label.capitalize()}: {src.headline}.**\n\n{src.detail}\n\n"

    if src.thunder_risk != "none":
        burst = src.burst_share
        out += (
            f"{THUNDER_WORDS[src.thunder_risk]} "
            + (f"About **{burst:.0%}** of the day's rain is modelled to fall "
               "in a single hour, which is the shape of a shower rather than "
               "of steady rain"
               if burst is not None else
               "The rainfall is modelled as showery rather than steady")
            + ". Treat this as a risk, not a promise — stored energy only "
              "becomes a storm if something sets it off, and plenty of days "
              "with this much fuel produce nothing at all.\n\n"
        )

    if src.terrain_note:
        out += f"{src.terrain_note}\n\n"

    if src.contributors:
        out += ("Also in play: " + "; ".join(src.contributors) + ".\n\n")

    out += (
        "> The driver is read from the wind direction about a kilometre and a "
        "half up, whether the models drop the rain in bursts or spread it out, "
        "the energy available for storms, and how close any low pressure system "
        "is. Where two of those disagree the page says so rather than picking "
        "one.\n"
    )
    return out


def week_summary(sources: Sequence[tuple[str, RainSource]]) -> str:
    """A line per day naming the driver, for the weekly bulletin."""
    if not sources:
        return ""
    out = "**What drives the rain each day**\n\n"
    out += "| Day | Driver | Character |\n|---|---|---|\n"
    for label, s in sources:
        thundery = ("thundery" if s.thunder_risk == "likely"
                    else "some thunder" if s.thunder_risk == "possible"
                    else "steady" if s.key in ("sw_monsoon", "system")
                    else "light/scattered")
        out += f"| **{label}** | {s.label} | {thundery} |\n"
    out += ("\n> A change of driver matters more than a change of total. "
            "Monsoon rain and easterly thunderstorms fall on opposite sides "
            "of the Ghats, so the same millimetres land in different places.\n")
    return out
