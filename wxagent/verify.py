"""
Forecast log and verification scoring.

Guide s26 and Handbook Ch.26 both make the same point: the log is what turns
practice into skill. Guide s15 Step 6 is explicit that the original forecast is
never edited after the fact - so this module only ever appends, and the
`actual_mm` column is filled in later by a separate command.

Scores implemented (Guide s26.1):
    Accuracy  = (hits + correct dry) / all
    POD       = hits / (hits + misses)
    FAR       = false alarms / (hits + false alarms)
    CSI       = hits / (hits + misses + false alarms)

Guide s26.1 also warns why accuracy alone misleads: "During a dry period,
saying 'no rain' every day may score well."
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

from . import config as C

FIELDS = (
    "issued", "valid_date", "site", "lead_hours", "regime",
    "p_measurable", "model_lo_mm", "model_hi_mm", "model_median_mm",
    "p_heavy", "predicted_category", "expected_character", "likely_window",
    "conf_occurrence", "conf_amount", "conf_timing",
    "orographic_850", "rh_700", "cape_peak", "trough_phase",
    "actual_mm", "actual_character", "outcome", "lesson", "actual_source",
)

# Where an observed value came from. The distinction is not cosmetic: ERA5 is
# a model analysis, and this project measured it under-reading convective peaks
# by roughly threefold against gauges (Borivali, 11 Aug 2026: 84 mm/12 h
# observed, 29 mm/24 h analysed). A score computed against reanalysis is
# therefore weaker evidence than one computed against a real observation, and
# the scorecard reports them apart rather than blending them.
SOURCE_ERA5 = "era5"
SOURCE_MANUAL = "manual"


def _ensure_header(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=FIELDS).writeheader()


def log_forecast(dd, conf, site_key: str, issued: datetime,
                 trough_phase: str = "", path: Path | None = None) -> None:
    """Append one forecast row. Never overwrites an existing forecast."""
    path = path or C.FORECAST_LOG
    _ensure_header(path)

    from .doctrine import daypart_breakdown, most_likely_window  # local import

    ep = dd.ensemble
    row = {
        "issued": issued.isoformat(timespec="minutes"),
        "valid_date": dd.day.isoformat(),
        "site": site_key,
        "lead_hours": dd.lead_hours,
        "regime": dd.regime,
        "p_measurable": (f"{ep.p_measurable:.3f}"
                         if ep and ep.p_measurable is not None else ""),
        "model_lo_mm": f"{dd.rain.lo:.1f}",
        "model_hi_mm": f"{dd.rain.hi:.1f}",
        "model_median_mm": f"{dd.rain.median:.1f}",
        "p_heavy": (f"{ep.p_heavy:.3f}"
                    if ep and ep.p_heavy is not None else ""),
        "predicted_category": dd.rain.category_hi,
        "expected_character": dd.organisation.character,
        "likely_window": "",
        "conf_occurrence": conf.occurrence,
        "conf_amount": conf.amount,
        "conf_timing": conf.timing,
        "orographic_850": (f"{dd.lift.orographic_850:.1f}"
                           if dd.lift.orographic_850 is not None else ""),
        "rh_700": (f"{dd.moisture.rh_700:.0f}"
                   if dd.moisture.rh_700 is not None else ""),
        "cape_peak": (f"{dd.stability.cape_peak:.0f}"
                      if dd.stability.cape_peak is not None else ""),
        "trough_phase": trough_phase,
        "actual_mm": "", "actual_character": "", "outcome": "", "lesson": "",
    }

    # Skip if this exact (site, valid_date, issued-day) already logged, so a
    # re-run does not pollute the verification sample.
    if _already_logged(path, site_key, dd.day, issued.date()):
        return

    with path.open("a", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=FIELDS).writerow(row)


def _already_logged(path: Path, site: str, valid: date, issued_day: date) -> bool:
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("site") == site
                    and row.get("valid_date") == valid.isoformat()
                    and row.get("issued", "")[:10] == issued_day.isoformat()):
                return True
    return False


# --------------------------------------------------------------------------
# Filling in what actually happened
# --------------------------------------------------------------------------

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def fetch_observed(site_key: str, days: Sequence[date]) -> dict[date, float]:
    """
    Observed daily rainfall from Open-Meteo's ERA5 archive.

    Uses the archive endpoint, whose request budget is separate from the
    forecast API - so verification keeps working on a day the forecast quota
    is spent, which is exactly when you would otherwise lose the record.
    """
    from .sources import _get_json

    site = C.SITES_BY_KEY.get(site_key) or C.HOME
    wanted = sorted(set(days))
    if not wanted:
        return {}
    params = {
        "latitude": site.lat, "longitude": site.lon,
        "start_date": wanted[0].isoformat(), "end_date": wanted[-1].isoformat(),
        "daily": "precipitation_sum", "timezone": C.TIMEZONE,
    }
    try:
        raw = _get_json(ARCHIVE_URL, params, timeout=60)
    except Exception:                             # noqa: BLE001
        return {}
    if isinstance(raw, list):
        raw = raw[0]
    daily = raw.get("daily", {})
    out: dict[date, float] = {}
    for t, v in zip(daily.get("time", []), daily.get("precipitation_sum", [])):
        if v is not None:
            out[date.fromisoformat(t)] = float(v)
    return out


def auto_fill(*, today: date | None = None, lag_days: int = 2,
              path: Path | None = None, quiet: bool = False) -> tuple[int, int]:
    """
    Fill every logged forecast whose day has passed, from the ERA5 archive.

    Returns (filled, still_pending). Rows already carrying a MANUAL value are
    never overwritten - a real observation always outranks reanalysis.
    """
    path = path or C.FORECAST_LOG
    today = today or date.today()
    if not path.exists():
        return 0, 0

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return 0, 0

    cutoff = today - timedelta(days=lag_days)      # ERA5 lags a little
    pending: dict[str, list[date]] = {}
    for row in rows:
        if row.get("actual_mm"):
            continue                               # already verified
        try:
            d = date.fromisoformat(row["valid_date"])
        except (ValueError, KeyError):
            continue
        if d <= cutoff:
            pending.setdefault(row.get("site") or C.HOME.key, []).append(d)

    if not pending:
        return 0, sum(1 for r in rows if not r.get("actual_mm"))

    observed: dict[tuple[str, date], float] = {}
    for site_key, days in pending.items():
        if not quiet:
            print(f"  fetching observations for {site_key} "
                  f"({len(set(days))} day(s))...")
        for d, mm in fetch_observed(site_key, days).items():
            observed[(site_key, d)] = mm

    filled = 0
    for row in rows:
        if row.get("actual_mm"):
            continue
        try:
            d = date.fromisoformat(row["valid_date"])
        except (ValueError, KeyError):
            continue
        mm = observed.get((row.get("site") or C.HOME.key, d))
        if mm is None:
            continue
        row["actual_mm"] = f"{mm:.1f}"
        row["actual_source"] = SOURCE_ERA5
        row["outcome"] = _classify(row, mm)
        filled += 1

    if filled:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows([{k: r.get(k, "") for k in FIELDS} for r in rows])

    still = sum(1 for r in rows if not r.get("actual_mm"))
    return filled, still


def _classify(row: dict, actual_mm: float) -> str:
    forecast_rain = _forecast_said_rain(row)
    observed_rain = actual_mm >= C.MEASURABLE_RAIN_MM
    if forecast_rain and observed_rain:
        return "hit"
    if forecast_rain and not observed_rain:
        return "false_alarm"
    if observed_rain:
        return "miss"
    return "correct_dry"


def record_actual(valid_date: date, site: str, actual_mm: float, *,
                  character: str = "", lesson: str = "",
                  path: Path | None = None) -> int:
    """
    Fill the observed rainfall for a past forecast and classify the outcome.

    Guide s15 Step 6: "Do not edit the original forecast. Write one sentence
    explaining the main error." Only the actual/outcome/lesson columns are
    written here; the forecast columns are left untouched.
    """
    path = path or C.FORECAST_LOG
    if not path.exists():
        return 0

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    updated = 0
    for row in rows:
        if row["valid_date"] != valid_date.isoformat() or row["site"] != site:
            continue

        forecast_rain = _forecast_said_rain(row)
        observed_rain = actual_mm >= C.MEASURABLE_RAIN_MM

        if forecast_rain and observed_rain:
            outcome = "hit"
        elif forecast_rain and not observed_rain:
            outcome = "false_alarm"
        elif not forecast_rain and observed_rain:
            outcome = "miss"
        else:
            outcome = "correct_dry"

        row["actual_mm"] = f"{actual_mm:.1f}"
        row["actual_character"] = character
        row["outcome"] = outcome
        row["actual_source"] = SOURCE_MANUAL     # outranks any auto-fill
        if lesson:
            row["lesson"] = lesson
        updated += 1

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return updated


def _forecast_said_rain(row: dict) -> bool:
    """
    The binary event, defined before scoring (Guide s26): the forecast
    'predicted rain' if ensemble probability was >= 50%, or - absent an
    ensemble - if the median model total reached the measurable threshold.
    """
    p = row.get("p_measurable", "")
    if p:
        try:
            return float(p) >= 0.5
        except ValueError:
            pass
    try:
        return float(row.get("model_median_mm") or 0) >= C.MEASURABLE_RAIN_MM
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

@dataclass
class Scores:
    n: int
    hits: int
    misses: int
    false_alarms: int
    correct_dry: int

    @property
    def accuracy(self) -> float | None:
        return (self.hits + self.correct_dry) / self.n if self.n else None

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


def score(site: str | None = None, *, max_lead_hours: int | None = None,
          source: str | None = None, path: Path | None = None) -> Scores:
    path = path or C.FORECAST_LOG
    counts = {"hit": 0, "miss": 0, "false_alarm": 0, "correct_dry": 0}
    if not path.exists():
        return Scores(0, 0, 0, 0, 0)

    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if site and row["site"] != site:
                continue
            if source and (row.get("actual_source") or SOURCE_ERA5) != source:
                continue
            if max_lead_hours is not None:
                try:
                    if int(row["lead_hours"]) > max_lead_hours:
                        continue
                except (ValueError, KeyError):
                    continue
            outcome = row.get("outcome", "")
            if outcome in counts:
                counts[outcome] += 1

    n = sum(counts.values())
    return Scores(n, counts["hit"], counts["miss"],
                  counts["false_alarm"], counts["correct_dry"])


def calibration(path: Path | None = None) -> list[tuple[str, int, float | None]]:
    """
    Probability calibration (Guide s26.2): "Collect all forecasts where you
    assigned 70% probability. If rain occurs in about 70% of them, the
    forecasts are calibrated."
    """
    path = path or C.FORECAST_LOG
    buckets: dict[str, list[bool]] = {}
    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            p_raw, actual = row.get("p_measurable", ""), row.get("actual_mm", "")
            if not p_raw or not actual:
                continue
            try:
                p, mm = float(p_raw), float(actual)
            except ValueError:
                continue
            lo = int(p * 10) * 10
            label = f"{lo}-{min(100, lo + 10)}%"
            buckets.setdefault(label, []).append(mm >= C.MEASURABLE_RAIN_MM)

    out = []
    for label in sorted(buckets, key=lambda s: int(s.split("-")[0])):
        hits = buckets[label]
        rate = sum(hits) / len(hits) if hits else None
        out.append((label, len(hits), rate))
    return out


def render_scorecard(site: str | None = None) -> str:
    s = score(site)
    if s.n == 0:
        return ("No verified forecasts yet. Record outcomes with:\n\n"
                "    python -m wxagent verify --date YYYY-MM-DD --mm 23.5\n\n"
                "Guide s29.1: build a transparent, time-stamped, verified "
                "record over 60-90 days before treating the output as "
                "trustworthy - let alone selling it.\n")

    def pc(v: float | None) -> str:
        return "--" if v is None else f"{v:.0%}"

    out = f"# Verification scorecard{f' — {site}' if site else ''}\n\n"
    out += (f"Sample: **{s.n}** verified forecasts "
            f"({s.hits} hits, {s.misses} misses, {s.false_alarms} false "
            f"alarms, {s.correct_dry} correct dry)\n\n")

    era = score(site, source=SOURCE_ERA5)
    man = score(site, source=SOURCE_MANUAL)
    if era.n and man.n:
        out += (f"Of those, {era.n} were verified against ERA5 reanalysis and "
                f"{man.n} against observations you entered by hand.\n\n")
    elif era.n:
        out += (f"All {era.n} were verified against ERA5 reanalysis — see the "
                "caveat at the foot of this card.\n\n")
    out += "| Score | Value | What it means |\n|---|---|---|\n"
    out += f"| Accuracy | {pc(s.accuracy)} | Can be misleading on its own — a dry spell rewards always saying 'no rain'. |\n"
    out += f"| POD | {pc(s.pod)} | Share of observed rain events you caught. |\n"
    out += f"| FAR | {pc(s.far)} | Share of your rain forecasts that verified dry. |\n"
    out += f"| CSI | {pc(s.csi)} | Balances hits, misses and false alarms — the honest headline number. |\n\n"

    cal = calibration()
    if cal:
        out += "## Probability calibration\n\n"
        out += "| Forecast probability | Cases | Observed rain rate |\n|---|---|---|\n"
        for label, n, rate in cal:
            out += f"| {label} | {n} | {pc(rate)} |\n"
        out += ("\nGuide s26.2: if you say 70% and it rains about 70% of those "
                "times, you are calibrated. Calibration is more valuable than "
                "sounding certain.\n")

    if era.n:
        out += (
            "\n> **What 'verified' means here.** Auto-filled rows are scored "
            "against ERA5 reanalysis, not rain gauges. This project measured "
            "ERA5 under-reading convective peaks roughly threefold against "
            "gauges (Borivali, 11 Aug 2026: 84 mm in 12 h observed against "
            "29 mm in 24 h analysed). It is reliable for **did it rain at all**, "
            "which is what these scores test, and unreliable for **how much**. "
            "Entering a real gauge figure with `verify --date … --mm …` "
            "overrides the auto-filled value and is always the better record.\n")
    return out
