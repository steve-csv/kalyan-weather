"""
Backtesting - scoring the agent against what actually happened.

Guide s26 is the whole basis for this module: "Accuracy alone can be
misleading. During a dry period, saying 'no rain' every day may score well."
The inverse is the real hazard here - in peak monsoon Konkan, "always say rain"
is an extremely strong baseline. So every score below is reported ALONGSIDE
that baseline, and the only number that means anything is whether the agent
beats it.

METHODOLOGY - the part that makes this a backtest rather than hindsight
-----------------------------------------------------------------------
The forecast side uses Open-Meteo's *previous runs* archive: for a target day D
it retrieves the forecast as it was actually issued 1, 2, 3 and 5 days before D.
Using today's model output for a past date would be reading the answer off the
back of the book and would show absurd skill.

The truth side uses ERA5 reanalysis daily precipitation.

  HONEST LIMITATION. ERA5 is not a rain gauge. It is itself a model product,
  and over the Western Ghats - where the whole windward/leeward contrast plays
  out across a couple of grid cells (Handbook Ch.21) - its precipitation is
  known to be least reliable. Verification against IMD gauge data would be the
  gold standard; it is not freely available by API. Treat the absolute scores
  as indicative and the RELATIVE comparisons (agent vs baseline, lead 1 vs lead
  5, surge days vs convective days) as the trustworthy part.

Regime labels are assigned from the ANALYSIS diagnostics - i.e. what the
atmosphere actually did - not from the forecast. That is deliberate: the
question being asked is "on days that turned out to be active surges, how
skilful were the forecasts?", which is a conditional-skill question.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from . import config as C
from .diagnostics import (
    _circular_mean, imd_category, orographic_component,
)
from .sources import _get_json

PREV_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HIST_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Lead times to score, in days before the target date.
LEADS = (1, 2, 3, 5)


# --------------------------------------------------------------------------
# Data acquisition
# --------------------------------------------------------------------------

def _daily_sums(times: Sequence[str], values: Sequence[float | None]
                ) -> dict[date, float]:
    out: dict[date, float] = defaultdict(float)
    seen: dict[date, bool] = {}
    for t, v in zip(times, values):
        d = datetime.fromisoformat(t).date()
        if v is not None:
            out[d] += v
            seen[d] = True
    return {d: v for d, v in out.items() if seen.get(d)}


def fetch_truth(site, start: date, end: date) -> dict[date, float]:
    """ERA5 reanalysis daily precipitation - the verification target."""
    params = {
        "latitude": site.lat, "longitude": site.lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "precipitation_sum", "timezone": C.TIMEZONE,
    }
    raw = _get_json(ARCHIVE_URL, params, timeout=90)
    if isinstance(raw, list):
        raw = raw[0]
    daily = raw.get("daily", {})
    out: dict[date, float] = {}
    for t, v in zip(daily.get("time", []), daily.get("precipitation_sum", [])):
        if v is not None:
            out[date.fromisoformat(t)] = float(v)
    return out


def fetch_forecasts(site, start: date, end: date, model: str,
                    leads: Sequence[int] = LEADS
                    ) -> dict[int, dict[date, float]]:
    """
    Forecasts for each target day as issued `lead` days beforehand.

    Returns {lead: {target_date: mm}}.
    """
    fields = ["precipitation"] + [f"precipitation_previous_day{n}" for n in leads]
    params = {
        "latitude": site.lat, "longitude": site.lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": ",".join(fields), "models": model,
        "timezone": C.TIMEZONE,
    }
    raw = _get_json(PREV_RUNS_URL, params, timeout=120)
    if isinstance(raw, list):
        raw = raw[0]
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])

    out: dict[int, dict[date, float]] = {}
    for n in leads:
        series = hourly.get(f"precipitation_previous_day{n}")
        if series:
            out[n] = _daily_sums(times, series)
    # lead 0 == the latest run, kept for reference only (near-analysis)
    if hourly.get("precipitation"):
        out[0] = _daily_sums(times, hourly["precipitation"])
    return out


def fetch_analysis_diagnostics(site, start: date, end: date
                               ) -> dict[date, dict[str, float]]:
    """
    Analysis-time ingredients, used to label each day's actual regime.

    Not used to make a forecast - only to answer "what kind of day was this?"
    """
    params = {
        "latitude": site.lat, "longitude": site.lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": ("relative_humidity_700hPa,relative_humidity_850hPa,"
                   "wind_speed_850hPa,wind_direction_850hPa,cape"),
        "models": "ecmwf_ifs025", "timezone": C.TIMEZONE,
        "wind_speed_unit": "ms",
    }
    raw = _get_json(HIST_FORECAST_URL, params, timeout=120)
    if isinstance(raw, list):
        raw = raw[0]
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])

    buckets: dict[date, dict[str, list]] = defaultdict(
        lambda: {"rh700": [], "rh850": [], "ws": [], "wd": [], "cape": []})
    for i, t in enumerate(times):
        d = datetime.fromisoformat(t).date()
        b = buckets[d]
        for key, name in (("rh700", "relative_humidity_700hPa"),
                          ("rh850", "relative_humidity_850hPa"),
                          ("ws", "wind_speed_850hPa"),
                          ("wd", "wind_direction_850hPa"),
                          ("cape", "cape")):
            series = hourly.get(name)
            if series and i < len(series) and series[i] is not None:
                b[key].append(series[i])

    out: dict[date, dict[str, float]] = {}
    for d, b in buckets.items():
        if not b["ws"] or not b["wd"]:
            continue
        speed = sum(b["ws"]) / len(b["ws"])
        direction = _circular_mean(b["wd"])
        out[d] = {
            "rh700": sum(b["rh700"]) / len(b["rh700"]) if b["rh700"] else float("nan"),
            "rh850": sum(b["rh850"]) / len(b["rh850"]) if b["rh850"] else float("nan"),
            "wind_speed": speed,
            "wind_dir": direction if direction is not None else float("nan"),
            "orographic": orographic_component(speed, direction) or 0.0,
            "cape_peak": max(b["cape"]) if b["cape"] else 0.0,
        }
    return out


# --------------------------------------------------------------------------
# Regime labelling (from analysis - what actually happened)
# --------------------------------------------------------------------------

def label_regime(diag: dict[str, float], day: date) -> str:
    """Simplified version of diagnostics.classify_regime, analysis-side."""
    orog = diag.get("orographic", 0.0)
    rh700 = diag.get("rh700", 0.0)
    cape = diag.get("cape_peak", 0.0)
    deep = rh700 >= C.RH_DEEP_700

    if C.SEASONS[day.month] == "monsoon":
        if orog >= C.OROG_STRONG and deep:
            return "Active surge"
        if orog >= C.OROG_MODERATE:
            return "Moderate onshore"
        if cape >= 800:
            return "Break / convective"
        return "Break / lull"
    if cape >= 1500:
        return "Convective risk"
    if cape >= 500:
        return "Isolated convection"
    return "Dry / settled"


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

@dataclass
class Contingency:
    hits: int = 0
    misses: int = 0
    false_alarms: int = 0
    correct_negatives: int = 0

    @property
    def n(self) -> int:
        return self.hits + self.misses + self.false_alarms + self.correct_negatives

    @property
    def accuracy(self) -> float | None:
        return (self.hits + self.correct_negatives) / self.n if self.n else None

    @property
    def pod(self) -> float | None:
        d = self.hits + self.misses
        return self.hits / d if d else None

    @property
    def far(self) -> float | None:
        d = self.hits + self.false_alarms
        return self.false_alarms / d if d else None

    @property
    def csi(self) -> float | None:
        d = self.hits + self.misses + self.false_alarms
        return self.hits / d if d else None

    @property
    def bias(self) -> float | None:
        """Frequency bias: forecast events / observed events. 1.0 is unbiased."""
        d = self.hits + self.misses
        return (self.hits + self.false_alarms) / d if d else None

    def add(self, forecast: bool, observed: bool) -> None:
        if forecast and observed:
            self.hits += 1
        elif forecast and not observed:
            self.false_alarms += 1
        elif not forecast and observed:
            self.misses += 1
        else:
            self.correct_negatives += 1


def heidke(c: Contingency) -> float | None:
    """
    Heidke Skill Score - accuracy corrected for what chance alone would give.

    This is the number that answers "does the agent beat guessing?", which raw
    accuracy cannot. 0 = no better than chance; 1 = perfect; below 0 = worse
    than chance.
    """
    a, b, cc, d = c.hits, c.false_alarms, c.misses, c.correct_negatives
    n = c.n
    if n == 0:
        return None
    expected = ((a + b) * (a + cc) + (cc + d) * (b + d)) / n
    denom = n - expected
    return ((a + d) - expected) / denom if denom else None


# --------------------------------------------------------------------------
# The backtest
# --------------------------------------------------------------------------

@dataclass
class SeasonSlice:
    """Scores restricted to one season. A period spanning a bone-dry month and
    a wet one produces a meaningless blended score, so they are kept apart."""
    label: str
    n_days: int
    base_rate: float
    agent: Contingency
    persistence: Contingency
    observed_mm: float
    forecast_mm: dict[int, float]


@dataclass
class BacktestResult:
    site_name: str
    start: date
    end: date
    n_days: int
    threshold: float
    base_rate: float
    truth: dict[date, float]
    per_lead: dict[int, Contingency]
    per_lead_model: dict[tuple[int, str], Contingency]
    baseline_always: Contingency
    baseline_never: Contingency
    baseline_persistence: Contingency
    mae_per_lead: dict[int, float]
    mae_wet_per_lead: dict[int, float]
    accum_per_lead: dict[int, float]
    observed_accum: float
    bias_per_lead: dict[int, float]
    category_hit: dict[int, float]
    regime_csi: dict[str, tuple[Contingency, int, int]]
    heavy_per_lead: dict[int, Contingency]
    heavy_days: list[tuple[date, float, dict[int, float]]]
    spread_bins: list[tuple[str, int, float]]
    seasons: list[SeasonSlice]


def run_backtest(site, start: date, end: date, *,
                 threshold: float = C.MEASURABLE_RAIN_MM,
                 models: Sequence[str] | None = None,
                 quiet: bool = False) -> BacktestResult:
    models = list(models or [m.key for m in C.MODELS])

    if not quiet:
        print(f"Backtesting {site.name}: {start} to {end}")
        print("  fetching ERA5 truth...")
    truth = fetch_truth(site, start, end)

    if not quiet:
        print("  fetching archived forecast runs...")
    fc: dict[str, dict[int, dict[date, float]]] = {}
    for m in models:
        try:
            fc[m] = fetch_forecasts(site, start, end, m)
        except Exception as exc:                  # noqa: BLE001
            print(f"  ! {m}: {exc}")
        time.sleep(0.5)

    if not quiet:
        print("  fetching analysis diagnostics for regime labels...")
    try:
        diags = fetch_analysis_diagnostics(site, start, end)
    except Exception as exc:                      # noqa: BLE001
        print(f"  ! diagnostics unavailable: {exc}")
        diags = {}

    days = sorted(d for d in truth if start <= d <= end)

    per_lead: dict[int, Contingency] = {n: Contingency() for n in LEADS}
    per_lead_model: dict[tuple[int, str], Contingency] = {}
    heavy_per_lead: dict[int, Contingency] = {n: Contingency() for n in LEADS}
    abs_err: dict[int, list[float]] = {n: [] for n in LEADS}
    abs_err_wet: dict[int, list[float]] = {n: [] for n in LEADS}
    accum: dict[int, float] = {n: 0.0 for n in LEADS}
    signed_err: dict[int, list[float]] = {n: [] for n in LEADS}
    cat_hits: dict[int, list[bool]] = {n: [] for n in LEADS}
    regime_cont: dict[str, Contingency] = defaultdict(Contingency)
    regime_n: dict[str, int] = defaultdict(int)
    regime_events: dict[str, int] = defaultdict(int)
    spread_records: list[tuple[float, bool]] = []
    heavy_days: list[tuple[date, float, dict[int, float]]] = []

    # Season slices - a dry month blended with a wet one yields a meaningless
    # aggregate, so each is scored on its own sample.
    season_days: dict[str, list[date]] = defaultdict(list)

    baseline_always = Contingency()
    baseline_never = Contingency()
    baseline_persistence = Contingency()
    observed_accum = 0.0

    for i, d in enumerate(days):
        observed_mm = truth[d]
        observed = observed_mm >= threshold
        observed_accum += observed_mm
        season_days[C.SEASONS[d.month]].append(d)

        baseline_always.add(True, observed)
        baseline_never.add(False, observed)
        if i > 0:
            prev = truth[days[i - 1]]
            baseline_persistence.add(prev >= threshold, observed)

        regime = label_regime(diags[d], d) if d in diags else "Unknown"
        regime_n[regime] += 1
        if observed:
            regime_events[regime] += 1

        if observed_mm >= C.HEAVY_DAY_MM:
            heavy_days.append(
                (d, observed_mm,
                 {n: statistics.median(v) for n in LEADS
                  if (v := [fc[m][n][d] for m in fc
                            if n in fc[m] and d in fc[m][n]])}))

        for n in LEADS:
            vals = [fc[m][n][d] for m in fc
                    if n in fc[m] and d in fc[m][n]]
            if not vals:
                continue

            median = statistics.median(vals)
            accum[n] += median
            per_lead[n].add(median >= threshold, observed)
            heavy_per_lead[n].add(median >= C.HEAVY_DAY_MM,
                                  observed_mm >= C.HEAVY_DAY_MM)
            abs_err[n].append(abs(median - observed_mm))
            if observed:
                abs_err_wet[n].append(abs(median - observed_mm))
            signed_err[n].append(median - observed_mm)
            cat_hits[n].append(imd_category(median)[0] == imd_category(observed_mm)[0])

            for m in fc:
                if n in fc[m] and d in fc[m][n]:
                    key = (n, m)
                    per_lead_model.setdefault(key, Contingency())
                    per_lead_model[key].add(fc[m][n][d] >= threshold, observed)

            if n == 1:
                regime_cont[regime].add(median >= threshold, observed)
                # Relative spread is unstable when totals are near zero, so
                # only days with a meaningful signal enter this comparison.
                if len(vals) > 1 and max(vals) >= 5.0:
                    rel_spread = (max(vals) - min(vals)) / max(vals)
                    correct = (median >= threshold) == observed
                    spread_records.append((rel_spread, correct))

    # ---- per-season slices ----------------------------------------------
    seasons: list[SeasonSlice] = []
    for season, sdays in season_days.items():
        if len(sdays) < 10:
            continue
        agent_c, persist_c = Contingency(), Contingency()
        obs_mm = sum(truth[d] for d in sdays)
        fc_mm = {n: 0.0 for n in LEADS}
        for j, d in enumerate(sdays):
            observed = truth[d] >= threshold
            vals = [fc[m][1][d] for m in fc if 1 in fc[m] and d in fc[m][1]]
            if vals:
                agent_c.add(statistics.median(vals) >= threshold, observed)
            if j > 0:
                persist_c.add(truth[sdays[j - 1]] >= threshold, observed)
            for n in LEADS:
                v = [fc[m][n][d] for m in fc if n in fc[m] and d in fc[m][n]]
                if v:
                    fc_mm[n] += statistics.median(v)
        seasons.append(SeasonSlice(
            label=C.SEASON_LABELS[season], n_days=len(sdays),
            base_rate=sum(1 for d in sdays if truth[d] >= threshold) / len(sdays),
            agent=agent_c, persistence=persist_c,
            observed_mm=obs_mm, forecast_mm=fc_mm))
    seasons.sort(key=lambda s: -s.n_days)

    # Does wide model spread actually flag a less-reliable forecast?
    spread_bins: list[tuple[str, int, float]] = []
    for label, lo, hi in (("tight (<25%)", 0.0, 0.25),
                          ("moderate (25-60%)", 0.25, 0.60),
                          ("wide (>60%)", 0.60, 1.01)):
        subset = [ok for sp, ok in spread_records if lo <= sp < hi]
        if subset:
            spread_bins.append((label, len(subset), sum(subset) / len(subset)))

    base_rate = (sum(1 for d in days if truth[d] >= threshold) / len(days)
                 if days else 0.0)

    return BacktestResult(
        site_name=site.name, start=start, end=end, n_days=len(days),
        threshold=threshold, base_rate=base_rate, truth=truth,
        per_lead=per_lead, per_lead_model=per_lead_model,
        baseline_always=baseline_always, baseline_never=baseline_never,
        baseline_persistence=baseline_persistence,
        mae_per_lead={n: (sum(v) / len(v) if v else float("nan"))
                      for n, v in abs_err.items()},
        mae_wet_per_lead={n: (sum(v) / len(v) if v else float("nan"))
                          for n, v in abs_err_wet.items()},
        accum_per_lead=accum, observed_accum=observed_accum,
        bias_per_lead={n: (sum(v) / len(v) if v else float("nan"))
                       for n, v in signed_err.items()},
        category_hit={n: (sum(v) / len(v) if v else float("nan"))
                      for n, v in cat_hits.items()},
        regime_csi={k: (v, regime_n[k], regime_events[k])
                    for k, v in regime_cont.items()},
        heavy_per_lead=heavy_per_lead, heavy_days=heavy_days,
        spread_bins=spread_bins, seasons=seasons,
    )


# --------------------------------------------------------------------------
# Terrain verification
# --------------------------------------------------------------------------

def terrain_check(start: date, end: date, *, quiet: bool = False
                  ) -> list[tuple[str, str, float, float]]:
    """
    Does the coast -> crest -> rain-shadow ordering the agent asserts actually
    verify in the truth data? (Handbook Ch.14)

    Returns [(site, zone, mean_daily_mm, total_mm)].
    """
    out = []
    for key in ("santacruz", "kalyan_west", "matheran", "lonavala", "pune"):
        site = C.SITES_BY_KEY.get(key)
        if site is None:
            continue
        if not quiet:
            print(f"  {site.name}...")
        try:
            truth = fetch_truth(site, start, end)
        except Exception as exc:                  # noqa: BLE001
            print(f"  ! {site.name}: {exc}")
            continue
        vals = list(truth.values())
        if vals:
            out.append((site.name, site.zone, sum(vals) / len(vals), sum(vals)))
        time.sleep(0.4)
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _pc(v: float | None) -> str:
    return "--" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.0%}"


def _f(v: float | None, spec: str = ".1f") -> str:
    return "--" if v is None or (isinstance(v, float) and math.isnan(v)) else format(v, spec)


def render_report(r: BacktestResult,
                  terrain: list[tuple[str, str, float, float]] | None = None
                  ) -> str:
    out = f"# Backtest — {r.site_name}\n\n"
    out += (f"**Period** {r.start:%d %b %Y} – {r.end:%d %b %Y} "
            f"({r.n_days} days)  \n"
            f"**Event** ≥ {r.threshold:g} mm in 24 h  \n"
            f"**Observed rain days** {sum(1 for v in r.truth.values() if v >= r.threshold)}"
            f" / {r.n_days} — base rate **{r.base_rate:.0%}**\n\n")

    out += ("> **Method.** Forecasts are the runs *as actually issued* 1–5 days "
            "before each target day, from Open-Meteo's previous-runs archive — "
            "not today's output replayed over the past, which would be "
            "hindsight and would show absurd skill. Truth is ERA5 reanalysis.\n\n")
    out += ("> **Limitation, stated up front.** ERA5 is a model product, not a "
            "rain gauge, and over the Western Ghats — where the entire "
            "windward/leeward contrast spans a couple of grid cells — its "
            "precipitation is least reliable. Treat absolute scores as "
            "indicative; the trustworthy signal is in the *relative* "
            "comparisons below.\n\n---\n\n")

    # ---- the headline question ------------------------------------------
    out += "## Does it beat just saying 'rain'?\n\n"
    out += ("In peak monsoon this is the only question that matters. Guide "
            "§26.1 makes the point in the dry direction; here it bites in the "
            "wet one — with a base rate of "
            f"{r.base_rate:.0%}, a parrot saying 'rain' every day scores well.\n\n")

    out += "| Forecaster | Accuracy | POD | FAR | CSI | HSS |\n|---|---|---|---|---|---|\n"
    for name, c in (("**Agent (1-day lead)**", r.per_lead.get(1)),
                    ("Always say rain", r.baseline_always),
                    ("Never say rain", r.baseline_never),
                    ("Persistence (= yesterday)", r.baseline_persistence)):
        if c is None or c.n == 0:
            continue
        out += (f"| {name} | {_pc(c.accuracy)} | {_pc(c.pod)} | {_pc(c.far)} | "
                f"{_pc(c.csi)} | {_f(heidke(c), '.2f')} |\n")
    out += ("\n**HSS** (Heidke Skill Score) is the one to read: it strips out "
            "the credit a forecaster gets for free from the base rate. 0 means "
            "no better than chance, 1 is perfect, negative is worse than "
            "chance. Accuracy and CSI both flatter a wet-season forecaster; HSS "
            "does not.\n\n")
    out += ("> **Persistence is the baseline that matters**, not the parrot. "
            "'Always rain' is trivially beaten; 'same as yesterday' is not, "
            "because monsoon regimes persist for days at a time. If the agent "
            "is not clearly ahead of the persistence row, its mechanism "
            "reasoning is not yet earning its keep on the rain/no-rain "
            "question — whatever it may be adding on timing, amount and "
            "explanation.\n\n")

    # ---- season split ----------------------------------------------------
    if len(r.seasons) > 1:
        out += "## Split by season — because a blended score is meaningless\n\n"
        out += ("A period spanning a bone-dry month and a wet one produces an "
                "aggregate that describes neither. Read these rows, not the "
                "combined figure above.\n\n")
        out += ("| Season | Days | Base rate | Agent CSI | Agent HSS | "
                "Persistence HSS |\n|---|---|---|---|---|---|\n")
        for s in r.seasons:
            note = " *" if s.base_rate == 0 or s.base_rate == 1 else ""
            out += (f"| {s.label}{note} | {s.n_days} | {s.base_rate:.0%} | "
                    f"{_pc(s.agent.csi)} | {_f(heidke(s.agent), '.2f')} | "
                    f"{_f(heidke(s.persistence), '.2f')} |\n")
        if any(s.base_rate in (0.0, 1.0) for s in r.seasons):
            out += ("\n\\* A season with **no observed rain events at all** "
                    "cannot produce a meaningful skill score — every correct "
                    "call is a free correct-negative, and CSI/POD are "
                    "undefined. Those rows inflate the combined accuracy above "
                    "and should be discounted entirely.\n")
        out += "\n"

    # ---- lead time -------------------------------------------------------
    out += "## Skill by lead time\n\n"
    out += ("| Lead | Accuracy | POD | FAR | CSI | HSS | MAE all | MAE wet days | "
            "Season total | Category hit |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n")
    for n in LEADS:
        c = r.per_lead.get(n)
        if not c or c.n == 0:
            continue
        accum = r.accum_per_lead.get(n, float("nan"))
        ratio = (accum / r.observed_accum) if r.observed_accum else float("nan")
        out += (f"| {n} day | {_pc(c.accuracy)} | {_pc(c.pod)} | {_pc(c.far)} | "
                f"{_pc(c.csi)} | {_f(heidke(c), '.2f')} | "
                f"{_f(r.mae_per_lead.get(n))} mm | "
                f"{_f(r.mae_wet_per_lead.get(n))} mm | "
                f"{_f(accum, '.0f')} mm ({_pc(ratio)}) | "
                f"{_pc(r.category_hit.get(n))} |\n")
    out += (f"\nObserved season total: **{r.observed_accum:.0f} mm**.\n\n")
    out += ("> **Do not read the 'MAE all' column as skill.** It is dominated "
            "by dry days, where every lead time scores near zero, and it "
            "actively rewards a forecast that is biased dry: under-forecasting "
            "shrinks the error on the many light days while badly missing the "
            "few heavy ones. The 'Season total' column exposes that bias "
            "directly — a long lead that lands well under 100% is losing the "
            "big events, whatever its MAE says. The heavy-rain table below is "
            "where lead-time degradation actually shows.\n\n")

    # ---- per model -------------------------------------------------------
    labels = {m.key: m.label for m in C.MODELS}
    if r.per_lead_model:
        out += "## Model by model\n\n"
        out += "| Model | Lead | POD | FAR | CSI | HSS |\n|---|---|---|---|---|---|\n"
        for (n, m), c in sorted(r.per_lead_model.items(),
                                key=lambda kv: (kv[0][1], kv[0][0])):
            if c.n == 0 or n not in (1, 3):
                continue
            out += (f"| {labels.get(m, m)} | {n} day | {_pc(c.pod)} | "
                    f"{_pc(c.far)} | {_pc(c.csi)} | {_f(heidke(c), '.2f')} |\n")
        out += ("\nHandbook Ch.21 claims ECMWF leads on precipitation. This is "
                "the local check on that claim — and the reason the agent keeps "
                "a per-location verification log rather than trusting general "
                "reputation (Guide §13.3).\n\n")

    # ---- heavy rain ------------------------------------------------------
    out += f"## Heavy rain (≥ {C.HEAVY_DAY_MM:g} mm/24 h)\n\n"
    any_heavy = False
    out += "| Lead | Events | Hits | Misses | False alarms | POD | FAR |\n|---|---|---|---|---|---|---|\n"
    for n in LEADS:
        c = r.heavy_per_lead.get(n)
        if not c or (c.hits + c.misses) == 0:
            continue
        any_heavy = True
        out += (f"| {n} day | {c.hits + c.misses} | {c.hits} | {c.misses} | "
                f"{c.false_alarms} | {_pc(c.pod)} | {_pc(c.far)} |\n")
    if not any_heavy:
        out += "| — | 0 | — | — | — | — | — |\n\nNo heavy-rain days in this period.\n"
    out += ("\nThese are the days that actually matter for decisions. A high "
            "overall CSI built on drizzle detection means little if the heavy "
            "days are being missed.\n\n")

    # ---- the heavy days, one by one - where lead time really shows -------
    if r.heavy_days:
        out += "### Every heavy day, forecast by forecast\n\n"
        out += "| Date | Observed | " + " | ".join(f"{n} day" for n in LEADS) + " |\n"
        out += "|---|---|" + "---|" * len(LEADS) + "\n"
        for d, obs, fcs in r.heavy_days:
            cells = " | ".join(
                (f"{fcs[n]:.0f} mm" if n in fcs else "--") for n in LEADS)
            out += f"| {d:%d %b} | **{obs:.0f} mm** | {cells} |\n"
        out += ("\nThis small table carries more information than every "
                "aggregate score above it. The pattern to look for is whether "
                "the longer leads collapse toward zero on the big events — "
                "that is the real lead-time degradation, and it is exactly what "
                "the averaged error statistics conceal.\n\n")

    # ---- regime ----------------------------------------------------------
    if r.regime_csi:
        out += "## Skill by regime — is the mechanism reasoning right?\n\n"
        out += ("The guides make a testable claim: organised, terrain-driven "
                "rain is more predictable than isolated convection "
                "(Handbook Ch.14/16). Regimes here are labelled from the "
                "*analysis* — what the atmosphere actually did — so this asks "
                "'on days that turned out to be X, how good was the 1-day "
                "forecast?'\n\n")
        out += ("| Actual regime | Days | Rain days | POD | FAR | CSI | HSS |\n"
                "|---|---|---|---|---|---|---|\n")
        degenerate = []
        for regime, (c, n_days, n_events) in sorted(r.regime_csi.items(),
                                                    key=lambda kv: -kv[1][1]):
            if c.n == 0:
                continue
            # A regime with no observed events, or where every day rained,
            # cannot yield a meaningful score - flag rather than print noise.
            if n_events == 0 or n_events == n_days:
                degenerate.append((regime, n_days, n_events))
                out += (f"| {regime} | {n_days} | {n_events} | — | — | — | — |\n")
                continue
            out += (f"| {regime} | {n_days} | {n_events} | {_pc(c.pod)} | "
                    f"{_pc(c.far)} | {_pc(c.csi)} | {_f(heidke(c), '.2f')} |\n")
        out += "\n"
        for regime, n_days, n_events in degenerate:
            if n_events == 0:
                out += (f"> **{regime}** ({n_days} days) is scored as `—` "
                        "deliberately: **not one of those days produced "
                        "measurable rain**, so POD is undefined and any single "
                        "borderline call would read as 100% false alarms. "
                        "There is no skill to measure here, in either "
                        "direction.\n\n")
            else:
                out += (f"> **{regime}** ({n_days} days) is scored as `—`: "
                        "**every single day produced rain**, so forecasting "
                        "rain is free and a perfect CSI means nothing. The "
                        "informative question for this regime is amount, not "
                        "occurrence — see the heavy-rain table.\n\n")

    # ---- spread ----------------------------------------------------------
    if r.spread_bins:
        out += "## Does model disagreement actually predict unreliability?\n\n"
        out += ("The agent widens its language when ECMWF, GFS and ICON "
                "disagree (Guide Case Study F). That is only justified if wide "
                "spread really does mean a less reliable forecast.\n\n")
        out += "| Model spread (1-day lead) | Days | Forecast correct |\n|---|---|---|\n"
        for label, n, rate in r.spread_bins:
            out += f"| {label} | {n} | {_pc(rate)} |\n"
        out += "\n"

    # ---- terrain ---------------------------------------------------------
    if terrain:
        out += "## Terrain gradient — does the rain shadow verify?\n\n"
        out += "| Site | Zone | Mean daily | Period total |\n|---|---|---|---|\n"
        for name, zone, mean, total in terrain:
            out += f"| {name} | {zone} | {mean:.1f} mm | {total:.0f} mm |\n"
        ghat = [t for t in terrain if t[1] == "ghat"]
        lee = [t for t in terrain if t[1] == "leeward"]
        if ghat and lee and lee[0][3] > 0:
            ratio = max(g[3] for g in ghat) / lee[0][3]
            out += (f"\nObserved ghat-to-leeward ratio: **{ratio:.1f}×**. "
                    "Handbook Ch.14 puts the climatological Mahabaleshwar/Pune "
                    "contrast near 8×, so a ratio in that neighbourhood means "
                    "the mechanism the agent reasons from is real in the data "
                    "and not just in the textbook.\n")
        out += "\n"

    out += ("---\n\n_Scores computed against ERA5 reanalysis, not gauge data. "
            "Guide §29.1: build a transparent, verified record over 60–90 days "
            "before treating output as trustworthy — and publish the misses._\n")
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the forecasting agent.")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD")
    ap.add_argument("--site", default=C.HOME.key)
    ap.add_argument("--threshold", type=float, default=C.MEASURABLE_RAIN_MM)
    ap.add_argument("--no-terrain", action="store_true")
    args = ap.parse_args(argv)

    end = (date.fromisoformat(args.end) if args.end
           else date.today() - timedelta(days=2))     # ERA5 lags a little
    start = (date.fromisoformat(args.start) if args.start
             else end - timedelta(days=100))

    site = C.SITES_BY_KEY.get(args.site, C.HOME)
    result = run_backtest(site, start, end, threshold=args.threshold)

    terrain = None
    if not args.no_terrain:
        print("  terrain check...")
        terrain = terrain_check(start, end)

    report = render_report(result, terrain)
    path = C.FORECAST_DIR / f"backtest_{site.key}_{start:%Y%m%d}_{end:%Y%m%d}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\n{report}")
    print(f"written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
