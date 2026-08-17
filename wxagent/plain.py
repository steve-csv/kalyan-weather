"""
Plain-language forecasts and major weather-shift alerts.

Two jobs:

1. WRITE FOR SOMEONE WHO IS NOT A METEOROLOGIST. Guide s29 is blunt about why
   this matters: "People rarely pay for a weather map. They pay for a decision:
   whether to pour concrete, schedule an outdoor event, begin a trek, move
   equipment, protect stock, alter transport timing." A line containing
   "terrain-normal component +11.2 m/s" answers none of those. The technical
   read stays available underneath - it is not dumbed down, it is translated.

   Rules applied here: no pressure levels, no J/kg, no m/s, no hPa, no regime
   codenames. Say when, say how heavy, say what to do, say how sure. Guide s27
   still governs the content - occurrence, character, timing and confidence
   stay separate - only the vocabulary changes.

2. FLAG THE TRANSITIONS. A seven-day table of similar-looking days buries the
   one thing that actually matters: the day the pattern changes. Handbook Ch.13
   makes this the central skill - "telling the two apart, a few days ahead...
   is one of the most valuable specific skills this guide can give you." This
   module scans the sequence for those turns and surfaces them at the top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Sequence

from . import config as C

# --------------------------------------------------------------------------
# Plain-language vocabulary
# --------------------------------------------------------------------------

# The register here is the one Konkan weather-watchers actually write in:
# proper meteorological terms - spells, isolated, waterlogging, active spell -
# used plainly and without textbook citations. Not jargon, not baby-talk.
# "Intermittent spells with dry gaps" tells a reader more than either
# "R-25mm/24h" or "a bit of rain on and off".

BAND_PLAIN = {
    "No / trace rain": "Dry",
    "Light rain": "Light rain",
    "Moderate rain": "Moderate rain",
    "Heavy rain": "Heavy rain",
    "Very heavy rain": "Very heavy rain",
    "Extremely heavy rain": "Extremely heavy rain",
}

BAND_ADVICE = {
    "No / trace rain": "No rain gear needed.",
    "Light rain": "Umbrella. No disruption expected.",
    "Moderate rain": "Umbrella. Minor waterlogging possible in low-lying spots.",
    "Heavy rain": "Waterlogging and traffic delays likely. Avoid low-lying "
                  "roads and underpasses; leave early.",
    "Very heavy rain": "Serious waterlogging likely. Avoid non-essential "
                       "travel, especially around high tide. Follow IMD warnings.",
    "Extremely heavy rain": "Stay put. Follow IMD warnings and civic instructions.",
}

CHARACTER_PLAIN = {
    "essentially dry": "",
    "continuous / steady": "Continuous spells through much of the day.",
    "organised with embedded heavy bursts": "Moderate-heavy spells with "
                                            "isolated intense bursts embedded.",
    "intermittent spells": "Intermittent spells with dry gaps between.",
    "isolated showers": "Isolated showers — highly localised, one suburb can "
                        "get hit while the next stays dry.",
}

CONFIDENCE_PLAIN = {
    "high": "High confidence",
    "moderate-high": "Fairly high confidence",
    "moderate": "Moderate confidence",
    "low": "Low confidence",
    "very low": "Very low confidence",
}

# Canonical daypart bands, shared by the text, the donut and the heatmap so
# all three describe the same slices of the day.
DAYPART_ORDER: tuple[tuple[str, int, int], ...] = (
    ("Pre-dawn", 0, 6),
    ("Morning", 6, 12),
    ("Afternoon", 12, 17),
    ("Evening", 17, 21),
    ("Night", 21, 24),
)

DAYPART_PLAIN = {
    "Early morning": "pre-dawn",
    "Morning": "through the morning",
    "Afternoon": "through the afternoon",
    "Evening": "in the evening",
    "Night": "overnight",
}


def _rain_probability_words(pct: float | None) -> str:
    if pct is None:
        return "rain possible"
    if pct >= 90:
        return "near certain"
    if pct >= 70:
        return "very likely"
    if pct >= 50:
        return "likely"
    if pct >= 30:
        return "possible"
    if pct >= 10:
        return "unlikely"
    return "not expected"


# --------------------------------------------------------------------------
# Day-level plain summary
# --------------------------------------------------------------------------

@dataclass
class PlainDay:
    day: date
    headline: str
    detail: str
    advice: str
    confidence: str
    icon: str
    # Kept apart so the renderer can suppress whichever part repeats. The
    # character of the rain is often unchanged for days while the timing moves
    # around; repeating the character sentence seven times drowns the timing,
    # which is the bit worth reading.
    character_text: str = ""
    timing_text: str = ""
    extra_text: str = ""


def _icon(band: str, character: str) -> str:
    if band in ("Very heavy rain", "Extremely heavy rain"):
        return "⛈️"
    if band == "Heavy rain":
        return "🌧️"
    if band == "Moderate rain":
        return "🌦️"
    if band == "Light rain":
        return "🌥️"
    return "☀️"


def day_facts(dd, conf, windows: Sequence) -> dict:
    """
    The raw facts a plain-language day description is built from.

    Stored alongside the finished sentences so the wording can be regenerated
    later without re-fetching anything. Previously only the finished strings
    were kept, which meant every change to the phrasing needed a full rebuild -
    and therefore an API budget - to take effect. The facts are cheap; keeping
    them makes re-wording free.
    """
    ep = dd.ensemble
    wet = [w for w in windows if w.total_mm >= 1.0]
    heaviest = max(wet, key=lambda w: w.total_mm) if wet else None
    return {
        "band": dd.rain.category_hi,
        "rainLo": round(dd.rain.lo, 1),
        "rainHi": round(dd.rain.hi, 1),
        "pct": (round(ep.p_measurable * 100)
                if ep and ep.p_measurable is not None else None),
        "character": dd.organisation.character,
        "peakRate": round(dd.organisation.max_hourly or 0.0, 1),
        "heaviestWindow": heaviest.label if heaviest else None,
        "onlyWindow": bool(heaviest and len(wet) == 1),
        "confOccurrence": conf.occurrence,
        "confAmount": conf.amount,
    }


def describe_from_facts(day: date, f: dict) -> PlainDay:
    """
    Rebuild a day description from stored facts.

    Mirrors describe_day exactly; the two must stay in step, which is why
    describe_day itself is implemented on top of this.
    """
    band = f.get("band", "No / trace rain")
    plain_band = BAND_PLAIN.get(band, band)
    pct = f.get("pct")
    rain_hi = f.get("rainHi") or 0.0

    if rain_hi < C.MEASURABLE_RAIN_MM:
        headline = f"Mostly dry — rain {_rain_probability_words(pct)}."
    else:
        headline = f"{plain_band} — {_rain_probability_words(pct)}."

    character_text = CHARACTER_PLAIN.get(f.get("character", ""), "")

    timing_text = ""
    if f.get("heaviestWindow"):
        when = DAYPART_PLAIN.get(f["heaviestWindow"], f["heaviestWindow"].lower())
        timing_text = f"Heaviest {when}."
        if f.get("onlyWindow"):
            timing_text += " Dry the rest of the day."

    extras: list[str] = []
    peak = f.get("peakRate") or 0.0
    if peak >= C.HEAVY_SPELL_MM_PER_H:
        extras.append(f"Peak intensity around {peak:.0f} mm/hr — enough to "
                      "waterlog roads within the hour.")
    if rain_hi >= C.HEAVY_DAY_MM:
        extras.append("Check tide timing — heavy rain near high tide drains "
                      "far worse here.")
    extra_text = " ".join(extras)

    detail = " ".join(p for p in (character_text, timing_text, extra_text) if p)
    if not detail:
        detail = "No significant rain signal."

    occ_key = f.get("confOccurrence", "moderate")
    amt_key = f.get("confAmount", "moderate")
    occ = CONFIDENCE_PLAIN.get(occ_key, occ_key)
    amt = CONFIDENCE_PLAIN.get(amt_key, amt_key).lower()
    if rain_hi < C.MEASURABLE_RAIN_MM:
        conf_line = f"{occ} — dry."
    elif occ_key == amt_key:
        conf_line = f"{occ}."
    else:
        conf_line = f"{occ} on rain, {amt.replace(' confidence', '')} on amount."

    return PlainDay(
        day=day, headline=headline, detail=detail,
        advice=BAND_ADVICE.get(band, ""), confidence=conf_line,
        icon=_icon(band, f.get("character", "")),
        character_text=character_text, timing_text=timing_text,
        extra_text=extra_text,
    )


def describe_day(dd, conf, windows: Sequence, *,
                 site_name: str = "Kalyan West") -> PlainDay:
    """
    Turn one DayDiagnosis into language a non-specialist can act on.

    Implemented on top of describe_from_facts so there is exactly one place
    where wording is decided - a second copy would drift the moment either was
    edited, and the two are used interchangeably (fresh build vs re-render).
    """
    return describe_from_facts(dd.day, day_facts(dd, conf, windows))


# --------------------------------------------------------------------------
# Area-level summary across the MMR
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Forecaster vocabulary
# --------------------------------------------------------------------------
# The regional convention (IMD's, and used by the Konkan forecasters worth
# reading) pairs a COVERAGE word with an INTENSITY word: "isolated very heavy",
# "fairly widespread moderate-heavy". Coverage describes how much of the area
# gets it; intensity describes how hard. Collapsing the two into one number is
# what makes a forecast unusable at area scale - "50 mm" says nothing about
# whether that is everywhere or one suburb.

def coverage_word(fraction: float) -> str:
    """Share of an area's sites reaching the threshold -> coverage term."""
    if fraction >= 0.90:
        return "widespread"
    if fraction >= 0.60:
        return "fairly widespread"
    if fraction >= 0.30:
        return "scattered"
    return "isolated"


def intensity_word(mm: float) -> str:
    """Daily total -> the intensity term, on IMD's bands."""
    if mm >= 244.5:
        return "extremely heavy"
    if mm >= 124.5:
        return "very heavy"
    if mm >= 64.5:
        return "heavy"
    if mm >= 35.0:
        return "moderate-heavy"
    if mm >= 15.6:
        return "moderate"
    if mm >= 2.5:
        return "light"
    return "dry"


def spell_phrase(typical_mm: float, peak_mm: float, coverage: float) -> str:
    """
    Compose the phrase the way it is actually written in this region -
    "mostly moderate-heavy with isolated very heavy spells".
    """
    base = intensity_word(typical_mm)
    peak = intensity_word(peak_mm)
    cov = coverage_word(coverage)

    if base == "dry" and peak == "dry":
        return "largely dry"
    if peak == base:
        return f"{cov} {base}"
    return f"mostly {base} with isolated {peak} spells"


@dataclass
class AreaSummary:
    key: str
    name: str
    character: str
    week_mm: float
    week_lo: float
    week_hi: float
    wettest_day: date | None
    wettest_mm: float
    peak_site_mm: float
    coverage: float
    band: str
    plain: str
    accumulation: str = ""
    rank: int = 0


def summarise_areas(forecasts: dict, days: Sequence[date],
                    areas: Sequence) -> list[AreaSummary]:
    """
    Roll site forecasts up into the named MMR areas.

    Rain across the MMR is not one number. Guide s11 lists five distinct
    rainfall environments inside the Mumbai-Pune corridor, and the whole point
    of the terrain analysis is that they diverge. An area-level roll-up keeps
    that visible without demanding the reader interpret eleven rows of grid
    points.
    """
    from .diagnostics import daily_rain_spread, imd_category, window_indices

    out: list[AreaSummary] = []
    for area in areas:
        per_day: list[tuple[date, float]] = []
        site_weeks: dict[str, float] = {}
        coverage_hits = coverage_total = 0

        for d in days:
            vals: list[float] = []
            for key in area.sites:
                pf = forecasts.get(key)
                if pf is None:
                    continue
                idx = window_indices(pf.times, d, 0, 24)
                if not idx:
                    continue
                v = daily_rain_spread(pf, idx).median
                vals.append(v)
                site_weeks[key] = site_weeks.get(key, 0.0) + v
                coverage_total += 1
                if v >= C.MEASURABLE_RAIN_MM:
                    coverage_hits += 1
            if vals:
                per_day.append((d, sum(vals) / len(vals)))
        if not per_day:
            continue

        week = sum(v for _, v in per_day)
        wettest_day, wettest_mm = max(per_day, key=lambda kv: kv[1])
        band = imd_category(wettest_mm)[0]

        week_lo = min(site_weeks.values()) if site_weeks else week
        week_hi = max(site_weeks.values()) if site_weeks else week
        coverage = (coverage_hits / coverage_total) if coverage_total else 0.0

        # Peak single-day at the wettest site - the "isolated spots" figure.
        peak_site_mm = 0.0
        for key in area.sites:
            pf = forecasts.get(key)
            if pf is None:
                continue
            for d in days:
                idx = window_indices(pf.times, d, 0, 24)
                if idx:
                    peak_site_mm = max(peak_site_mm,
                                       daily_rain_spread(pf, idx).hi)

        # Accumulation, phrased the way this region's forecasters write it.
        # A one-site area has no internal spread, so a "60-60 mm" range is
        # noise - say a single figure and let the reader see it is a point.
        # Collapse to one figure whenever the range vanishes at the precision
        # actually printed - "around 63-63 mm" is just noise.
        collapsed = f"{week_lo:.0f}" == f"{week_hi:.0f}"
        if week_hi < 5:
            accumulation = "negligible"
        elif collapsed:
            accumulation = f"around {week_hi:.0f} mm"
        elif abs(week_hi - week_lo) < 8:
            accumulation = f"around {week_lo:.0f}–{week_hi:.0f} mm"
        else:
            accumulation = f"{week_lo:.0f}–{week_hi:.0f} mm"
        # "isolated spots touching X" is reserved for a genuinely notable
        # single-day total - the IMD heavy threshold. Attaching it to a 26 mm
        # day devalues the phrase for the day it actually matters.
        if peak_site_mm >= C.HEAVY_DAY_MM and week_hi >= 5:
            accumulation += (f", isolated spots touching "
                             f"{peak_site_mm:.0f} mm in a day")

        spell = spell_phrase(week / max(1, len(per_day)), wettest_mm, coverage)

        if wettest_mm < C.MEASURABLE_RAIN_MM:
            plain = "Little or no rain expected all week."
        elif wettest_mm >= C.HEAVY_DAY_MM:
            plain = (f"{spell.capitalize()}. Wettest {wettest_day:%A} — expect "
                     "waterlogging and delays that day.")
        else:
            plain = f"{spell.capitalize()}. Wettest {wettest_day:%A}."

        out.append(AreaSummary(
            key=area.key, name=area.name, character=area.character,
            week_mm=week, week_lo=week_lo, week_hi=week_hi,
            wettest_day=wettest_day, wettest_mm=wettest_mm,
            peak_site_mm=peak_site_mm, coverage=coverage,
            band=band, plain=plain, accumulation=accumulation))

    out.sort(key=lambda a: -a.week_mm)
    for i, a in enumerate(out, 1):
        a.rank = i
    return out


def render_areas(areas: Sequence[AreaSummary], *,
                 home_key: str = "kalyan_belt") -> str:
    if not areas:
        return "_Area breakdown unavailable this run._\n"

    out = ("| Area | Weekly accum. | Wettest day | Character |\n"
           "|---|---|---|---|\n")
    for a in areas:
        star = " ★" if a.key == home_key else ""
        out += (f"| **{a.name}**{star} | {a.accumulation} | "
                f"{a.wettest_day:%a %d} | {a.plain} |\n")

    wettest, driest = areas[0], areas[-1]
    spread = wettest.week_mm - driest.week_mm
    out += (f"\n> **The MMR is not one forecast.** {wettest.name} takes about "
            f"{wettest.week_mm:.0f} mm this week against {driest.week_mm:.0f} mm "
            f"for {driest.name} — a {spread:.0f} mm spread across a region you "
            "can drive across in two hours. Guide §11: five distinct rainfall "
            "environments sit inside this corridor, and a single 'Mumbai' "
            "number hides all of them.\n")

    home = next((a for a in areas if a.key == home_key), None)
    if home is not None:
        out += (f"\n> **{home.name}** (★) ranks {home.rank} of {len(areas)} "
                f"for rain this week. {home.character}\n")
    return out


# --------------------------------------------------------------------------
# The weekend
# --------------------------------------------------------------------------

@dataclass
class Weekend:
    days: list[PlainDay]
    verdict: str
    plans: str


def weekend_days(plain_days: Sequence[PlainDay]) -> list[PlainDay]:
    """The next Saturday and Sunday inside the forecast window."""
    return [p for p in plain_days if p.day.weekday() in (5, 6)]


def summarise_weekend(plain_days: Sequence[PlainDay],
                      diagnoses: Sequence) -> Weekend | None:
    """
    A verdict on the weekend specifically - the question most people are
    actually asking a forecast, and one a seven-row table answers badly.
    """
    wknd = weekend_days(plain_days)
    if not wknd:
        return None

    by_day = {d.day: d for d in diagnoses}
    wet_days = 0
    heavy_days = 0
    best = worst = None
    best_mm = 1e9
    worst_mm = -1.0

    for p in wknd:
        dd = by_day.get(p.day)
        if dd is None:
            continue
        mm = dd.rain.hi
        if mm >= C.MEASURABLE_RAIN_MM:
            wet_days += 1
        if mm >= C.HEAVY_DAY_MM:
            heavy_days += 1
        if mm < best_mm:
            best_mm, best = mm, p
        if mm > worst_mm:
            worst_mm, worst = mm, p

    if heavy_days:
        verdict = "Wet weekend, and heavy at times."
        plans = ("Not a weekend for anything outdoors that can't be moved "
                 "indoors at short notice.")
    elif wet_days == len(wknd) and worst_mm >= 15.6:
        verdict = "Wet both days."
        plans = ("Outdoor plans will get rained on. Indoor backup worth having.")
    elif wet_days == len(wknd):
        verdict = "Rain both days, but nothing heavy."
        plans = ("Outdoor plans are fine with an umbrella. Nothing should get "
                 "called off.")
    elif wet_days:
        drier = best.day.strftime("%A") if best else ""
        verdict = f"Mixed — {drier} is the drier day."
        plans = f"If you can pick, do outdoor things on {drier}."
    else:
        verdict = "Dry weekend."
        plans = "Good for anything outdoors."

    if best is not None and worst is not None and best.day != worst.day \
            and worst_mm > best_mm * 1.6 and worst_mm >= 5:
        plans += (f" {worst.day:%A} looks the wetter of the two.")

    return Weekend(days=wknd, verdict=verdict, plans=plans)


def render_weekend(wk: Weekend | None) -> str:
    if wk is None:
        return ""
    out = f"**{wk.verdict}** {wk.plans}\n\n"
    for p in wk.days:
        out += (f"**{p.icon} {p.day:%A %d %B}** — {p.headline}  \n"
                f"{p.detail}  \n"
                f"_{p.advice}_  \n"
                f"<sub>{p.confidence}</sub>\n\n")
    return out


# --------------------------------------------------------------------------
# The nowcast line
# --------------------------------------------------------------------------

def nowcast_line(short, areas: Sequence[AreaSummary], now: datetime, *,
                 home_area: str = "kalyan_belt") -> str:
    """
    One timestamped line, written the way a live nowcast is written here:
    time, which part of the MMR, what is coming, what to do about it.

    Deliberately short. This is the line that would be read on a phone while
    someone decides whether to leave now or wait twenty minutes, and every
    extra clause makes it less likely to be read at all.
    """
    # %-I (no zero pad) is POSIX-only and raises on Windows, so pad and strip.
    stamp = now.strftime("%I:%M %p").lstrip("0")

    home = next((a for a in areas if a.key == home_area), None)
    label = home.name if home else "the MMR"

    if short is None or short.total_mm < 0.5:
        return (f"**{stamp}** – {label}: nothing significant on the models for "
                "the next few hours. Check radar before you rely on that — a "
                "cell already formed will beat the model to it.")

    if short.peak_mm_h >= C.HEAVY_SPELL_MM_PER_H:
        return (f"**{stamp}** – {label}: heavy downpours lined up, likely to "
                "cause waterlogging over the next few hours. Travel "
                "accordingly. ⛈️")

    when = (f" from about {short.first_wet_hour:%H:%M}"
            if short.first_wet_hour else "")
    trend = {"increasing": "picking up", "easing": "easing off",
             "steady": "steady", "dry": "petering out"}.get(short.trend, "")
    return (f"**{stamp}** – {label}: spells of rain{when}, "
            f"{trend} through the next few hours. "
            f"About {short.total_mm:.0f} mm expected in that window. 🌧️")


# --------------------------------------------------------------------------
# Verification in public - what we said last time, and what happened
# --------------------------------------------------------------------------

def verification_line(path=None) -> str:
    """
    Surface the most recent verified forecast against what was observed.

    Guide s29.1 makes publishing the misses a precondition for being trusted,
    and the forecasters worth reading in this region routinely open with
    whether their last call worked out. This reads the forecast log rather than
    asserting anything - if nothing has been verified yet, it says so.
    """
    import csv

    path = path or C.FORECAST_LOG
    if not path.exists():
        return ""

    rows = []
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("outcome")]
    except OSError:
        return ""
    if not rows:
        return ("_No verified forecasts on record yet. Scores will appear here "
                "once outcomes are logged — including the misses._")

    last = rows[-1]
    outcome = last.get("outcome", "")
    actual = last.get("actual_mm", "?")
    lo, hi = last.get("model_lo_mm", "?"), last.get("model_hi_mm", "?")
    when = last.get("valid_date", "")

    verdict = {
        "hit": "called correctly",
        "correct_dry": "correctly called dry",
        "miss": "**missed** — rain was forecast unlikely and it rained",
        "false_alarm": "**false alarm** — rain was forecast and it stayed dry",
    }.get(outcome, outcome)

    return (f"_Last verified: **{when}** — forecast {lo}–{hi} mm, observed "
            f"{actual} mm. {verdict.capitalize()}._")


# --------------------------------------------------------------------------
# Weather shift alerts
# --------------------------------------------------------------------------

SEVERITY_ORDER = {"critical": 0, "warning": 1, "watch": 2, "info": 3}


@dataclass
class ShiftAlert:
    severity: str        # critical | warning | watch | info
    when: date | None
    title: str
    body: str
    icon: str

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 9)


def _band_rank(band: str) -> int:
    order = ["No / trace rain", "Light rain", "Moderate rain",
             "Heavy rain", "Very heavy rain", "Extremely heavy rain"]
    return order.index(band) if band in order else 0


def detect_shifts(diagnoses: Sequence, *,
                  thermal_outlook=None,
                  systems_picture=None) -> list[ShiftAlert]:
    """
    Scan the forecast sequence for the transitions worth interrupting someone
    over. A run of similar days generates nothing; the turn generates an alert.
    """
    alerts: list[ShiftAlert] = []
    if not diagnoses:
        return alerts

    # ---- rain intensity step-ups ----------------------------------------
    for prev, cur in zip(diagnoses, diagnoses[1:]):
        jump = _band_rank(cur.rain.category_hi) - _band_rank(prev.rain.category_hi)
        if jump >= 2:
            alerts.append(ShiftAlert(
                "warning", cur.day,
                f"Sharp jump in rain on {cur.day:%A %d %b}",
                f"Goes from {BAND_PLAIN.get(prev.rain.category_hi, '').lower()} "
                f"to {BAND_PLAIN.get(cur.rain.category_hi, '').lower()} in a "
                "single day. Plan around this one rather than the days either "
                "side of it.",
                "⚠️"))
        elif jump <= -2:
            alerts.append(ShiftAlert(
                "info", cur.day,
                f"Rain eases markedly on {cur.day:%A %d %b}",
                f"Drops from {BAND_PLAIN.get(prev.rain.category_hi, '').lower()} "
                f"to {BAND_PLAIN.get(cur.rain.category_hi, '').lower()} — the "
                "first realistic window for outdoor work.",
                "🌤️"))

    # ---- heavy-rain days -------------------------------------------------
    for dd in diagnoses:
        ep = dd.ensemble
        p_heavy = ep.p_heavy if ep and ep.p_heavy is not None else 0.0
        if dd.rain.hi >= 124.5 or p_heavy >= 0.30:
            alerts.append(ShiftAlert(
                "critical", dd.day,
                f"Very heavy rain possible on {dd.day:%A %d %b}",
                f"Guidance reaches {dd.rain.hi:.0f} mm"
                + (f" and {p_heavy:.0%} of ensemble members cross the heavy-rain "
                   "threshold" if p_heavy else "")
                + ". Serious flooding risk, especially if it lands near high "
                  "tide. Follow IMD warnings for this one — they are "
                  "authoritative and this is not.",
                "🚨"))
        elif dd.rain.hi >= C.HEAVY_DAY_MM or p_heavy >= 0.15:
            alerts.append(ShiftAlert(
                "warning", dd.day,
                f"Heavy rain likely on {dd.day:%A %d %b}",
                "Expect waterlogging and travel delays. Check the tide timing "
                "if you have to be out.",
                "🌧️"))

    # ---- regime transitions ---------------------------------------------
    for prev, cur in zip(diagnoses, diagnoses[1:]):
        if prev.regime == cur.regime:
            continue
        if "SURGE" in cur.regime and "SURGE" not in prev.regime:
            alerts.append(ShiftAlert(
                "warning", cur.day,
                f"Monsoon surge begins {cur.day:%A %d %b}",
                "The wind swings round to drive straight at the Ghats and the "
                "air is moist all the way up. That combination is what produces "
                "days of repeated, persistent rain rather than passing showers.",
                "🌀"))
        elif "BREAK" in cur.regime and "BREAK" not in prev.regime:
            alerts.append(ShiftAlert(
                "info", cur.day,
                f"Monsoon takes a break from {cur.day:%A %d %b}",
                "The onshore wind falls away. Expect longer dry gaps, more sun, "
                "and muggier air — with the odd sharp local thunderstorm rather "
                "than steady rain.",
                "☀️"))

    # ---- dry spell ending / starting ------------------------------------
    wet = [d.rain.hi >= C.MEASURABLE_RAIN_MM for d in diagnoses]
    for i in range(1, len(wet)):
        if wet[i] and not any(wet[max(0, i - 3):i]):
            alerts.append(ShiftAlert(
                "watch", diagnoses[i].day,
                f"Rain returns {diagnoses[i].day:%A %d %b}",
                "First meaningful rain after a dry run.", "🌦️"))
            break
    for i in range(1, len(wet)):
        if not wet[i] and all(wet[max(0, i - 3):i]) and i + 1 < len(wet) and not wet[i + 1]:
            alerts.append(ShiftAlert(
                "info", diagnoses[i].day,
                f"Dry window opens {diagnoses[i].day:%A %d %b}",
                "At least two drier days in a row — the realistic slot for "
                "anything that needs to stay dry.", "🌤️"))
            break

    # ---- thermal ---------------------------------------------------------
    if thermal_outlook is not None:
        if len(thermal_outlook.heat_spell) >= 2:
            alerts.append(ShiftAlert(
                "critical", thermal_outlook.heat_spell[0],
                f"Heatwave criteria met from {thermal_outlook.heat_spell[0]:%A %d %b}",
                f"{len(thermal_outlook.heat_spell)} consecutive days meet IMD's "
                "temperature criteria. Heat is a genuine health hazard for the "
                "elderly, outdoor workers and anyone without reliable shade or "
                "water. Defer to official IMD heat warnings.",
                "🔥"))
        elif len(thermal_outlook.heat_spell) == 1:
            alerts.append(ShiftAlert(
                "watch", thermal_outlook.heat_spell[0],
                f"One very hot day, {thermal_outlook.heat_spell[0]:%A %d %b}",
                "Meets the temperature criteria but not IMD's "
                "two-consecutive-day rule, so no heatwave would be declared. "
                "The heat risk on the day itself is unchanged.",
                "🌡️"))

        if len(thermal_outlook.cold_spell) >= 2:
            alerts.append(ShiftAlert(
                "watch", thermal_outlook.cold_spell[0],
                f"Unusually cold nights from {thermal_outlook.cold_spell[0]:%A %d %b}",
                "Cold by local standards — Handbook Ch.18 notes a formal cold "
                "wave essentially never applies here, but a sharp cold snap "
                "does happen after a western disturbance passes.",
                "🥶"))

        hot = [d for d in thermal_outlook.days
               if d.heat_index is not None and d.heat_index >= 41]
        if hot and not thermal_outlook.heat_spell:
            alerts.append(ShiftAlert(
                "watch", hot[0].day,
                f"Oppressive humid heat {hot[0].day:%A %d %b}",
                f"Feels like {hot[0].heat_index:.0f}°C even though the actual "
                "temperature stays well under any heatwave threshold. In a "
                "humid coastal city these measure different things — no "
                "heatwave declared does not mean no heat risk.",
                "🥵"))

    # ---- synoptic systems ------------------------------------------------
    if systems_picture is not None:
        for a in systems_picture.significant:
            sev = "warning" if a.relevance == "high" else "watch"
            alerts.append(ShiftAlert(
                sev, None, a.headline, a.reasoning, "🌀"))
        if systems_picture.cyclone_window:
            alerts.append(ShiftAlert(
                "info", None, "Arabian Sea cyclone window",
                "This month sits in one of the two cyclone windows for this "
                "coast. Track official IMD cyclone bulletins — they are "
                "authoritative and this tool explicitly is not.",
                "🌪️"))

    # De-duplicate by title, keep the most severe, then order.
    seen: dict[str, ShiftAlert] = {}
    for a in alerts:
        prev = seen.get(a.title)
        if prev is None or a.rank < prev.rank:
            seen[a.title] = a
    ordered = sorted(seen.values(),
                     key=lambda a: (a.rank, a.when or date.max))
    return ordered


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

SEVERITY_LABEL = {
    "critical": "ACT",
    "warning": "PREPARE",
    "watch": "BE AWARE",
    "info": "FYI",
}


def render_alerts(alerts: Sequence[ShiftAlert]) -> str:
    if not alerts:
        return ("**No major weather shifts** in the next seven days — the "
                "pattern holds steady throughout.\n")
    out = ""
    for a in alerts:
        out += (f"- {a.icon} **[{SEVERITY_LABEL.get(a.severity, a.severity.upper())}] "
                f"{a.title}**  \n  {a.body}\n")
    return out


def render_plain_week(plain_days: Sequence[PlainDay]) -> str:
    """
    Render the week, collapsing repetition.

    A settled week produces seven days with genuinely identical descriptions.
    Printing the same sentence seven times is accurate and useless - it reads as
    machine output and hides the days that DO differ. So an unchanged day says
    so in one line, and the full description returns only when something
    actually changes.
    """
    out = ""
    prev: PlainDay | None = None

    for p in plain_days:
        parts: list[str] = []

        # The character sentence only earns its place when it changes.
        if prev is None or p.character_text != prev.character_text:
            if p.character_text:
                parts.append(p.character_text)
        # Timing almost always differs and is the useful bit - always shown.
        if p.timing_text:
            parts.append(p.timing_text)
        if p.extra_text:
            parts.append(p.extra_text)

        detail = " ".join(parts)
        line = f"**{p.icon} {p.day:%A %d %B}** — {p.headline}"
        if detail:
            line += f"  \n{detail}"
        # Advice repeats freely when the band is unchanged; drop it then.
        if prev is None or p.advice != prev.advice:
            line += f"  \n_{p.advice}_"
        line += f"  \n<sub>{p.confidence}</sub>\n\n"

        out += line
        prev = p

    return out
