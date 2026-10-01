"""
Monsoon onset and withdrawal, diagnosed instead of read off the calendar.

WHY THIS EXISTS
---------------
On 1 October 2026 the page announced "Post-monsoon transition" because
`SEASONS[10]` says October is post-monsoon - while an Arabian Sea low sat
139 km off the coast and IMD had not withdrawn the monsoon from Mumbai. The
calendar is a climatology, not a diagnosis: withdrawal from the Konkan has
fallen anywhere from late September to the third week of October.

The cost was not only a wrong label. The wording layer keys its measured
low-probability correction on `season == "monsoon"` (plain.py,
LOW_PROB_MONSOON_RAIN_RATE), so a calendar flip silently switched off a
correction the backtest says applies to 22% of quiet days - on the first of
the month, with the monsoon still over the region.

IMD'S CRITERIA FOR WITHDRAWAL OVER A REGION (Handbook Ch.13)
------------------------------------------------------------
  1. No rainfall over the area for five consecutive days.
  2. An anticyclonic circulation established in the lower troposphere
     (850 hPa) - operationally, the loss of the monsoon westerly.
  3. A considerable reduction in moisture. IMD uses RH <= 50% at 850 hPa for
     northwest India; that number belongs to NW India, not the Konkan, so it
     is reported here as a comparison and never as a pass/fail test.
  4. The monsoon trough ceases to be identifiable, pushed south and replaced
     by post-monsoon easterlies.

WHAT THIS MODULE CAN AND CANNOT DO
----------------------------------
It cannot observe any of those. Each is estimated from the primary model's
analysis of the last ten days at one point, which is a far thinner basis than
the station network IMD declares from. **IMD's declaration is the only
official withdrawal date**; this is a reading of whether the signature is
present, and it says so wherever it is reported.

The distinction that matters most here is between a BREAK and a WITHDRAWAL.
They look identical in these fields - dry, westerly lost, drier column - and
the only thing separating them is the date and what happens next. A break in
July ends; a withdrawal in October does not. So the same signature is read as
a break before `WITHDRAWAL_EARLIEST` and as withdrawal after it, and the
module never calls a withdrawal in mid-monsoon however dry it gets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Sequence

from . import config as C

# A day with less than this much rain counts as "no rainfall" for criterion 1.
# IMD says no rainfall; a hard zero is unreachable in a model field, where
# trace drizzle appears in almost every grid box during the season.
DRY_DAY_MM = 1.0
DRY_RUN_NEEDED = 5

# The monsoon westerly at 850 hPa. Outside this arc the moisture-bearing
# current is gone, whatever the surface wind is doing.
MONSOON_ARC = (200.0, 290.0)
MONSOON_MIN_MS = 3.0            # below this the direction means little
WESTERLY_SHARE = 0.4            # of the recent window, to still call it present

# IMD's NW-India moisture criterion, carried for comparison only.
IMD_RH850_DRY = 50.0
# Konkan-specific: what the column actually does here when the monsoon goes.
# Monsoon RH at 850 over Kalyan runs 80-95%; post-withdrawal it sits in the
# 40-60s. 65 is the midpoint, and it is a reading, not IMD's test.
KONKAN_RH850_DRY = 65.0

# No withdrawal is called before this day-of-year, however dry it looks: in
# July and August the same fields mean a break. The earliest withdrawal from
# the Konkan in the modern record is in the last days of September.
WITHDRAWAL_EARLIEST = (9, 25)

# Days of model analysis to look back over.
LOOKBACK_DAYS = 10
RECENT_WINDOW = 5


@dataclass
class MonsoonState:
    """Where the monsoon is in its life cycle, and on what evidence."""
    state: str                  # active | break | withdrawing | withdrawn | outside
    label: str                  # short human label for the page
    calendar_season: str        # what SEASONS[month] says
    effective_season: str       # what the wording layer should use
    dry_run: int                # trailing days under DRY_DAY_MM
    westerly_share: float       # share of the recent window in the monsoon arc
    rh850: float | None         # mean 850 hPa RH over the recent window
    pwat: float | None          # mean precipitable water, mm
    mean_dir: float | None      # vector-mean 850 hPa direction
    trough_lat: float | None
    criteria: list[tuple[str, bool, str]] = field(default_factory=list)
    sentence: str = ""
    source_note: str = ""

    @property
    def met(self) -> int:
        return sum(1 for _, ok, _ in self.criteria if ok)

    @property
    def is_monsoon(self) -> bool:
        return self.effective_season == "monsoon"


def _vector_mean(dirs: Sequence[float], speeds: Sequence[float]
                 ) -> float | None:
    """Mean wind direction weighted by speed.

    A plain arithmetic mean of 350 deg and 10 deg is 180 - the exact opposite
    of the two winds it averages. Directions only average as vectors.
    """
    x = y = 0.0
    for d, s in zip(dirs, speeds):
        if d is None or s is None:
            continue
        r = math.radians(d)
        x += s * math.sin(r)
        y += s * math.cos(r)
    if x == 0.0 and y == 0.0:
        return None
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _in_arc(deg: float | None) -> bool:
    if deg is None:
        return False
    lo, hi = MONSOON_ARC
    return lo <= deg <= hi


def _daily_rollup(ms, times: Sequence[str]) -> dict[date, dict]:
    """Collapse one model's hourly series into per-day numbers."""
    out: dict[date, dict] = {}
    precip = ms.get("precipitation")
    rh = ms.get("relative_humidity_850hPa")
    wd = ms.get("wind_direction_850hPa")
    wsp = ms.get("wind_speed_850hPa")
    pw = ms.get("total_column_integrated_water_vapour")
    for i, t in enumerate(times):
        try:
            day = datetime.fromisoformat(t).date()
        except ValueError:
            continue
        slot = out.setdefault(day, {"mm": 0.0, "rh": [], "dir": [], "spd": [],
                                    "pw": []})
        for src, key in ((precip, "mm"), (rh, "rh"), (wd, "dir"),
                         (wsp, "spd"), (pw, "pw")):
            if i >= len(src):
                continue
            v = src[i]
            if v is None:
                continue
            if key == "mm":
                slot["mm"] += v
            else:
                slot[key].append(v)
    return out


def _mean(xs: Sequence[float]) -> float | None:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def diagnose(pf, *, today: date | None = None,
             trough_lat: float | None = None,
             primary: str | None = None) -> MonsoonState:
    """Read the monsoon's state from the last `LOOKBACK_DAYS` of analysis.

    `pf` must be a PointForecast fetched with `past_days` set, or the dry-run
    count has nothing to count. With no past data the function degrades to the
    calendar rather than guessing, and says so in `source_note`.
    """
    today = today or date.today()
    cal = C.SEASONS[today.month]

    if not pf or not pf.models:
        return MonsoonState(
            state="outside", label=C.SEASON_LABELS.get(cal, cal),
            calendar_season=cal, effective_season=cal,
            dry_run=0, westerly_share=0.0, rh850=None, pwat=None,
            mean_dir=None, trough_lat=trough_lat,
            source_note="No model data; season is the calendar's.")

    key = primary if primary in pf.models else next(iter(pf.models))
    ms = pf.models[key]
    by_day = _daily_rollup(ms, pf.times)

    # Yesterday backwards: today is still in progress and would understate a
    # wet day as dry for most of the morning.
    past = sorted(d for d in by_day if d < today)
    recent = past[-RECENT_WINDOW:]

    dry_run = 0
    for d in reversed(past):
        if by_day[d]["mm"] < DRY_DAY_MM:
            dry_run += 1
        else:
            break

    arc_days = [d for d in recent
                if _in_arc(_vector_mean(by_day[d]["dir"], by_day[d]["spd"]))
                and (_mean(by_day[d]["spd"]) or 0.0) >= MONSOON_MIN_MS]
    share = len(arc_days) / len(recent) if recent else 0.0

    rh850 = _mean([v for d in recent for v in by_day[d]["rh"]])
    pwat = _mean([v for d in recent for v in by_day[d]["pw"]])
    mean_dir = _vector_mean([v for d in recent for v in by_day[d]["dir"]],
                            [v for d in recent for v in by_day[d]["spd"]])

    # ---- the four criteria, each reported whether or not it is met --------
    crit: list[tuple[str, bool, str]] = []
    crit.append((
        "Five rainless days",
        dry_run >= DRY_RUN_NEEDED,
        f"{dry_run} consecutive day{'s' if dry_run != 1 else ''} under "
        f"{DRY_DAY_MM:g} mm (IMD asks for {DRY_RUN_NEEDED})."))
    crit.append((
        "Monsoon westerly lost at 850 hPa",
        share < WESTERLY_SHARE,
        (f"The moisture-bearing current is in the monsoon arc on "
         f"{len(arc_days)} of the last {len(recent)} days"
         + (f"; the mean flow is from {mean_dir:.0f}°" if mean_dir else "")
         + ".")))
    crit.append((
        "Column dried out",
        rh850 is not None and rh850 <= KONKAN_RH850_DRY,
        (f"850 hPa humidity averages {rh850:.0f}% over five days"
         f" — monsoon values here are 80-95%, and IMD's northwest-India "
         f"criterion is {IMD_RH850_DRY:g}%."
         if rh850 is not None else "No 850 hPa humidity available.")))
    crit.append((
        "Monsoon trough gone from the plains",
        trough_lat is None or trough_lat >= 28.0,
        (f"The axis is at {trough_lat:.1f}°N."
         if trough_lat is not None else
         "No identifiable trough along the 80°E transect.")))

    # Only the first three decide the state. The trough position is reported
    # because it is the thing a reader can check against an IMD chart, but a
    # trough sitting north is the normal break signature too, so letting it
    # vote would call a withdrawal every time the monsoon went quiet.
    core = sum(1 for _, ok, _ in crit[:3] if ok)
    too_early = (today.month, today.day) < WITHDRAWAL_EARLIEST

    # ---- state -----------------------------------------------------------
    if cal in ("winter", "pre_monsoon"):
        state = "outside"
    elif core >= 3 and not too_early:
        state = "withdrawn"
    elif core == 2 and not too_early:
        state = "withdrawing"
    elif core >= 2:
        state = "break"
    else:
        state = "active"

    label, eff, sentence = _wording(state, today, cal, dry_run, share,
                                    rh850, core)
    return MonsoonState(
        state=state, label=label, calendar_season=cal, effective_season=eff,
        dry_run=dry_run, westerly_share=share, rh850=rh850, pwat=pwat,
        mean_dir=mean_dir, trough_lat=trough_lat, criteria=crit,
        sentence=sentence,
        source_note=(
            f"Diagnosed from {ms.label}'s analysis of the last "
            f"{len(past)} days at {pf.site_name}, not from station reports. "
            "IMD's declaration is the official one."))


def _wording(state: str, today: date, cal: str, dry_run: int,
             share: float, rh850: float | None, core: int
             ) -> tuple[str, str, str]:
    """Label, effective season, and the sentence the page prints."""
    normal = "the Konkan normally loses the monsoon in the first half of October"

    if state == "outside":
        return (C.SEASON_LABELS.get(cal, cal), cal, "")

    if state == "withdrawn":
        return (
            "Monsoon withdrawn (unofficially)",
            "post_monsoon",
            "All three of IMD's withdrawal tests now read as met here: "
            f"{dry_run} rainless days, the 850 hPa westerly gone, and the "
            "column dried out. On this evidence the monsoon has left the "
            "region — but IMD declares withdrawal from its own station "
            f"network, and {normal}, so treat this as the signature arriving "
            "rather than the date being set.")

    if state == "withdrawing":
        return (
            "Monsoon withdrawing",
            "monsoon",
            f"Two of IMD's three withdrawal tests read as met ({core} of 3). "
            "The monsoon is going but is not gone: this is the stage where a "
            "late Arabian Sea system can still put a week's rain into the "
            "Konkan, so the wording here keeps treating a quiet forecast as "
            "'nothing organised' rather than 'dry'.")

    if state == "break":
        early = (" It is too early in the season to read this as withdrawal; "
                 "a break ends.")
        return (
            "Southwest monsoon — in a break",
            "monsoon",
            f"The region is in a break: {dry_run} rainless days and the "
            "westerly weakened. A break and a withdrawal look identical in "
            "these fields." + early)

    # Do not call 64% humid. The criteria table immediately above this
    # sentence may already have scored the column as dried out, and a summary
    # that contradicts its own evidence table is worse than no summary.
    if rh850 is None:
        moisture = ""
    elif rh850 > KONKAN_RH850_DRY + 10:
        moisture = f" and the column is still humid at {rh850:.0f}% at 850 hPa"
    elif rh850 > KONKAN_RH850_DRY:
        moisture = (f" though the column has thinned to {rh850:.0f}% at "
                    "850 hPa")
    else:
        moisture = (f", though at {rh850:.0f}% the column has already dried to "
                    "post-monsoon values — the moisture goes before the wind "
                    "does, and this is what the fortnight before withdrawal "
                    "looks like")

    return (
        "Southwest monsoon — active",
        "monsoon",
        ("The monsoon is still established over the region: the 850 hPa "
         f"westerly is present on {share:.0%} of recent days"
         + moisture
         + ". No withdrawal signature yet."
         + (f" For reference, {normal}, so this is the window to watch."
            if cal == "post_monsoon" else "")))


def render(st: MonsoonState | None) -> str:
    """Markdown block for the bulletin."""
    if st is None or st.state == "outside" or not st.sentence:
        return ""
    out = f"## Monsoon status\n**{st.label}.** {st.sentence}\n\n"
    out += "| IMD withdrawal test | Reads as | Evidence |\n|---|---|---|\n"
    for name, ok, why in st.criteria:
        out += f"| {name} | {'met' if ok else 'not met'} | {why} |\n"
    out += f"\n> {st.source_note}\n\n"
    return out
