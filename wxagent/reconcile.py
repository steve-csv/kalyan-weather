"""
Making the two nowcast sections agree, or say why they do not.

THE PROBLEM THIS EXISTS FOR
---------------------------
The page carries two views of the next few hours and they can flatly
contradict each other.

On 2 September 2026 a forecaster posted a radar-based nowcast warning of rain
reaching Mumbai. Our upstream scan agreed with him: 17 of 30 sampled points
upstream were wet, the nearest active area was 25 km away, and it was a train
of areas rather than one band. Our belt table, on the same page, from the same
run, called the island city and Navi Mumbai "dry".

Both were reporting honestly. They measure different things:

  * the SCAN counts how many upstream sample points have any rain at all. It
    answers "is something out there and heading this way".
  * the BELTS read the model's hourly rainfall AT each place. They answer
    "how much does the model put on your street this hour".

A 25 km grid box smears a convective train into a low hourly average, so the
belts go quiet while the scan still sees the cells. The scan is the better
instrument for "is anything coming"; the belts for "how much, eventually".

Presented side by side with nothing joining them, a reader simply believes
whichever they read last. So when they disagree, the page now says so, and
says which one to trust for the question being asked.
"""

from __future__ import annotations

# A cell this close, arriving this soon, is worth warning about even when the
# hourly numbers are small.
NEAR_KM = 60.0
SOON_HOURS = 1.5

# Below this the belt table is effectively saying "nothing much".
QUIET_MM_H = 1.5


def belts_vs_scan(scan, belt_status) -> str:
    """A note for the belt section when the upstream scan disagrees with it.

    Returns "" when the two broadly agree, which is most of the time.
    """
    if scan is None or not belt_status:
        return ""

    nearest = getattr(scan, "nearest", None)
    if nearest is None:
        return ""

    eta = getattr(scan, "eta_hours", None)
    close = nearest.distance_km <= NEAR_KM
    soon = eta is not None and eta <= SOON_HOURS
    if not (close and soon):
        return ""

    # How much of the upstream fan is wet? A handful of points is noise; half
    # the fan is a wet airmass moving in.
    cells = getattr(scan, "cells", []) or []
    active = [c for c in cells if getattr(c, "mm_now", 0) > 0.1]
    if len(cells) < 8 or len(active) < len(cells) * 0.3:
        return ""

    peak = max((b.peak_mm_h for b in belt_status), default=0.0)
    if peak > QUIET_MM_H:
        return ""          # the belts already expect something; no conflict

    quiet = [b.belt.name for b in belt_status if b.state in ("dry", "drizzling")]
    return (
        "**The upstream scan disagrees with the table above, and on this "
        "question the scan is the one to believe.** It finds "
        f"{len(active)} of {len(cells)} sample points upstream already wet, "
        f"with the nearest rain about {nearest.distance_km:.0f} km away and "
        f"roughly {eta * 60:.0f} minutes out on the current wind. The hourly "
        "figures stay low because they are an average over a 25 km square, and "
        "an average is exactly the wrong measure for scattered cells — most of "
        "the square is dry while the bit under the cell is not.\n\n"
        "So treat the quiet-looking belts"
        + (f" — {', '.join(quiet[:4])}" if quiet else "")
        + " — as **not yet raining** rather than as staying dry. And for the "
        "next hour specifically, the radar loop beats both of these."
    )
