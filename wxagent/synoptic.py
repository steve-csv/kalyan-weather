"""
Synoptic setting - the pressure-pattern layer both bulletins open with.

Handbook Ch.25 puts this first in the morning routine, ahead of anything
local: "Pressure layer - trough/ridge position. This single layer, checked
first, tells you more about the day's character than almost anything else."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from . import config as C
from .diagnostics import (
    OffshoreTroughDiagnosis, TroughDiagnosis, monsoon_trough, offshore_trough,
)
from .sources import PressureTransect, fetch_pressure_transect


@dataclass
class SynopticPicture:
    trough: TroughDiagnosis
    offshore: OffshoreTroughDiagnosis
    season: str
    valid_for: date


def fetch_synoptic(days: int = 7) -> tuple[PressureTransect | None,
                                           PressureTransect | None,
                                           PressureTransect | None]:
    """Fetch the three pressure lines used to diagnose the pattern."""
    trough = fetch_pressure_transect(C.MONSOON_TROUGH_TRANSECT, days=days)
    offshore = fetch_pressure_transect(C.OFFSHORE_TROUGH_LINE, days=days)
    inland = fetch_pressure_transect(C.INLAND_REFERENCE_LINE, days=days)
    return trough, offshore, inland


def _hour_index(transect: PressureTransect | None, day: date,
                hour: int = 12) -> int:
    """Index of local `hour` on `day` within a transect's time axis."""
    if transect is None:
        return 0
    for i, t in enumerate(transect.times):
        dt = datetime.fromisoformat(t)
        if dt.date() == day and dt.hour == hour:
            return i
    return 0


def build(trough_t: PressureTransect | None,
          offshore_t: PressureTransect | None,
          inland_t: PressureTransect | None,
          day: date, season: str) -> SynopticPicture:
    idx = _hour_index(trough_t, day)
    idx_o = _hour_index(offshore_t, day)
    return SynopticPicture(
        trough=monsoon_trough(trough_t, idx),
        offshore=offshore_trough(offshore_t, inland_t, idx_o),
        season=season,
        valid_for=day,
    )


def render(sp: SynopticPicture) -> str:
    """Markdown block for the bulletin."""
    lines: list[str] = []
    lines.append(f"**Season:** {C.SEASON_LABELS[sp.season]}. "
                 f"{C.SEASON_FRAMEWORK[sp.season]}\n")

    t = sp.trough
    if t.axis_lat is not None:
        lines.append(
            f"**Monsoon trough:** axis near **{t.axis_lat:.1f}°N** "
            f"(minimum {t.min_pressure:.1f} hPa along the 80°E transect) — "
            f"**{t.phase}**.  \n{t.interpretation}\n"
        )
    else:
        lines.append(f"**Monsoon trough:** {t.interpretation}\n")

    o = sp.offshore
    lines.append(f"**Cross-coast gradient:** {o.interpretation}\n")

    if sp.season == "monsoon":
        if t.phase == "active-leaning" and o.present:
            lines.append(
                "> Both the large-scale trough and the coastal trough favour "
                "rain. Guide s12: this is the configuration where the system, "
                "the wind response and the lifting zone all line up over the "
                "Konkan.\n"
            )
        elif t.phase == "break-leaning" and not o.present:
            lines.append(
                "> Both features argue against significant rain. Handbook "
                "Ch.13: expect a drier, hotter, humid-but-rainless run of days "
                "until the trough moves back south.\n"
            )
        else:
            lines.append(
                "> The two features disagree. Guide s10.3: models often "
                "disagree most on timing during these transitions, so lean "
                "harder on observations — radar and satellite — than usual.\n"
            )
    return "\n".join(lines)
