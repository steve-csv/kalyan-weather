"""
The rolling track record - what was forecast, what actually happened.

The deep backtest (backtest.py --deep) is the rigorous version: three monsoons,
bootstrap intervals, threshold sweeps. It is also long, statistical and aimed at
someone who wants to argue with it. This module answers the version of the
question an ordinary reader actually asks: **was it right last week?**

HOW A DAY GETS SCORED
---------------------
Forecast side: the run issued the day BEFORE the target day, pulled from
Open-Meteo's previous-runs archive. Not today's model replayed over last week -
that would be marking your own homework with the answers in front of you.

Truth side: ERA5 reanalysis.

WHY THE MOST RECENT DAYS SHOW AS "AWAITING"
-------------------------------------------
ERA5 lags real time by about five days. Every alternative for filling that gap
makes the scorecard dishonest in the same direction:

  * scoring against the model's own analysis is model-versus-model, and would
    flatter the agent precisely on the days a reader is most likely to check;
  * scoring against nothing and quietly dropping the day would hide the misses
    that are still pending.

So recent days are listed with the forecast visible and the verdict blank. A
reader can see what was claimed before the verification lands, which is the
part that makes the record checkable rather than curated.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from . import config as C
from .backtest import fetch_forecasts, fetch_truth
from .diagnostics import imd_category

# Rain / no-rain threshold for the verdict, matching the rest of the agent.
THRESHOLD = C.MEASURABLE_RAIN_MM

VERDICTS = {
    "hit": ("Correct", "rain was forecast and it rained"),
    "correct_dry": ("Correct", "no rain forecast and none fell"),
    "miss": ("Missed", "rain fell that was not forecast"),
    "false_alarm": ("False alarm", "rain was forecast and none fell"),
}


@dataclass
class DayCheck:
    day: date
    forecast_mm: float | None
    observed_mm: float | None

    @property
    def verified(self) -> bool:
        return self.observed_mm is not None and self.forecast_mm is not None

    @property
    def verdict(self) -> str | None:
        if not self.verified:
            return None
        f = self.forecast_mm >= THRESHOLD
        o = self.observed_mm >= THRESHOLD
        if f and o:
            return "hit"
        if not f and not o:
            return "correct_dry"
        return "miss" if o else "false_alarm"

    @property
    def correct(self) -> bool | None:
        v = self.verdict
        return None if v is None else v in ("hit", "correct_dry")

    @property
    def band_match(self) -> bool | None:
        """Did it get the IMD rainfall band right, not just rain/no-rain?"""
        if not self.verified:
            return None
        return imd_category(self.forecast_mm)[0] == imd_category(self.observed_mm)[0]

    @property
    def sentence(self) -> str:
        if not self.verified:
            return (f"Forecast {self.forecast_mm:.0f} mm — "
                    "still waiting on the reanalysis to score it."
                    if self.forecast_mm is not None else "No archived forecast.")
        label, why = VERDICTS[self.verdict]
        return (f"Forecast {self.forecast_mm:.0f} mm, actual "
                f"{self.observed_mm:.0f} mm — **{label}** ({why}).")


@dataclass
class Record:
    site_name: str
    days: list[DayCheck]

    @property
    def verified(self) -> list[DayCheck]:
        return [d for d in self.days if d.verified]

    @property
    def n_correct(self) -> int:
        return sum(1 for d in self.verified if d.correct)

    @property
    def accuracy(self) -> float | None:
        v = self.verified
        return (self.n_correct / len(v)) if v else None

    @property
    def band_accuracy(self) -> float | None:
        v = self.verified
        return (sum(1 for d in v if d.band_match) / len(v)) if v else None

    @property
    def mae(self) -> float | None:
        v = self.verified
        if not v:
            return None
        return statistics.mean(abs(d.forecast_mm - d.observed_mm) for d in v)

    @property
    def mae_wet(self) -> float | None:
        """Error on days it actually rained - the number that matters.

        Averaging error across dry days flatters any forecast, because both
        sides are near zero and agreeing about nothing is easy.
        """
        v = [d for d in self.verified if d.observed_mm >= THRESHOLD]
        if not v:
            return None
        return statistics.mean(abs(d.forecast_mm - d.observed_mm) for d in v)

    @property
    def base_rate(self) -> float | None:
        v = self.verified
        if not v:
            return None
        return sum(1 for d in v if d.observed_mm >= THRESHOLD) / len(v)

    @property
    def summary(self) -> str:
        """The honest headline.

        "Called rain correctly on 9 of 9 days" is the number a reader wants to
        see and the one that means least: in peak monsoon every day rains, so
        the rain/no-rain call is free and 100% is what a parrot scores. When
        the sample contains no contrast this says so outright and points at the
        numbers that do carry information - the rainfall band and the error in
        millimetres.
        """
        v = self.verified
        if not v:
            return "No days verified yet."
        br = self.base_rate
        plural = "day" if len(v) == 1 else "days"
        core = (f"Over the last **{len(v)} verified {plural}**, the one-day "
                f"forecast landed in the right IMD rainfall band on "
                f"**{self.band_accuracy:.0%}** of them, and on days it rained "
                f"the average error was **{self.mae_wet:.1f} mm**.")
        if br is not None and br >= 0.98:
            return (core + f" It also called rain correctly on all {len(v)} — "
                    "but **every one of those days rained**, so that particular "
                    "score is free and should not impress anyone. The band and "
                    "the millimetres are where the skill shows.")
        if br is not None and br <= 0.02:
            return (core + " Every one of those days was dry, so the "
                    "rain/no-rain score carries no information here.")
        return (core + f" It called rain or no-rain correctly on "
                f"**{self.n_correct} of {len(v)}** ({self.accuracy:.0%}), "
                f"against a {br:.0%} chance of rain on any given day in this "
                "stretch — that gap is the part that means something.")

    @property
    def misses(self) -> list[DayCheck]:
        return [d for d in self.verified if d.verdict == "miss"]

    @property
    def false_alarms(self) -> list[DayCheck]:
        return [d for d in self.verified if d.verdict == "false_alarm"]


TABLE_DAYS = 14      # how many recent days the table lists


def assess(site=None, *, days_back: int = 35, quiet: bool = True) -> Record | None:
    """Day-by-day forecast-vs-actual for the recent past.

    The window is wider than the table it feeds, because ERA5 does not simply
    lag by a fixed number of days - it arrives in patches. On 27 Aug 2026 the
    archive held 1-12 Aug and 21 Aug and nothing else in between, so a
    fourteen-day window landed almost entirely inside the hole and the record
    collapsed to a single verified day. Scoring over a wider span and listing
    only the recent part keeps the statistics steady while the table still
    shows what was claimed lately.
    """
    site = site or C.HOME
    end = date.today()
    start = end - timedelta(days=days_back)

    try:
        truth = fetch_truth(site, start, end)
    except Exception as exc:                          # noqa: BLE001
        if not quiet:
            print(f"  ! recent record: truth unavailable: {exc}")
        truth = {}

    try:
        fc = fetch_forecasts(site, start, end, "ecmwf_ifs025", leads=(1,))
    except Exception as exc:                          # noqa: BLE001
        if not quiet:
            print(f"  ! recent record: archived forecasts unavailable: {exc}")
        return None

    lead1 = fc.get(1, {})
    days: list[DayCheck] = []
    d = start
    while d <= end:
        days.append(DayCheck(day=d,
                             forecast_mm=lead1.get(d),
                             observed_mm=truth.get(d)))
        d += timedelta(days=1)

    # Trim leading days with neither side, which are just archive gaps.
    while days and days[0].forecast_mm is None and days[0].observed_mm is None:
        days.pop(0)
    return Record(site_name=site.name, days=days)


def render(rec: Record | None) -> str:
    """Markdown for the bulletin."""
    if rec is None or not rec.days:
        return ""
    v = rec.verified
    out = "**How it has actually done** — every day, forecast against outcome\n\n"
    if not v:
        return out + "_No days verified yet._\n"

    out += rec.summary + "\n\n"

    out += "| Day | Forecast | Actual | Verdict |\n|---|---|---|---|\n"
    for d in rec.days[-TABLE_DAYS:]:
        if d.forecast_mm is None and d.observed_mm is None:
            continue
        f = f"{d.forecast_mm:.0f} mm" if d.forecast_mm is not None else "—"
        if d.observed_mm is None:
            out += f"| {d.day:%a %d %b} | {f} | _awaiting_ | — |\n"
        else:
            out += (f"| {d.day:%a %d %b} | {f} | {d.observed_mm:.0f} mm | "
                    f"{VERDICTS[d.verdict][0]} |\n")

    if rec.misses or rec.false_alarms:
        out += "\n"
        if rec.misses:
            out += (f"**Missed days:** "
                    + ", ".join(f"{d.day:%d %b} ({d.observed_mm:.0f} mm fell, "
                                f"{d.forecast_mm:.0f} forecast)"
                                for d in rec.misses) + ". ")
        if rec.false_alarms:
            out += ("**False alarms:** "
                    + ", ".join(f"{d.day:%d %b} ({d.forecast_mm:.0f} mm "
                                f"forecast, {d.observed_mm:.0f} fell)"
                                for d in rec.false_alarms) + ".")
        out += "\n"

    out += ("\n> Forecasts are the run issued the day before each date, from "
            "the archive — not today's model replayed over the past. Truth is "
            "ERA5 reanalysis, which lags about five days, so the most recent "
            "days show their forecast with the verdict still pending.\n")
    return out
