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

# The deep backtest goes out to 7 days, which is where the useful range of a
# deterministic rainfall forecast ends (Handbook Ch.20). Anything beyond that
# is a climatological statement wearing a forecast's clothes.
DEEP_LEADS = (1, 2, 3, 5, 7)

# The IMD 24 h rainfall bands, used as a threshold sweep. Skill measured at
# 2.5 mm answers "will it rain", which in monsoon Konkan is nearly free. The
# thresholds that carry a decision are the ones further down this list.
IMD_BANDS: tuple[tuple[float, str], ...] = (
    (2.5, "Measurable"),
    (15.6, "Rather heavy"),
    (64.5, "Heavy"),
    (124.5, "Very heavy"),
)

# Members of the time-lagged, multi-model ensemble used for the probabilistic
# scores. For a target day D at lead L the members are every model's run issued
# L, L+1 and L+2 days before D - all of which are genuinely in hand at issue
# time, so this stays a forecast and not hindsight.
LAG_MEMBERS = (0, 1, 2)

# Moving-block bootstrap settings. Rainfall is strongly autocorrelated day to
# day - monsoon spells last the better part of a week - so resampling
# individual days independently would treat ~300 correlated days as ~300
# independent ones and produce confidence intervals far too narrow.
BLOCK_DAYS = 5
N_BOOTSTRAP = 2000


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
        # models=era5 is NOT optional. Left to its default the archive is
        # "seamless": it serves ERA5 where ERA5 exists and quietly splices in
        # ECMWF's own IFS analysis for the last few days, which ERA5 has not
        # caught up to. Verifying an ECMWF forecast against an ECMWF analysis
        # is marking your own homework, and the two genuinely disagree - on
        # 11 Aug 2026 at Kalyan, ERA5 said 14.0 mm and IFS said 32.8 mm.
        # Pinning this to era5 makes recent days return null instead, which is
        # the honest answer: not yet verifiable.
        "models": "era5",
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
# Deep backtest - multi-season, multi-threshold, with uncertainty
# --------------------------------------------------------------------------
#
# The shallow backtest above answers "how did the agent do over the last ~100
# days?". Four things stop that being a real evaluation, and this section fixes
# each of them:
#
#   1. ONE SEASON IS ONE SAMPLE. A single monsoon can be skilful by luck. Skill
#      that does not replicate across separate years is not skill.
#   2. NO ERROR BARS. With ~100 autocorrelated days, an HSS gap of 0.05 between
#      the agent and persistence is indistinguishable from noise. Every headline
#      number here carries a bootstrap interval, and the interval on the
#      DIFFERENCE is what decides whether the agent is genuinely ahead.
#   3. ONE THRESHOLD. "Will it rain" is nearly free in monsoon Konkan. The
#      thresholds that carry a decision are 64.5 and 124.5 mm.
#   4. NO PROBABILITIES. A deterministic yes/no throws away the confidence
#      information the agent actually has. The time-lagged ensemble below is
#      scored for calibration, which is the property that decides whether
#      "likely" in a bulletin can be taken at face value.

def _contiguous(days: Sequence[date]) -> list[list[date]]:
    """Split a day list into runs of consecutive dates.

    Persistence ("same as yesterday") and spell-transition logic are only
    meaningful inside an unbroken run. Carrying them across the nine-month gap
    between two monsoons would invent a comparison that never existed.
    """
    segs: list[list[date]] = []
    for d in sorted(days):
        if segs and d - segs[-1][-1] == timedelta(days=1):
            segs[-1].append(d)
        else:
            segs.append([d])
    return segs


@dataclass
class DayRecord:
    """One target day, with everything needed to score it after the fact."""
    day: date
    observed_mm: float
    median: dict[int, float] = field(default_factory=dict)     # lead -> mm
    members: dict[int, list[float]] = field(default_factory=dict)  # lead -> mm
    regime: str = "Unknown"
    # Yesterday's observed rainfall, carried on the record itself.
    #
    # This exists so the persistence baseline survives resampling. If the
    # bootstrap computed persistence as "the previous entry in the resampled
    # series", then at every block boundary it would pair two days that were
    # never consecutive - roughly one pair in five at a five-day block length.
    # Those spurious pairings are random with respect to each other, which
    # drags persistence's score down and hands the agent a lead it did not
    # earn. Pinning the true predecessor to each day keeps the baseline honest
    # no matter how the blocks are shuffled.
    prev_mm: float | None = None


@dataclass
class DeepResult:
    site_name: str
    periods: list[tuple[date, date]]
    records: list[DayRecord]
    segments: list[list[DayRecord]]
    per_year: dict[int, list[DayRecord]]


def run_deep(site, periods: Sequence[tuple[date, date]], *,
             models: Sequence[str] | None = None,
             quiet: bool = False) -> DeepResult:
    """Assemble the day-by-day record. All scoring happens afterwards, so the
    same fetched data serves every threshold, lead and subset below."""
    models = list(models or [m.key for m in C.MODELS])
    need = sorted({L + k for L in DEEP_LEADS for k in LAG_MEMBERS})

    records: list[DayRecord] = []
    for start, end in periods:
        if not quiet:
            print(f"  {site.name} {start:%b %Y}-{end:%b %Y}: truth...")
        truth = fetch_truth(site, start, end)

        fc: dict[str, dict[int, dict[date, float]]] = {}
        for m in models:
            if not quiet:
                print(f"    {m}...")
            try:
                fc[m] = fetch_forecasts(site, start, end, m, leads=need)
            except Exception as exc:              # noqa: BLE001
                print(f"    ! {m}: {exc}")
            time.sleep(0.4)

        try:
            diags = fetch_analysis_diagnostics(site, start, end)
        except Exception as exc:                  # noqa: BLE001
            print(f"    ! diagnostics: {exc}")
            diags = {}
        time.sleep(0.4)

        for d in sorted(truth):
            if not (start <= d <= end):
                continue
            rec = DayRecord(day=d, observed_mm=truth[d],
                            regime=label_regime(diags[d], d) if d in diags
                            else "Unknown")
            for L in DEEP_LEADS:
                at_lead = [fc[m][L][d] for m in fc
                           if L in fc[m] and d in fc[m][L]]
                if at_lead:
                    rec.median[L] = statistics.median(at_lead)
                # Time-lagged, multi-model members: every model's run issued
                # L, L+1 and L+2 days out. All are in hand at issue time.
                mem = [fc[m][L + k][d] for m in fc for k in LAG_MEMBERS
                       if (L + k) in fc[m] and d in fc[m][L + k]]
                if mem:
                    rec.members[L] = mem
            records.append(rec)

    records.sort(key=lambda r: r.day)
    per_year: dict[int, list[DayRecord]] = defaultdict(list)
    for r in records:
        per_year[r.day.year].append(r)

    segs = [[r for r in records if r.day in set(s)]
            for s in _contiguous([r.day for r in records])]
    for seg in segs:
        for i in range(1, len(seg)):
            seg[i].prev_mm = seg[i - 1].observed_mm
    return DeepResult(site_name=site.name, periods=list(periods),
                      records=records, segments=segs, per_year=dict(per_year))


# ---- scoring helpers over the record list --------------------------------

def score(records: Sequence[DayRecord], lead: int, thr: float) -> Contingency:
    c = Contingency()
    for r in records:
        if lead in r.median:
            c.add(r.median[lead] >= thr, r.observed_mm >= thr)
    return c


def score_persistence(records: Sequence[DayRecord], thr: float) -> Contingency:
    """'Tomorrow is whatever today was', scored off each day's true predecessor.

    Takes a flat record list rather than segments so that the point estimate
    and every bootstrap draw run through identical code - the pairings travel
    with the days themselves.
    """
    c = Contingency()
    for r in records:
        if r.prev_mm is not None:
            c.add(r.prev_mm >= thr, r.observed_mm >= thr)
    return c


def transitions(records: Sequence[DayRecord], thr: float
                ) -> tuple[list[DayRecord], list[DayRecord]]:
    """Days when the wet/dry state flipped - onsets and cessations.

    These are the days persistence gets wrong *by construction*, so they are
    the cleanest test of whether the mechanism reasoning adds anything beyond
    "tomorrow looks like today".
    """
    onset: list[DayRecord] = []
    cess: list[DayRecord] = []
    for r in records:
        if r.prev_mm is None:
            continue
        prev_wet, wet = r.prev_mm >= thr, r.observed_mm >= thr
        if wet and not prev_wet:
            onset.append(r)
        elif prev_wet and not wet:
            cess.append(r)
    return onset, cess


def brier(records: Sequence[DayRecord], lead: int, thr: float
          ) -> tuple[float, float, int] | None:
    """Brier score, and the climatological reference built from this sample."""
    ps, os_ = [], []
    for r in records:
        mem = r.members.get(lead)
        if not mem:
            continue
        ps.append(sum(1 for v in mem if v >= thr) / len(mem))
        os_.append(1.0 if r.observed_mm >= thr else 0.0)
    if not ps:
        return None
    bs = sum((p - o) ** 2 for p, o in zip(ps, os_)) / len(ps)
    clim = sum(os_) / len(os_)
    bs_clim = sum((clim - o) ** 2 for o in os_) / len(os_)
    return bs, bs_clim, len(ps)


def reliability(records: Sequence[DayRecord], lead: int, thr: float,
                bins: int = 5) -> list[tuple[str, int, float, float]]:
    """Forecast probability against observed frequency.

    A well-calibrated forecast has observed frequency tracking forecast
    probability down the table. Systematic overshoot means "likely" in a
    bulletin should be read down, not taken at face value.
    """
    edges = [i / bins for i in range(bins + 1)]
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for r in records:
        mem = r.members.get(lead)
        if not mem:
            continue
        p = sum(1 for v in mem if v >= thr) / len(mem)
        o = 1.0 if r.observed_mm >= thr else 0.0
        idx = min(int(p * bins), bins - 1)
        buckets[idx].append((p, o))
    out = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        out.append((f"{edges[i]:.0%}-{edges[i+1]:.0%}", len(b),
                    sum(p for p, _ in b) / len(b),
                    sum(o for _, o in b) / len(b)))
    return out


# ---- uncertainty ----------------------------------------------------------

def _block_resample(segments: Sequence[Sequence[DayRecord]],
                    rng) -> list[DayRecord]:
    """One moving-block bootstrap draw, preserving day-to-day correlation."""
    flat = [r for seg in segments for r in seg]
    n = len(flat)
    out: list[DayRecord] = []
    while len(out) < n:
        seg = segments[rng.randrange(len(segments))]
        if len(seg) <= BLOCK_DAYS:
            out.extend(seg)
            continue
        i = rng.randrange(len(seg) - BLOCK_DAYS)
        out.extend(seg[i:i + BLOCK_DAYS])
    return out[:n]


def bootstrap_hss(segments: Sequence[Sequence[DayRecord]], lead: int,
                  thr: float, *, n: int = N_BOOTSTRAP
                  ) -> tuple[tuple[float, float], tuple[float, float],
                             tuple[float, float], float] | None:
    """
    90% intervals for agent HSS, persistence HSS, and their difference.

    The interval on the DIFFERENCE is the one that settles the argument. If it
    straddles zero, the agent has not been shown to beat persistence on this
    sample, whatever the point estimates look like.
    """
    import random
    rng = random.Random(20260817)

    agent_s, persist_s, diff_s = [], [], []
    for _ in range(n):
        draw = _block_resample(segments, rng)
        ca, cp = Contingency(), Contingency()
        for r in draw:
            if lead in r.median:
                ca.add(r.median[lead] >= thr, r.observed_mm >= thr)
            if r.prev_mm is not None:
                cp.add(r.prev_mm >= thr, r.observed_mm >= thr)
        ha, hp = heidke(ca), heidke(cp)
        if ha is None or hp is None:
            continue
        agent_s.append(ha)
        persist_s.append(hp)
        diff_s.append(ha - hp)
    if not diff_s:
        return None

    def ci(v: list[float]) -> tuple[float, float]:
        v = sorted(v)
        return v[int(0.05 * len(v))], v[int(0.95 * len(v)) - 1]

    beat = sum(1 for d in diff_s if d > 0) / len(diff_s)
    return ci(agent_s), ci(persist_s), ci(diff_s), beat


# ---- report ---------------------------------------------------------------

def render_deep(r: DeepResult,
                zones: list[tuple[str, str, Contingency, float]] | None = None
                ) -> str:
    recs, segs = r.records, r.segments
    span = ", ".join(f"{s:%b %Y}–{e:%b %Y}" for s, e in r.periods)
    wet = sum(1 for x in recs if x.observed_mm >= 2.5)
    base = wet / len(recs) if recs else 0.0

    out = f"# Deep backtest — {r.site_name}\n\n"
    out += (f"**Seasons** {span}  \n"
            f"**Days scored** {len(recs)} across {len(segs)} unbroken runs  \n"
            f"**Observed rain days** {wet} — base rate **{base:.0%}**  \n"
            f"**Leads** {', '.join(str(L) + 'd' for L in DEEP_LEADS)}  \n"
            f"**Truth** ERA5 reanalysis\n\n")

    out += ("> **What makes this a backtest and not hindsight.** Every forecast "
            "below is the run *as it was actually issued* 1–9 days before its "
            "target day, pulled from Open-Meteo's previous-runs archive. "
            "Replaying today's model output over past dates would show "
            "spectacular and entirely fake skill.\n\n")
    out += ("> **What is on trial here.** The forecast being scored is the "
            "agent's quantitative core — the median of ECMWF, GFS and ICON at "
            "each lead. It is *not* the diagnostic layer: the moisture-depth "
            "checks, the terrain-normal wind component, the trough analysis and "
            "the wording that come out of the bulletins are not reduced to a "
            "number and cannot be scored this way. So this measures the "
            "foundation the agent reasons from, not the reasoning. A good score "
            "here does not vouch for the narrative, and a poor one does not "
            "condemn it.\n\n")
    out += ("> **The limitation, stated before the numbers.** ERA5 is a model "
            "product, not a rain gauge, and over the Western Ghats — where the "
            "whole windward/leeward contrast spans a couple of grid cells — its "
            "precipitation is at its least reliable. It also smooths extremes, "
            "so the heavy-rain rows below are scored against a truth that "
            "under-reads the biggest days. Read absolute scores as indicative; "
            "the *relative* comparisons carry the real information.\n\n---\n\n")

    # ---- headline: does it beat persistence, and is the gap real? ---------
    out += "## 1. Does it beat persistence — and is the gap real?\n\n"
    out += ("'Always say rain' is a strawman at a "
            f"{base:.0%} base rate. The baseline that bites is **persistence** "
            "— tomorrow is whatever today was — because monsoon regimes hold "
            "for days. Point estimates alone cannot settle this: with "
            f"{len(recs)} strongly autocorrelated days, small gaps are noise. "
            "So each figure carries a 90% moving-block bootstrap interval "
            f"(block = {BLOCK_DAYS} days, {N_BOOTSTRAP:,} resamples).\n\n")

    out += ("| Threshold | Events | Agent HSS (90% CI) | Persistence HSS (90% CI) "
            "| Difference (90% CI) | P(agent ahead) |\n"
            "|---|---|---|---|---|---|\n")
    for thr, name in IMD_BANDS:
        ev = sum(1 for x in recs if x.observed_mm >= thr)
        if ev < 5:
            out += (f"| {name} ≥{thr} mm | {ev} | — | — | — | — |\n")
            continue
        ca = score(recs, 1, thr)
        cp = score_persistence(recs, thr)
        ha, hp = heidke(ca), heidke(cp)
        bs = bootstrap_hss(segs, 1, thr)
        if bs is None or ha is None or hp is None:
            out += f"| {name} ≥{thr} mm | {ev} | — | — | — | — |\n"
            continue
        (al, ah), (pl, ph), (dl, dh), beat = bs
        out += (f"| {name} ≥{thr} mm | {ev} | {ha:.2f} "
                f"({al:.2f}–{ah:.2f}) | {hp:.2f} "
                f"({pl:.2f}–{ph:.2f}) | **{ha - hp:+.2f}** "
                f"({dl:+.2f}–{dh:+.2f}) | {beat:.0%} |\n")
    out += ("\n**How to read the difference column.** If its interval includes "
            "zero, this sample has *not* shown the agent to beat persistence at "
            "that threshold — the point estimate may be positive and still be "
            "noise. Only an interval clear of zero is evidence.\n\n")

    # ---- replication across years ----------------------------------------
    if len(r.per_year) > 1:
        out += "## 2. Does the skill replicate across seasons?\n\n"
        out += ("One good monsoon can be luck. Skill that appears in one season "
                "and vanishes in the next is not skill — it is a sample. This "
                "is the single most important table here.\n\n")
        out += ("| Season | Days | Base rate | Agent HSS | Persistence HSS | "
                "Gap |\n|---|---|---|---|---|---|\n")
        for yr in sorted(r.per_year):
            yrecs = r.per_year[yr]
            ca, cp = score(yrecs, 1, 2.5), score_persistence(yrecs, 2.5)
            ha, hp = heidke(ca), heidke(cp)
            br = sum(1 for x in yrecs if x.observed_mm >= 2.5) / len(yrecs)
            gap = (f"{ha - hp:+.2f}" if ha is not None and hp is not None else "—")
            out += (f"| {yr} | {len(yrecs)} | {br:.0%} | {_f(ha, '.2f')} | "
                    f"{_f(hp, '.2f')} | {gap} |\n")
        out += "\n"

    # ---- lead time --------------------------------------------------------
    out += "## 3. How fast does skill decay with lead time?\n\n"
    out += ("| Lead | POD | FAR | CSI | HSS | Bias | Heavy-day POD (≥64.5) |\n"
            "|---|---|---|---|---|---|---|\n")
    for L in DEEP_LEADS:
        c = score(recs, L, 2.5)
        h = score(recs, L, 64.5)
        if c.n == 0:
            continue
        hpod = _pc(h.pod) if (h.hits + h.misses) else "—"
        out += (f"| {L} day | {_pc(c.pod)} | {_pc(c.far)} | {_pc(c.csi)} | "
                f"{_f(heidke(c), '.2f')} | {_f(c.bias, '.2f')} | {hpod} |\n")
    n_heavy = sum(1 for x in recs if x.observed_mm >= 64.5)
    out += ("\n**Bias** is forecast events ÷ observed events. Above 1 the agent "
            "cries rain too often; below 1 it misses days. The heavy-day column "
            "is where lead time really tells: occurrence skill decays slowly "
            "because monsoon rain is nearly always *somewhere*, but the ability "
            "to place a big day decays fast.\n\n")
    if 0 < n_heavy < 30:
        out += (f"> **That heavy-day column rests on {n_heavy} events.** One "
                "day moving between hit and miss shifts it by several "
                f"percentage points, which is why it does not fall cleanly with "
                "lead time. Read it as 'poor throughout', not as a trend.\n\n")

    # ---- threshold sweep --------------------------------------------------
    out += "## 4. Skill by rainfall band — where it stops working\n\n"
    out += ("| Band | Events | POD | FAR | CSI | HSS |\n|---|---|---|---|---|---|\n")
    for thr, name in IMD_BANDS:
        c = score(recs, 1, thr)
        ev = c.hits + c.misses
        if ev == 0:
            out += f"| {name} ≥{thr} mm | 0 | — | — | — | — |\n"
            continue
        out += (f"| {name} ≥{thr} mm | {ev} | {_pc(c.pod)} | {_pc(c.far)} | "
                f"{_pc(c.csi)} | {_f(heidke(c), '.2f')} |\n")
    out += ("\nThis is the table to quote when someone asks how much to trust "
            "the agent. Detecting *rain* in monsoon Konkan is close to free. "
            "Detecting the day that floods an underpass is the hard problem, "
            "and the row that matters is the lowest one with a usable sample.\n\n")

    # ---- transitions ------------------------------------------------------
    out += "## 5. The days persistence cannot get right\n\n"
    onset, cess = transitions(recs, 2.5)
    trans = onset + cess
    if trans:
        c = Contingency()
        for x in trans:
            if 1 in x.median:
                c.add(x.median[1] >= 2.5, x.observed_mm >= 2.5)
        pairable = sum(1 for x in recs if x.prev_mm is not None)
        share = len(trans) / max(1, pairable)
        out += (f"Of {pairable} scoreable days, "
                f"**{len(trans)} ({share:.0%})** were flips — "
                f"{len(onset)} onsets (dry→wet) and {len(cess)} cessations "
                "(wet→dry). Persistence is wrong on **every one of them by "
                "construction**. So this subset isolates exactly what the "
                "mechanism reasoning is for.\n\n")
        out += (f"On those flip days the agent called "
                f"**{c.hits + c.correct_negatives} of {c.n} correctly "
                f"({_pc(c.accuracy)})** — against persistence's 0%.\n\n")
        out += ("| | Called rain | Called dry |\n|---|---|---|\n"
                f"| **Rained** | {c.hits} | {c.misses} |\n"
                f"| **Stayed dry** | {c.false_alarms} | {c.correct_negatives} |\n\n")
        out += ("That accuracy figure is the honest answer to 'is this better "
                "than a rule of thumb?'. It is also why the aggregate HSS gap "
                "in §1 is modest: flips are a minority of days, so even perfect "
                "flip-day skill moves the overall score only so far.\n\n")

    # ---- probabilistic ----------------------------------------------------
    out += "## 6. Are the confidence words honest?\n\n"
    out += ("The bulletins say *likely*, *possible*, *expected*. Those words "
            "are only worth anything if they are calibrated. Members here are a "
            "time-lagged multi-model ensemble: every model's run issued at "
            "L, L+1 and L+2 days out — all genuinely in hand at issue time.\n\n")
    out += "| Lead | Brier score | Climatology | Brier Skill Score |\n|---|---|---|---|\n"
    for L in DEEP_LEADS:
        b = brier(recs, L, 2.5)
        if not b:
            continue
        bs, bsc, n = b
        bss = 1 - bs / bsc if bsc else float("nan")
        out += f"| {L} day | {bs:.3f} | {bsc:.3f} | **{bss:+.2f}** |\n"
    out += ("\nBrier is an error score — lower is better, 0 is perfect. The "
            "Brier Skill Score compares it against simply quoting the "
            "climatological rain frequency every day; **positive means the "
            "probabilities beat climatology, negative means they are worse than "
            "saying nothing**.\n\n")

    rel = reliability(recs, 1, 2.5)
    under: list[tuple[float, float, int]] = []   # bands the ensemble under-calls
    if rel:
        out += "**Reliability at 1-day lead** — do the probabilities mean what they say?\n\n"
        out += ("| Forecast probability | Days | Mean forecast | "
                "Observed frequency | Verdict |\n|---|---|---|---|---|\n")
        for label, n, mp, of in rel:
            gap = of - mp
            flag = ("too confident" if gap < -0.10
                    else "too cautious" if gap > 0.10 else "calibrated")
            out += f"| {label} | {n} | {mp:.0%} | {of:.0%} | {flag} |\n"
        # The direction of miscalibration is read off the data rather than
        # asserted - it differs by site and by season, and a fixed sentence
        # here would eventually describe the opposite of what the table shows.
        low = [(mp, of, n) for _, n, mp, of in rel if mp < 0.5]
        under = [x for x in low if x[1] - x[0] > 0.10]
        out += "\n"
        if under:
            worst = max(under, key=lambda x: x[1] - x[0])
            out += (f"**The low-probability bands are the weak spot.** In the "
                    f"band where the ensemble averaged {worst[0]:.0%}, it "
                    f"actually rained **{worst[1]:.0%}** of the time "
                    f"({worst[2]} days). A near-zero ensemble probability is "
                    "therefore *not* a safe 'no rain' in monsoon Konkan — the "
                    "members are all resolving the same grid-scale flow and "
                    "share the same blind spot for locally forced convection. "
                    "Treat a low number as 'no organised rain signal', not as "
                    "'dry'.\n\n")
        top = [x for x in ((mp, of, n) for _, n, mp, of in rel) if x[0] >= 0.8]
        if top and abs(top[-1][1] - top[-1][0]) <= 0.10:
            out += (f"At the other end the ensemble is honest: when it says "
                    f"{top[-1][0]:.0%} it rains {top[-1][1]:.0%} of the time "
                    f"across {top[-1][2]} days. **High confidence can be taken "
                    "at face value; low confidence cannot.**\n\n")

    # ---- regime -----------------------------------------------------------
    reg: dict[str, Contingency] = defaultdict(Contingency)
    reg_n: dict[str, int] = defaultdict(int)
    reg_ev: dict[str, int] = defaultdict(int)
    for x in recs:
        reg_n[x.regime] += 1
        if x.observed_mm >= 2.5:
            reg_ev[x.regime] += 1
        if 1 in x.median:
            reg[x.regime].add(x.median[1] >= 2.5, x.observed_mm >= 2.5)
    if reg:
        out += "## 7. Skill by regime — is the mechanism story true?\n\n"
        out += ("The guides make a testable claim: organised, terrain-driven "
                "rain is more predictable than isolated convection. Regimes are "
                "labelled from the **analysis** — what the atmosphere actually "
                "did — so this asks 'on days that turned out to be X, how good "
                "was the forecast?'\n\n")
        out += ("| Actual regime | Days | Rain days | POD | FAR | CSI | HSS |\n"
                "|---|---|---|---|---|---|---|\n")
        for name, c in sorted(reg.items(), key=lambda kv: -reg_n[kv[0]]):
            nd, ne = reg_n[name], reg_ev[name]
            if ne == 0 or ne == nd:
                out += (f"| {name} | {nd} | {ne} | — | — | — | — |\n")
                continue
            out += (f"| {name} | {nd} | {ne} | {_pc(c.pod)} | {_pc(c.far)} | "
                    f"{_pc(c.csi)} | {_f(heidke(c), '.2f')} |\n")
        out += ("\nRows scored `—` had either no rain days or nothing but rain "
                "days: with no contrast there is no skill to measure, and "
                "printing a perfect CSI there would be meaningless.\n\n")

    # ---- zones ------------------------------------------------------------
    if zones:
        out += "## 8. Does skill depend on where you stand?\n\n"
        out += ("The agent forecasts for coast, transition, crest and rain "
                "shadow from the same models. If terrain-forced rain really is "
                "more predictable, ghat sites should score better than leeward "
                "ones.\n\n")
        out += ("| Site | Zone | Base rate | POD | FAR | CSI | HSS |\n"
                "|---|---|---|---|---|---|---|\n")
        for name, zone, c, br in zones:
            out += (f"| {name} | {zone} | {br:.0%} | {_pc(c.pod)} | "
                    f"{_pc(c.far)} | {_pc(c.csi)} | {_f(heidke(c), '.2f')} |\n")
        # HSS is deflated wherever the base rate approaches 0 or 1, because
        # there is almost nothing left for a forecaster to add. Reading the
        # column without this note inverts the conclusion at the wettest sites.
        extreme = [z for z in zones if z[3] >= 0.90]
        out += "\n"
        if extreme:
            names = ", ".join(z[0] for z in extreme)
            out += (f"> **Do not read the HSS column down the page.** "
                    f"{names} sits at a base rate of "
                    f"{max(z[3] for z in extreme):.0%} — it rains there on "
                    "nearly every monsoon day, so there is barely any "
                    "uncertainty left to resolve and HSS collapses toward zero "
                    "however good the forecast is. Its CSI tells the truer "
                    "story. Compare HSS only between sites with similar base "
                    "rates.\n\n")

    # ---- bottom line ------------------------------------------------------
    # Computed rather than written, so it cannot drift away from the tables
    # above the next time this is run on a different sample.
    out += "## The bottom line\n\n"
    c1 = score(recs, 1, 2.5)
    cp1 = score_persistence(recs, 2.5)
    h1, hp1 = heidke(c1), heidke(cp1)
    bs1 = bootstrap_hss(segs, 1, 2.5)
    heavy = score(recs, 1, 64.5)
    n_heavy_ev = heavy.hits + heavy.misses

    out += "**Trust it for:**\n\n"
    out += (f"- *Will it rain in the next 1–3 days.* CSI {_pc(c1.csi)} at "
            f"one day, still {_pc(score(recs, 3, 2.5).csi)} at three, with a "
            f"false-alarm rate of only {_pc(c1.far)}.\n")
    if rel and rel[-1][2] >= 0.8 and abs(rel[-1][3] - rel[-1][2]) <= 0.10:
        out += (f"- *Its confident calls.* When the ensemble is near-unanimous "
                f"it verifies {rel[-1][3]:.0%} of the time. High confidence "
                "means what it says.\n")
    if trans:
        ct = Contingency()
        for x in trans:
            if 1 in x.median:
                ct.add(x.median[1] >= 2.5, x.observed_mm >= 2.5)
        out += (f"- *Turning points.* {_pc(ct.accuracy)} correct on the "
                f"{len(trans)} days the weather flipped — the days a rule of "
                "thumb necessarily gets wrong.\n")

    out += "\n**Do not trust it for:**\n\n"
    if n_heavy_ev:
        out += (f"- *Heavy rain.* At ≥64.5 mm it caught {heavy.hits} of "
                f"{n_heavy_ev} events and was wrong {_pc(heavy.far)} of the "
                "times it called one. For flooding decisions, use IMD.\n")
    if under:
        w = max(under, key=lambda x: x[1] - x[0])
        out += (f"- *Quiet days.* A {w[0]:.0%} probability still rained "
                f"{w[1]:.0%} of the time. Low numbers mean 'no organised "
                "signal', never 'dry'.\n")
    b7 = brier(recs, 7, 2.5)
    if b7 and b7[1]:
        out += (f"- *Day 7.* Brier skill falls to {1 - b7[0] / b7[1]:+.2f} — "
                "barely distinguishable from quoting the seasonal average. "
                "Treat the far end of the week as context, not forecast.\n")

    out += "\n**Still unproven:**\n\n"
    if bs1 and h1 is not None and hp1 is not None:
        (_, _), (_, _), (dl, dh), beat = bs1
        if dl <= 0 <= dh:
            out += (f"- *That it beats persistence.* The agent leads "
                    f"{h1:.2f} to {hp1:.2f} on HSS, but the 90% interval on "
                    f"that gap runs {dl:+.2f} to {dh:+.2f} — it contains zero, "
                    f"so on {len(recs)} days the lead is not statistically "
                    f"established ({beat:.0%} of resamples favour the agent). "
                    "More seasons are needed, not a better story.\n")
        else:
            out += (f"- *(Now proven at ≥2.5 mm: gap {dl:+.2f} to {dh:+.2f}, "
                    "clear of zero.)*\n")
    yrs = {y: heidke(score(v, 1, 2.5)) for y, v in r.per_year.items()}
    yrp = {y: heidke(score_persistence(v, 2.5)) for y, v in r.per_year.items()}
    losing = [y for y in yrs if yrs[y] is not None and yrp[y] is not None
              and yrs[y] < yrp[y]]
    if losing:
        out += (f"- *That the skill replicates.* In "
                f"{', '.join(str(y) for y in sorted(losing))} the agent scored "
                "**below** persistence. A method that wins in two seasons out "
                f"of {len(yrs)} has not yet shown a durable edge.\n")

    out += ("\nNone of that makes the agent useless — it makes it a good "
            "1–3 day occurrence forecast with an honest confidence scale and a "
            "known blind spot on extremes. That is a more useful thing to own "
            "than an unverified one that claims more.\n\n")

    out += ("---\n\n_Truth is ERA5 reanalysis, not gauge data. Guide §29.1: "
            "build a transparent, verified record before treating output as "
            "trustworthy — and publish the misses._\n")
    return out


def save_deep(r: DeepResult, path) -> None:
    """Cache the assembled record so the report can be reworded for free.

    Fetching three seasons across five sites is ~90 API calls and several
    minutes; the scoring afterwards is instant. Separating the two means
    iterating on the analysis never costs another request.
    """
    path.write_text(json.dumps({
        "site_name": r.site_name,
        "periods": [[s.isoformat(), e.isoformat()] for s, e in r.periods],
        "records": [{
            "day": x.day.isoformat(), "observed_mm": x.observed_mm,
            "median": {str(k): v for k, v in x.median.items()},
            "members": {str(k): v for k, v in x.members.items()},
            "regime": x.regime, "prev_mm": x.prev_mm,
        } for x in r.records],
    }), encoding="utf-8")


def load_deep(path) -> DeepResult:
    raw = json.loads(path.read_text(encoding="utf-8"))
    recs = [DayRecord(
        day=date.fromisoformat(x["day"]), observed_mm=x["observed_mm"],
        median={int(k): v for k, v in x["median"].items()},
        members={int(k): v for k, v in x["members"].items()},
        regime=x["regime"], prev_mm=x["prev_mm"]) for x in raw["records"]]
    recs.sort(key=lambda x: x.day)
    per_year: dict[int, list[DayRecord]] = defaultdict(list)
    for x in recs:
        per_year[x.day.year].append(x)
    segs = [[x for x in recs if x.day in set(s)]
            for s in _contiguous([x.day for x in recs])]
    return DeepResult(
        site_name=raw["site_name"],
        periods=[(date.fromisoformat(a), date.fromisoformat(b))
                 for a, b in raw["periods"]],
        records=recs, segments=segs, per_year=dict(per_year))


def zone_skill(periods: Sequence[tuple[date, date]], *,
               keys: Sequence[str] = ("santacruz", "kalyan_west", "matheran",
                                      "lonavala", "pune"),
               quiet: bool = False
               ) -> list[tuple[str, str, Contingency, float]]:
    """1-day occurrence skill at one site per terrain zone."""
    out = []
    for key in keys:
        site = C.SITES_BY_KEY.get(key)
        if site is None:
            continue
        try:
            dr = run_deep(site, periods, quiet=quiet)
        except Exception as exc:                  # noqa: BLE001
            print(f"  ! {key}: {exc}")
            continue
        if not dr.records:
            continue
        c = score(dr.records, 1, 2.5)
        br = sum(1 for x in dr.records if x.observed_mm >= 2.5) / len(dr.records)
        out.append((site.name, site.zone, c, br))
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _monsoon_window(year: int, *, today: date | None = None
                    ) -> tuple[date, date] | None:
    """1 Jun - 30 Sep, clipped to what the archives can actually verify.

    ERA5 lags real time by a few days, so the current season is truncated
    rather than padded with days that have no truth to score against.
    """
    today = today or date.today()
    start, end = date(year, 6, 1), date(year, 9, 30)
    limit = today - timedelta(days=6)
    end = min(end, limit)
    return (start, end) if end > start else None


def _main_deep(args) -> int:
    site = C.SITES_BY_KEY.get(args.site, C.HOME)
    years = [int(y) for y in str(args.years).split(",") if y.strip()]
    periods = [w for y in years if (w := _monsoon_window(y))]
    if not periods:
        print("no scoreable monsoon windows in the requested years")
        return 1

    tag = "-".join(str(y) for y in years)
    cache = C.FORECAST_DIR / f".deep_{site.key}_{tag}.json"
    zcache = C.FORECAST_DIR / f".deep_zones_{tag}.json"

    print(f"Deep backtest — {site.name}")
    for s, e in periods:
        print(f"  season: {s} to {e}")

    if args.cached and cache.exists():
        print(f"  using cached records ({cache.name}) — no API calls")
        result = load_deep(cache)
    else:
        result = run_deep(site, periods)
        if result.records:
            save_deep(result, cache)
    if not result.records:
        print("no data returned")
        return 1

    zones = None
    if not args.no_zones:
        if args.cached and zcache.exists():
            zones = [(n, z, Contingency(**c), b)
                     for n, z, c, b in json.loads(
                         zcache.read_text(encoding="utf-8"))]
        else:
            print("  zone comparison across terrain...")
            zones = zone_skill(periods, quiet=True)
            zcache.write_text(json.dumps(
                [[n, z, c.__dict__, b] for n, z, c, b in zones]),
                encoding="utf-8")

    report = render_deep(result, zones)
    path = C.FORECAST_DIR / f"backtest_deep_{site.key}_{tag}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\n{report}")
    print(f"written: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the forecasting agent.")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD")
    ap.add_argument("--site", default=C.HOME.key)
    ap.add_argument("--threshold", type=float, default=C.MEASURABLE_RAIN_MM)
    ap.add_argument("--no-terrain", action="store_true")
    ap.add_argument("--deep", action="store_true",
                    help="multi-season run with bootstrap intervals, a "
                         "threshold sweep and probabilistic scores")
    ap.add_argument("--years", default="2024,2025,2026",
                    help="monsoon seasons to score in --deep mode")
    ap.add_argument("--no-zones", action="store_true")
    ap.add_argument("--cached", action="store_true",
                    help="reuse the saved record set instead of refetching")
    args = ap.parse_args(argv)

    if args.deep:
        return _main_deep(args)

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
