"""
Named-belt rain arrivals - "who gets wet, and when".

The rest of the nowcast talks in bearings and kilometres, which is the right
language for reasoning and the wrong one for a person deciding whether to leave
the house. This module answers the only question most readers actually have:
**is it about to rain where I am?**

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
It is the hourly rainfall forecast sampled at named places across the MMR, then
grouped into belts people recognise and phrased in plain words.

It is NOT radar extrapolation. The agent has refused from the start to invert
IMD's radar colour scale into reflectivity and extrapolate an arrival time,
because that produces a number that looks precise and is not (nowcast.py says
so at length, and the page repeats it). Nothing here reads a radar pixel. What
this does instead is take the model's own hourly field - which is honest about
being a model - and say where and when it puts rain, in language a reader can
act on.

The practical consequence, which the page states plainly: inside an hour the
radar loop is better than this. Where this helps is in telling you what to look
for on that loop, and in covering the belts the radar image is hardest to read
by eye.

WHY BELTS RATHER THAN POINTS
----------------------------
A single grid point is noisy at the hourly scale, and "Malad" alone invites
false precision about a 2 km neighbourhood. Each belt pools two or three
locations and reports the wettest, because for "should I carry an umbrella"
the worst case across a belt is the useful answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config as C
from .sources import FetchError, _get_json

# Rain rates that mean something to a person, not to a model.
TRACE_MM_H = 0.2       # damp air, not really rain
LIGHT_MM_H = 1.0       # you would notice it
MODERATE_MM_H = 4.0    # umbrella weather
HEAVY_MM_H = 10.0      # roads start holding water
VERY_HEAVY_MM_H = 20.0 # waterlogging within the hour

HOURS_AHEAD = 4

# WHY THIS IS HOURLY AND NOT QUARTER-HOURLY
# ----------------------------------------
# Open-Meteo exposes a `minutely_15` precipitation field, and using it would
# let this section say "arriving in about 15 minutes" - which reads far better
# than "within the hour". It was tried and then removed, because for this
# location the field is not real. ECMWF has no native 15-minute output here, so
# Open-Meteo spreads the hourly value across the four quarter-hour steps:
# sampled at Lonavala, 12 of 13 hours had all four steps byte-identical. An
# arrival time quoted to the quarter-hour off that data would be invented
# precision dressed as a nowcast, which is the one thing this agent refuses to
# do. Hourly values are genuine, so hourly is what gets quoted.
STEPS_AHEAD = 4          # four hours forward
STEPS_BACK = 2           # two hours of history, for rising or easing


@dataclass(frozen=True)
class Belt:
    """A group of places people think of together."""
    key: str
    name: str
    zone: str
    points: tuple[tuple[str, float, float], ...]


# Chosen so that between them they cover the areas a Kalyan/MMR reader actually
# names: the western line, the island city, the central line out to Karjat, the
# harbour line, and the three ghat sections the user asked for by name.
BELTS: tuple[Belt, ...] = (
    Belt("western", "Malad–Borivali belt", "coastal", (
        ("Malad", 19.1860, 72.8490),
        ("Borivali", 19.2307, 72.8567),
        ("Andheri", 19.1136, 72.8697),
    )),
    Belt("island", "Dadar–Colaba (island city)", "coastal", (
        ("Dadar", 19.0176, 72.8440),
        ("Colaba", 18.9067, 72.8147),
    )),
    Belt("vasai", "Vasai–Palghar belt", "coastal", (
        ("Vasai", 19.3919, 72.8397),
        ("Palghar", 19.6967, 72.7656),
    )),
    Belt("thane_kalyan", "Thane–Kalyan–Dombivli belt", "transition", (
        ("Thane", 19.2183, 72.9781),
        ("Kalyan", 19.2437, 73.1305),
        ("Dombivli", 19.2183, 73.0868),
    )),
    Belt("badlapur", "Badlapur–Karjat belt", "transition", (
        ("Badlapur", 19.1552, 73.2650),
        ("Karjat", 18.9107, 73.3233),
    )),
    Belt("harbour", "Navi Mumbai–Panvel belt", "transition", (
        ("Panvel", 18.9894, 73.1175),
        ("Vashi", 19.0771, 72.9986),
    )),
    Belt("ghat_north", "Igatpuri (northern ghats)", "ghat", (
        ("Igatpuri", 19.6967, 73.5620),
    )),
    Belt("ghat_central", "Matheran–Lonavala (central ghats)", "ghat", (
        ("Matheran", 18.9866, 73.2707),
        ("Lonavala", 18.7546, 73.4062),
    )),
    Belt("ghat_malshej", "Malshej", "ghat", (
        ("Malshej Ghat", 19.3333, 73.7667),
    )),
)

ALL_POINTS = [(nm, la, lo) for b in BELTS for (nm, la, lo) in b.points]


@dataclass
class BeltStatus:
    belt: Belt
    now_mm_h: float = 0.0
    hours: list[float] = field(default_factory=list)   # next N hours, mm/h
    hour_labels: list[str] = field(default_factory=list)   # "21:00", ...
    wettest_place: str = ""
    state: str = "dry"   # raining | drizzling | arriving | later | dry
    eta_hours: int | None = None
    peak_mm_h: float = 0.0
    prev_mm_h: float = 0.0            # two hours ago, for rising/easing

    @property
    def intensity_word(self) -> str:
        return intensity_word(max(self.now_mm_h, self.peak_mm_h))

    @property
    def sentence(self) -> str:
        """One line a non-specialist can act on."""
        if self.state == "raining":
            w = intensity_word(self.now_mm_h)
            tail = ""
            if self.peak_mm_h > self.now_mm_h * 1.6 and self.peak_mm_h >= MODERATE_MM_H:
                tail = (f" Getting heavier over the next few hours — up to "
                        f"{rate(self.peak_mm_h)}.")
            elif self.now_mm_h >= MODERATE_MM_H and self.peak_mm_h < self.now_mm_h * 0.5:
                tail = " Easing off over the next few hours."
            elif self.prev_mm_h and self.now_mm_h > self.prev_mm_h * 1.8:
                tail = " Picking up over the last couple of hours."
            elif self.prev_mm_h > self.now_mm_h * 1.8 and self.prev_mm_h >= LIGHT_MM_H:
                tail = " Easing — it was heavier a couple of hours ago."
            return f"**Raining now** — {w}, about {rate(self.now_mm_h)}.{tail}"
        if self.state == "drizzling":
            return (f"**Spitting only** — about {rate(self.now_mm_h)}, barely "
                    "enough to wet the road.")
        if self.state == "arriving":
            # The rate AT ARRIVAL, not the four-hour peak. Quoting the peak
            # made this sentence contradict the hour-by-hour table beside it:
            # "1.0 mm/hr when it lands" over a row reading 0.6, 0.6, 0.6,
            # because the 1.0 was three hours later.
            landing = (self.hours[self.eta_hours - 1]
                       if self.eta_hours and len(self.hours) >= self.eta_hours
                       else self.peak_mm_h)
            tail = ""
            if self.peak_mm_h > landing * 1.5 and self.peak_mm_h >= LIGHT_MM_H:
                tail = f" Building to {rate(self.peak_mm_h)} after that."
            return (f"**Rain arriving within the hour** — "
                    f"{intensity_word(landing)} when it lands "
                    f"({rate(landing)}).{tail}")
        if self.state == "later":
            h = self.eta_hours or 2
            return (f"**Dry for now.** Rain expected in about "
                    f"{h} hour{'s' if h != 1 else ''} — "
                    f"{intensity_word(self.peak_mm_h)}.")
        return "**Staying dry** for the next few hours."


def rate(mm_h: float) -> str:
    """Format a rain rate. Light rates keep a decimal, because rounding 0.9
    to '1 mm/hr' and 0.4 to '0 mm/hr' both misdescribe what you would feel."""
    return f"{mm_h:.1f} mm/hr" if mm_h < 10 else f"{mm_h:.0f} mm/hr"


def intensity_word(mm_h: float) -> str:
    if mm_h >= VERY_HEAVY_MM_H:
        return "torrential"
    if mm_h >= HEAVY_MM_H:
        return "heavy"
    if mm_h >= MODERATE_MM_H:
        return "moderate"
    if mm_h >= LIGHT_MM_H:
        return "light"
    if mm_h >= TRACE_MM_H:
        return "drizzle"
    return "nothing much"


def fetch(*, now: datetime | None = None, model: str = "ecmwf_ifs025",
          quiet: bool = True) -> list[BeltStatus] | None:
    """Hourly rain at every belt point, reduced to one status per belt."""
    params = {
        "latitude": ",".join(f"{la}" for _, la, _ in ALL_POINTS),
        "longitude": ",".join(f"{lo}" for _, _, lo in ALL_POINTS),
        "hourly": "precipitation",
        "past_hours": STEPS_BACK,
        "models": model,
        "forecast_days": 1,
        "timezone": C.TIMEZONE,
    }
    try:
        raw = _get_json(C.FORECAST_URL, params, timeout=75)
    except FetchError as exc:
        if not quiet:
            print(f"  ! belt outlook unavailable: {exc}")
        return None
    if not isinstance(raw, list):
        raw = [raw]
    if len(raw) < len(ALL_POINTS):
        if not quiet:
            print(f"  ! belts: expected {len(ALL_POINTS)} points, got {len(raw)}")
        return None

    now = now or datetime.now()
    series: dict[str, tuple[list[float], list[float]]] = {}   # name -> (past, ahead)
    labels: list[str] = []      # clock labels for the hours ahead, e.g. "21:00"
    for (name, _, _), loc in zip(ALL_POINTS, raw):
        h = loc.get("hourly", {})
        times = h.get("time", [])
        vals = h.get("precipitation") or []
        cur = 0
        for i, t in enumerate(times):
            try:
                ts = datetime.fromisoformat(t)
            except ValueError:
                continue
            if ts <= now:
                cur = i
            else:
                break

        def mm(i):
            return (float(vals[i])
                    if 0 <= i < len(vals) and vals[i] is not None else 0.0)
        past = [mm(i) for i in range(max(0, cur - STEPS_BACK), cur + 1)]
        span = range(cur + 1, min(cur + 1 + STEPS_AHEAD, len(vals)))
        ahead = [mm(i) for i in span]
        series[name] = (past, ahead)
        # Every location shares one time grid, so the labels only need taking
        # once - but take them from a location that actually returned times
        # rather than assuming the first one did.
        if not labels:
            labels = [times[i][11:16] for i in span if i < len(times)]

    out: list[BeltStatus] = []
    for belt in BELTS:
        rows = [(name, series[name]) for name, _, _ in belt.points
                if name in series]
        if not rows:
            continue

        # Rank by what is falling NOW, then by what is coming - ranking on the
        # peak alone let one dry location speak for a belt whose other half was
        # already being drenched.
        best_name, (best_past, best_ahead) = max(
            rows, key=lambda r: (r[1][0][-1] if r[1][0] else 0.0,
                                 max(r[1][1]) if r[1][1] else 0.0))

        now_rate = max((p[-1] if p else 0.0) for _, (p, _) in rows)
        peak = max((max(a) if a else 0.0) for _, (_, a) in rows)
        prev_rate = max((p[0] if p else 0.0) for _, (p, _) in rows)

        # The hour-by-hour strip is the belt's worst case at each hour, to
        # match now_mm_h and peak_mm_h. Taking one location's series instead
        # would let a dry corner of the belt speak for a wet one, and the
        # strip would then disagree with the headline above it.
        n_ahead = max((len(a) for _, (_, a) in rows), default=0)
        ahead_max = [max((a[i] if i < len(a) else 0.0) for _, (_, a) in rows)
                     for i in range(n_ahead)]

        if now_rate >= LIGHT_MM_H:
            state, eta = "raining", 0
        elif now_rate >= TRACE_MM_H:
            state, eta = "drizzling", 0
        elif best_ahead and best_ahead[0] >= LIGHT_MM_H * 0.5:
            state, eta = "arriving", 1
        elif best_ahead and max(best_ahead) >= LIGHT_MM_H * 0.5:
            step = next(i for i, v in enumerate(best_ahead)
                        if v >= LIGHT_MM_H * 0.5)
            state, eta = "later", step + 1
        else:
            state, eta = "dry", None

        out.append(BeltStatus(
            belt=belt, now_mm_h=now_rate, hours=ahead_max,
            hour_labels=list(labels),
            wettest_place=best_name, state=state, eta_hours=eta,
            peak_mm_h=peak, prev_mm_h=prev_rate,
        ))
    return out


def headline(statuses: list[BeltStatus] | None) -> str:
    """One sentence covering the whole region, for the top of the section."""
    if not statuses:
        return ""
    wet = [s for s in statuses if s.state == "raining"]
    spit = [s for s in statuses if s.state == "drizzling"]
    soon = [s for s in statuses if s.state == "arriving"]
    dry = [s for s in statuses if s.state == "dry"]

    if not wet and not soon:
        if spit:
            names = ", ".join(s.belt.name for s in spit[:3])
            return (f"**No real rain anywhere across the MMR right now** — "
                    f"just spitting over {names}, and nothing heavier due "
                    "within the hour.")
        return ("**Nothing falling anywhere across the MMR right now**, and "
                "nothing due within the hour.")

    parts: list[str] = []
    if wet:
        heaviest = max(wet, key=lambda s: s.now_mm_h)
        names = ", ".join(s.belt.name for s in wet[:3])
        more = f" and {len(wet) - 3} more" if len(wet) > 3 else ""
        parts.append(f"**Rain is falling now** over {names}{more} — heaviest "
                     f"around {heaviest.wettest_place} at "
                     f"{rate(heaviest.now_mm_h)}")
    if soon:
        names = ", ".join(s.belt.name for s in soon[:3])
        parts.append(f"**reaching {names} within the hour**")
    if dry and len(dry) <= 3:
        parts.append(f"staying dry for now over "
                     f"{', '.join(s.belt.name for s in dry)}")
    return ". ".join(parts) + "."


def render(statuses: list[BeltStatus] | None) -> str:
    """Markdown for the bulletin."""
    if not statuses:
        return ""
    out = "**Where the rain is, belt by belt**\n\n"
    out += headline(statuses) + "\n\n"
    # Numbers first. The sentences underneath say what the numbers mean, but
    # "how much, and at what time" is the question they cannot answer in words.
    labels = next((x.hour_labels for x in statuses if x.hour_labels), [])[:3]
    if labels:
        out += ("| Region | Now | " + " | ".join(labels) + " |\n"
                "|---|---|" + "---|" * len(labels) + "\n")
        for s in statuses:
            cells = " | ".join(
                (f"{s.hours[i]:.1f}" if i < len(s.hours) else "—")
                for i in range(len(labels)))
            out += f"| **{s.belt.name}** | {s.now_mm_h:.1f} | {cells} |\n"
        out += "\nAll figures are mm/hr, the worst case across each belt.\n\n"

    out += "| Region | What that means |\n|---|---|\n"
    for s in statuses:
        out += f"| **{s.belt.name}** | {s.sentence} |\n"
    out += ("\n> These come from the hourly model field sampled at each place, "
            "not from the radar picture. Inside an hour the radar loop above "
            "is the better guide — this tells you which belts to watch on it.\n")
    return out
