"""
Reading IMD's Mumbai radar as DATA, not as a picture to link to.

WHAT CHANGED, AND WHY THIS IS NOW POSSIBLE
------------------------------------------
Every previous version of this agent told the reader that radar could not be
ingested, in those words: "IMD's radar images carry no map extent or
projection, and a guessed geometry would put these alerts on the wrong
suburbs." That was wrong, and it was wrong for five days while the page kept
telling people it was dry during rain.

The metadata is there. It is PRINTED IN THE IMAGE rather than embedded in the
file, which is why a byte-level look for georeferencing found nothing:

    DWR MUMBAI ( 18.9013N , 72.8075E , 100.0000 mts )
    Display Range : 250 Km
    Display Res   : 0.9 Km/Pix

That is a complete specification: centre, extent, scale. The projection is a
simple equirectangular one about the radar site, and it was verified rather
than assumed - predicting the pixel for Kalyan, Bhiwandi, Matheran, Igatpuri,
Palghar, Panvel and Alibag from lat/lon put every crosshair on that station's
own printed marker, to within about two pixels (under 2 km).

WHAT THIS IS FOR
----------------
Current conditions, and nothing else. The models answer "how much over the
day"; radar answers "is it raining on me now", and on 12 Sep 2026 the model
said 0.0 mm for the hour while the radar had an echo over Kalyan and a 5 mm/hr
core sixteen kilometres away. For the present tense the radar wins, every
time, and this module exists so the page stops arguing with the sky.

WHAT IT IS NOT
--------------
Not a forecast. An echo is what the beam sees aloft, converted to a surface
rain rate through a Z-R relationship that is a population average, not a
measurement - see MARSHALL_PALMER below. It is right about WHERE and WHETHER
far more than about HOW MUCH.
"""

from __future__ import annotations

import io
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from .sources import USER_AGENT

# The radar site and its display geometry, as printed in the product header.
# Checked against nine labelled stations before being trusted; see module docs.
RADAR_LAT, RADAR_LON = 18.9013, 72.8075
KM_PER_PX = 0.9

# Where the PPI centre sits in the image. Found by intersecting the two
# through-going azimuth spokes rather than assumed, and re-derived at runtime
# so a change to IMD's layout is caught instead of silently mis-locating every
# suburb. This is only the fallback if that search fails.
FALLBACK_CENTRE = (320, 469)

# The colour key, top (strongest) to bottom, is drawn down the right-hand side
# at a fixed pitch. The VALUES are the standard IMD DWR reflectivity ladder;
# the COLOURS are read out of each image at runtime, so a palette change
# cannot silently remap every rain rate on the page.
LEGEND_X = 802
LEGEND_TOP_Y = 475
LEGEND_PITCH = 18
LEGEND_DBZ = (64.00, 61.07, 58.13, 55.20, 52.27, 49.33, 46.40, 43.47,
              40.53, 37.60, 34.67, 31.73, 28.80, 25.87, 22.93)

PRODUCTS = {
    "maxz": "https://mausam.imd.gov.in/Radar/caz_mum.gif",
    "sri": "https://mausam.imd.gov.in/Radar/sri_mum.gif",
}

# Marshall-Palmer, Z = 200 R^1.6, inverted.
#
# This is a population average over many storms, not a measurement of this
# one. Real drop-size distributions vary enough that the same 40 dBZ can be
# 6 mm/hr in one shower and 20 in another, and the monsoon's warm-rain
# processes bias it further. So every rate this module produces is reported as
# an estimate and the BAND is what gets asserted, never the figure - the same
# rule the model forecasts follow.
MARSHALL_PALMER_A = 200.0
MARSHALL_PALMER_B = 1.6

# Below this a return is more likely to be cloud, insects or ground clutter
# than rain reaching anybody.
MIN_RAIN_DBZ = 20.0


def dbz_to_mm_h(dbz: float) -> float:
    z = 10.0 ** (dbz / 10.0)
    return (z / MARSHALL_PALMER_A) ** (1.0 / MARSHALL_PALMER_B)


@dataclass
class RadarScan:
    product: str
    width: int
    height: int
    centre_px: tuple[int, int]
    scanned_at: datetime | None
    # (x, y) -> dBZ for every pixel that carries a legend colour
    dbz: dict[tuple[int, int], float]

    def to_px(self, lat: float, lon: float) -> tuple[int, int]:
        cx, cy = self.centre_px
        dy_km = (lat - RADAR_LAT) * 111.32
        dx_km = ((lon - RADAR_LON) * 111.32
                 * math.cos(math.radians((lat + RADAR_LAT) / 2)))
        return (round(cx + dx_km / KM_PER_PX), round(cy - dy_km / KM_PER_PX))

    def sample(self, lat: float, lon: float, *,
               at_km: float = 3.0, near_km: float = 16.0) -> dict:
        """What the radar has over a place, and what is close to it.

        Two radii on purpose. `at_km` is "is it raining here" - kept tight,
        because the whole point of radar is that it does not smear a cell
        across a 25 km grid box the way the models do. `near_km` answers the
        question a reader asks next, which is whether something heavier is
        about to arrive.
        """
        cx, cy = self.to_px(lat, lon)
        at_r = max(1, round(at_km / KM_PER_PX))
        near_r = max(at_r, round(near_km / KM_PER_PX))

        at: float | None = None
        near: float | None = None
        near_d = 0.0
        for dx in range(-near_r, near_r + 1):
            for dy in range(-near_r, near_r + 1):
                v = self.dbz.get((cx + dx, cy + dy))
                if v is None:
                    continue
                d = math.hypot(dx, dy)
                if d > near_r:
                    continue
                if d <= at_r and (at is None or v > at):
                    at = v
                if near is None or v > near:
                    near, near_d = v, d
        return {
            "dbz_here": at,
            "mm_h_here": dbz_to_mm_h(at) if at is not None else 0.0,
            "dbz_near": near,
            "mm_h_near": dbz_to_mm_h(near) if near is not None else 0.0,
            "near_km": round(near_d * KM_PER_PX, 1) if near is not None else None,
        }

    @property
    def scene_max_dbz(self) -> float | None:
        return max(self.dbz.values()) if self.dbz else None

    @property
    def age_minutes(self) -> float | None:
        if self.scanned_at is None:
            return None
        return (datetime.now() - self.scanned_at).total_seconds() / 60.0


def _find_centre(pixels, width: int, height: int) -> tuple[int, int]:
    """Intersect the two through-going azimuth spokes.

    Searched inside the plot rather than across the whole image: the box
    borders are longer runs of dark pixels than the spokes are, and a naive
    longest-line search locks onto the frame instead of the radar.
    """
    def dark(x: int, y: int) -> bool:
        p = pixels[y * width + x]
        return p[0] < 80 and p[1] < 80 and p[2] < 80

    try:
        best_y, best_n = FALLBACK_CENTRE[1], -1
        for y in range(380, min(560, height)):
            n = sum(1 for x in range(120, min(560, width)) if dark(x, y))
            if n > best_n:
                best_y, best_n = y, n
        best_x, best_n = FALLBACK_CENTRE[0], -1
        for x in range(240, min(420, width)):
            n = sum(1 for y in range(200, min(740, height)) if dark(x, y))
            if n > best_n:
                best_x, best_n = x, n
        # Sanity: the found centre must sit near where the layout puts it.
        if (abs(best_x - FALLBACK_CENTRE[0]) <= 30
                and abs(best_y - FALLBACK_CENTRE[1]) <= 30):
            return (best_x, best_y)
    except Exception:                              # noqa: BLE001
        pass
    return FALLBACK_CENTRE


def fetch(product: str = "maxz", *, quiet: bool = True) -> RadarScan | None:
    """Download one radar product and turn it into dBZ per pixel."""
    try:
        from PIL import Image
    except ImportError:
        if not quiet:
            print("  ! radar needs Pillow (pip install Pillow)")
        return None

    url = PRODUCTS.get(product)
    if url is None:
        return None
    # Cache-bust: IMD serves these with long cache headers and a stale frame
    # reported as "now" is worse than no radar at all.
    url = f"{url}?t={datetime.now():%Y%m%d%H%M%S}"
    scanned_at = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
            scanned_at = _header_time(resp.headers.get("Last-Modified"))
    except Exception as exc:                       # noqa: BLE001
        if not quiet:
            print(f"  ! radar unavailable: {exc}")
        return None

    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:                       # noqa: BLE001
        if not quiet:
            print(f"  ! radar image unreadable: {exc}")
        return None

    w, h = im.size
    pixels = list(im.getdata())

    # Read the colour key out of THIS image, so a palette change is picked up
    # rather than silently mapping every echo to the wrong rain rate.
    key: dict[tuple[int, int, int], float] = {}
    for i, dbz in enumerate(LEGEND_DBZ):
        y = LEGEND_TOP_Y + LEGEND_PITCH * i
        if 0 <= LEGEND_X < w and 0 <= y < h:
            key[pixels[y * w + LEGEND_X]] = dbz
    if len(key) < len(LEGEND_DBZ) - 2:
        if not quiet:
            print(f"  ! radar colour key not found ({len(key)} of "
                  f"{len(LEGEND_DBZ)} swatches) — layout may have changed")
        return None

    centre = _find_centre(pixels, w, h)

    # Only the plot area. Sampling the whole frame would pick the legend
    # swatches themselves up as echoes sitting out in the Arabian Sea.
    dbz: dict[tuple[int, int], float] = {}
    x0, x1 = max(0, centre[0] - 300), min(w, centre[0] + 300)
    y0, y1 = max(0, centre[1] - 300), min(h, centre[1] + 300)
    for y in range(y0, y1):
        row = y * w
        for x in range(x0, x1):
            v = key.get(pixels[row + x])
            if v is not None and v >= MIN_RAIN_DBZ:
                dbz[(x, y)] = v

    return RadarScan(product=product, width=w, height=h, centre_px=centre,
                     scanned_at=scanned_at, dbz=dbz)


def _header_time(value: str | None) -> datetime | None:
    """Scan time from the response's Last-Modified header, in local time.

    The filename IMD prints across the top of the image carries the scan time
    too, but it is DRAWN, not stored - it does not appear in the file's bytes,
    so parsing it would need OCR. The header is exact, free, and has matched
    the printed stamp on every frame checked.

    An unknown age is reported as unknown rather than quietly presented as
    current: a twenty-minute-old frame and a three-hour-old one look identical
    on screen, and only one of them is worth acting on.
    """
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

# Rain-rate bands for what is happening NOW. Deliberately not the IMD 24-hour
# ladder: that one answers "how much fell today" and these have to answer
# "what is it doing outside", which is a different question with different
# thresholds.
NOW_BANDS: tuple[tuple[float, str], ...] = (
    (0.5,  "a few spots"),
    (2.5,  "light rain"),
    (7.5,  "moderate rain"),
    (20.0, "heavy rain"),
    (1e9,  "very heavy rain"),
)


def now_band(mm_h: float) -> str:
    for limit, name in NOW_BANDS:
        if mm_h < limit:
            return name
    return NOW_BANDS[-1][1]


def render_now(scan: RadarScan | None, belts) -> str:
    """The observed-conditions section: what radar has, belt by belt.

    This sits ABOVE the model belt table and outranks it for the present
    tense. The models answer how much over a day; only this answers whether
    it is raining on you right now.
    """
    if scan is None:
        return ""

    age = scan.age_minutes
    when = (f"{scan.scanned_at:%H:%M}" if scan.scanned_at else "an unknown time")
    age_txt = (f"**{age:.0f} minute{'' if 0.5 <= age < 1.5 else 's'} old**"
               if age is not None
               else "**of unknown age — treat it with care**")

    out = "**What the radar can actually see right now**\n\n"
    out += (
        f"This is observation, not forecast. The scan below is from **{when}**, "
        f"{age_txt}. Everything else on this page is a model's opinion about "
        "the day; this is the beam reporting what is in the air over each "
        "place at that moment, so where the two disagree about **right now**, "
        "believe this one.\n\n"
    )

    rows: list[str] = []
    wet_names: list[str] = []
    for belt in belts:
        here_max = 0.0
        near_best: tuple[float, float] | None = None   # (mm_h, km)
        for _nm, la, lo in belt.points:
            r = scan.sample(la, lo)
            here_max = max(here_max, r["mm_h_here"])
            if r["dbz_near"] is not None:
                cand = (r["mm_h_near"], r["near_km"] or 0.0)
                if near_best is None or cand[0] > near_best[0]:
                    near_best = cand

        if here_max >= 0.5:
            wet_names.append(belt.name)
            state = f"**{now_band(here_max).capitalize()}** (~{here_max:.0f} mm/hr)"
        elif here_max > 0:
            state = "Just detectable — a few spots at most"
        else:
            state = "Nothing overhead"

        if near_best and near_best[0] >= 2.5 and near_best[1] > 3:
            nearby = (f"{now_band(near_best[0])} ~{near_best[1]:.0f} km away")
        elif near_best and near_best[0] > 0:
            nearby = "only light echo nearby"
        else:
            nearby = "clear for 16 km around"
        rows.append(f"| **{belt.name}** | {state} | {nearby} |")

    if wet_names:
        out += ("Rain is falling now over **" + ", ".join(wet_names)
                + "**.\n\n")
    else:
        out += "**No rain is reaching the ground anywhere in the MMR** on this scan.\n\n"

    out += "| Area | Overhead now | Nearest echo |\n|---|---|---|\n"
    out += "\n".join(rows) + "\n\n"

    peak = scan.scene_max_dbz
    if peak:
        out += (f"The strongest return anywhere in range is **{peak:.0f} dBZ**, "
                f"about **{dbz_to_mm_h(peak):.0f} mm/hr** — "
                f"{now_band(dbz_to_mm_h(peak))} where it is falling.\n\n")

    out += (
        "> **How to read this.** Rates are converted from reflectivity using "
        "the standard Marshall-Palmer relationship, which is an average over "
        "many storms rather than a measurement of this one — the same 40 dBZ "
        "can be 6 mm/hr in one shower and 20 in another. So trust the "
        "**where** and the **whether** far more than the millimetres. The beam "
        "also rises with distance, so rain far out can be shallower than it "
        "looks, or missed entirely underneath the beam.\n"
    )
    return out
