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
    # Which radar, and its display geometry. Defaults are the Colaba S-band.
    site: str = "S-band DWR Mumbai (Colaba)"
    site_lat: float = RADAR_LAT
    site_lon: float = RADAR_LON
    km_per_px: float = KM_PER_PX
    # False when scanned_at is only IMD's UPLOAD time rather than the scan's
    # own printed time - see _header_time. The page then says "published".
    time_exact: bool = True
    published_at: datetime | None = None
    # A cropped copy of the picture for the page, as a data: URI, plus the
    # colour key it was decoded with: [(dBZ, "#rrggbb"), ...].
    image: str | None = None
    legend: tuple[tuple[float, str], ...] = ()
    # Why this radar and not the other one, when the page fell back.
    note: str = ""
    # Whether the OTHER radar is fit to confirm or doubt this one this run,
    # and if not, why - see can_check().
    check_ok: bool = True
    check_note: str = ""

    def to_px(self, lat: float, lon: float) -> tuple[int, int]:
        cx, cy = self.centre_px
        dy_km = (lat - self.site_lat) * 111.32
        dx_km = ((lon - self.site_lon) * 111.32
                 * math.cos(math.radians((lat + self.site_lat) / 2)))
        return (round(cx + dx_km / self.km_per_px),
                round(cy - dy_km / self.km_per_px))

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
        at_r = max(1, round(at_km / self.km_per_px))
        near_r = max(at_r, round(near_km / self.km_per_px))

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
            "near_km": (round(near_d * self.km_per_px, 1)
                        if near is not None else None),
        }

    @property
    def scene_max_dbz(self) -> float | None:
        return max(self.dbz.values()) if self.dbz else None

    @property
    def age_minutes(self) -> float | None:
        if self.scanned_at is None:
            return None
        return (datetime.now() - self.scanned_at).total_seconds() / 60.0

    @property
    def short_name(self) -> str:
        return "C-band" if self.site.startswith("C-band") else "S-band"


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

    # scanned_at here is the UPLOAD time; see _header_time.
    return RadarScan(product=product, width=w, height=h, centre_px=centre,
                     scanned_at=scanned_at, dbz=dbz, time_exact=False,
                     published_at=scanned_at)


def _header_time(value: str | None) -> datetime | None:
    """The response's Last-Modified header, in local time.

    This is when IMD UPLOADED the file, not when the radar scanned. The two
    were once thought to match; on 21 Sep 2026 they did not - a Colaba frame
    stamped 15:23:35 UTC was uploaded at 15:35:01 - so a scan read this way
    is marked time_exact=False and the page says "published", not
    "scanned". The C-band's printed time is read off the image instead; see
    _cband_time.

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
# C-band DWR Mumbai (Veravali, Andheri) - the primary radar from 21 Sep 2026
# --------------------------------------------------------------------------
#
# IMD's second Mumbai radar, and the better one for this page:
#   * about 30 km from Kalyan against Colaba's 50, so the beam is lower over
#     the belts that matter and less shallow rain slips underneath it;
#   * 0.28 km per pixel against 0.9, so a cell over Dombivli is not smeared
#     into Kalyan;
#   * a colour key in 1 dBZ steps against Colaba's 15 dBZ ones, so light and
#     moderate rain are told apart instead of sharing a swatch.
# Its weakness is the wavelength. A C-band beam loses power passing through
# heavy rain, so a cell BEHIND a heavy core, seen from Andheri, reads weaker
# than it is. Colaba's S-band does not suffer that, and stays as the fallback
# whenever this one cannot be read or is out of date.
#
# The geometry was MEASURED, not assumed. The header prints the site and a
# 249 km range; the rings drawn every 50 km sit 177.8 px apart (100 / 150 /
# 200 / 249 km at 357.5 / 535 / 712.5 / 886.5 px), which is 0.2807 km per
# pixel, and the lat/lon ticks and the Surat, Nashik and Pune labels land
# where an equirectangular mapping about the site puts them.
CBAND_URL = "https://mausam.imd.gov.in/Radar/caz_vrv.gif"
CBAND_SITE = "C-band DWR Mumbai (Veravali)"
CBAND_LAT, CBAND_LON = 19.1342, 72.8762
CBAND_KM_PER_PX = 0.2807
CBAND_SIZE = (3045, 2466)
CBAND_CENTRE = (930, 1527)
# The plan view inside its frame. The strips above and to the right of it are
# vertical cross-sections, not maps, and must never be sampled as if they were.
CBAND_MAP = (40, 640, 1815, 2425)             # x0, y0, x1, y1 (exclusive)
# The colour bar: its vertical extent and pitch - one dBZ per ~42 px. Its
# column is found in each frame, and the colours are read from it, so a
# palette change is picked up rather than silently remapped.
CBAND_BAR_Y = (636, 2422)
CBAND_BAR_PX_PER_DBZ = 41.95
# Pure white is the 39-40 dBZ band - and also the fill of the range labels
# drawn on the map. White counts as echo only when echo surrounds it.
CBAND_WHITE = (254, 254, 254)
# The header column, where the scan time is printed.
CBAND_HEADER = (2450, 0, 3045, 640)
# How far (RGB distance) a map colour may sit from a bar colour and still be
# read as that echo. The nearest background colour measured was 28 away.
CBAND_COLOUR_TOL = 25.0
# Pixels per side of the squares the scan is summarised on (~1.1 km).
CBAND_BLOCK = 4
# Of the nine squares around one (itself included), how many must carry echo
# for it to count. Four keeps any shower two kilometres or more across.
CBAND_SUPPORT = 4
# What the page shows: the MMR, the Ghats and the sea upwind of both.
CBAND_VIEW = (20.05, 18.45, 72.30, 73.95)     # north, south, west, east

# IMD's upload lag, for the S-band, whose time is not read off the image: on
# 21 Sep 2026 a Colaba frame stamped 15:23:35 UTC was published at 15:35:01.
# Used only to widen the staleness test, never shown as a time.
PUBLISH_LAG_ALLOWANCE_MIN = 15.0

# Digit reading. Every known digit scored at least 0.977 against its own
# template; the closest wrong digit inside the same hole-count group scored
# 0.78, and 0 against 6 reached 0.87. Accept at 0.93; read "no hole, matches
# nothing" as 3 only below 0.90, and leave the band between unread.
GLYPH_ACCEPT = 0.93
GLYPH_THREE_BELOW = 0.90
HOLES = {"0": 1, "1": 0, "2": 0, "3": 0, "4": 1,
         "5": 0, "6": 1, "7": 0, "8": 2, "9": 1}

_TEMPLATES: dict[str, list[list[float]]] | None = None


def _templates() -> dict[str, list[list[float]]]:
    global _TEMPLATES
    if _TEMPLATES is None:
        from .radar_glyphs import GLYPHS
        t = {d: [[int(c, 16) / 15.0 for c in row] for row in rows]
             for d, rows in GLYPHS.items()}
        # 9 is 6 turned through 180 degrees in this typeface family.
        t.setdefault("9", [row[::-1] for row in t["6"][::-1]])
        _TEMPLATES = t
    return _TEMPLATES


def _holes(g: list[list[float]]) -> list[float]:
    """Enclosed counters in a glyph, as the height fraction of each centroid."""
    h, w = len(g), len(g[0])
    H, W = h + 2, w + 2
    paper = [[not (0 < y < H - 1 and 0 < x < W - 1 and g[y - 1][x - 1] >= 0.5)
              for x in range(W)] for y in range(H)]
    seen = [[False] * W for _ in range(H)]
    found: list[float] = []
    for sy in range(H):
        for sx in range(W):
            if not paper[sy][sx] or seen[sy][sx]:
                continue
            stack, rows, touches_edge = [(sy, sx)], [], False
            seen[sy][sx] = True
            while stack:
                y, x = stack.pop()
                rows.append(y)
                if y in (0, H - 1) or x in (0, W - 1):
                    touches_edge = True
                for yy, xx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if (0 <= yy < H and 0 <= xx < W and paper[yy][xx]
                            and not seen[yy][xx]):
                        seen[yy][xx] = True
                        stack.append((yy, xx))
            if not touches_edge:
                found.append((sum(rows) / len(rows) - 1) / h)
    return found


def _match(g: list[list[float]], t: list[list[float]]) -> float:
    """Normalised overlap of two glyphs, best over one-pixel shifts. Needed
    because the same digit lands on a different sub-pixel offset each time."""
    best = 0.0
    hh, ww = max(len(g), len(t)) + 1, max(len(g[0]), len(t[0])) + 1
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            num = gg = tt = 0.0
            for y in range(hh):
                for x in range(ww):
                    a = g[y][x] if y < len(g) and x < len(g[0]) else 0.0
                    ty, tx = y + dy, x + dx
                    b = (t[ty][tx] if 0 <= ty < len(t) and 0 <= tx < len(t[0])
                         else 0.0)
                    num += a * b
                    gg += a * a
                    tt += b * b
            if gg and tt:
                best = max(best, num / math.sqrt(gg * tt))
    return best


def _digit(g: list[list[float]]) -> str | None:
    holes = _holes(g)
    group = [d for d, n in HOLES.items() if n == len(holes)]
    temps = _templates()
    scores = {d: _match(g, temps[d]) for d in group if d in temps}
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] >= GLYPH_ACCEPT:
            # The derived 9 must also have its counter in the upper half.
            if best == "9" and not (holes and holes[0] < 0.5):
                return None
            return best
    if not holes and all(s < GLYPH_THREE_BELOW for s in scores.values()):
        return "3"
    return None


def _text_lines(px, box) -> list[list[list[list[float]]]]:
    """Glyphs of each printed line inside `box`, as ink grids (0..1)."""
    x0, y0, x1, y1 = box

    def ink(x, y):
        return px[x, y] < 128

    rows = [y for y in range(y0, y1) if any(ink(x, y) for x in range(x0, x1))]
    bands, cur = [], []
    for y in rows:
        if cur and y != cur[-1] + 1:
            bands.append((cur[0], cur[-1]))
            cur = []
        cur.append(y)
    if cur:
        bands.append((cur[0], cur[-1]))

    lines = []
    for a, b in bands:
        cols = [x for x in range(x0, x1)
                if any(ink(x, y) for y in range(a, b + 1))]
        spans, c = [], []
        for x in cols:
            if c and x != c[-1] + 1:
                spans.append((c[0], c[-1]))
                c = []
            c.append(x)
        if c:
            spans.append((c[0], c[-1]))
        glyphs = []
        for s0, s1 in spans:
            ys = [y for y in range(a, b + 1)
                  if any(ink(x, y) for x in range(s0, s1 + 1))]
            glyphs.append([[(255 - px[x, y]) / 255.0 for x in range(s0, s1 + 1)]
                           for y in range(ys[0], ys[-1] + 1)])
        lines.append(glyphs)
    return lines


def _is_colon(g: list[list[float]]) -> bool:
    if len(g[0]) > 9:
        return False
    runs, inside = 0, False
    for row in g:
        on = any(v >= 0.5 for v in row)
        if on and not inside:
            runs += 1
        inside = on
    return runs == 2


def _cband_time(gray, published: datetime | None,
                header=CBAND_HEADER) -> datetime | None:
    """The scan time printed on a C-band image, in local time, or None.

    READ, because it cannot be looked up. IMD re-uploads the same bytes every
    few minutes: on 21 Sep 2026 a frame stamped 15:07:48 UTC carried a file
    time of 15:25 and then 15:30, byte for byte the same image. Trusting the
    file time would make a radar that stalled hours ago look live.

    Three layers stop a misread becoming a wrong age: each glyph must match a
    template inside its own hole-count group (_digit); the line must parse as
    HH:MM:SS with a day and year that fit the publication date; and the scan
    must come before the file that carries it.
    """
    from datetime import timedelta, timezone

    for glyphs in _text_lines(gray.load(), header):
        # "HH:MM:SS UTC / DD Mon YYYY" is 21 glyphs when nothing touches.
        if len(glyphs) != 21 or not (_is_colon(glyphs[2])
                                     and _is_colon(glyphs[5])):
            continue
        digits = [_digit(glyphs[i]) for i in (0, 1, 3, 4, 6, 7, 12, 13,
                                                17, 18, 19, 20)]
        if None in digits:
            return None
        s = "".join(digits)
        hh, mm, ss = int(s[0:2]), int(s[2:4]), int(s[4:6])
        day, year = int(s[6:8]), int(s[8:12])
        if hh > 23 or mm > 59 or ss > 59 or not 1 <= day <= 31:
            return None

        # A naive time is local; astimezone() reads it as such.
        ref = (published.astimezone(timezone.utc) if published
               else datetime.now(timezone.utc))
        month_year = [(ref.year, ref.month)]
        prev = ref.replace(day=1) - timedelta(days=1)
        month_year.append((prev.year, prev.month))
        for y, m in month_year:
            if y != year:
                continue
            try:
                scan = datetime(y, m, day, hh, mm, ss, tzinfo=timezone.utc)
            except ValueError:
                continue
            if ref - timedelta(hours=36) <= scan <= ref + timedelta(minutes=10):
                return scan.astimezone().replace(tzinfo=None)
        return None
    return None


def _km_to_px(lat: float, lon: float,
              centre=CBAND_CENTRE) -> tuple[float, float]:
    """Pixel position on the C-band image, same mapping as RadarScan.to_px."""
    cx, cy = centre
    dy = (lat - CBAND_LAT) * 111.32
    dx = (lon - CBAND_LON) * 111.32 * math.cos(math.radians((lat + CBAND_LAT) / 2))
    return cx + dx / CBAND_KM_PER_PX, cy - dy / CBAND_KM_PER_PX


def _cband_picture(im, marks, centre, map_box) -> str | None:
    """The MMR part of the image, marked, as a data: URI for the page.

    A crop rather than the whole 3045 px frame: the full image is 1.4 MB and
    most of it is Gujarat, open sea and cross-section strips. `marks` is
    [(lat, lon, label)]; the first is drawn as the home ring.
    """
    import base64
    try:
        from PIL import ImageDraw, ImageFont
        n, s, w, e = CBAND_VIEW
        x0, y0 = _km_to_px(n, w, centre)
        x1, y1 = _km_to_px(s, e, centre)
        box = (max(map_box[0], round(x0)), max(map_box[1], round(y0)),
               min(map_box[2], round(x1)), min(map_box[3], round(y1)))
        pic = im.convert("RGB").crop(box)
        d = ImageDraw.Draw(pic)
        try:
            font = ImageFont.load_default(size=20)
        except TypeError:                          # Pillow < 10.1
            font = ImageFont.load_default()
        for i, (la, lo, label) in enumerate(marks):
            x, y = _km_to_px(la, lo, centre)
            x, y = x - box[0], y - box[1]
            r = 11 if i == 0 else 5
            d.ellipse((x - r - 2, y - r - 2, x + r + 2, y + r + 2),
                      outline=(255, 255, 255), width=3)
            d.ellipse((x - r, y - r, x + r, y + r), outline=(0, 0, 0), width=3)
            if label:
                d.text((x + r + 6, y - 12), label, font=font, fill=(0, 0, 0),
                       stroke_width=3, stroke_fill=(255, 255, 255))
        buf = io.BytesIO()
        pic.save(buf, format="WEBP", quality=78, method=6)
        return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:                               # noqa: BLE001
        return None


def fetch_cband(*, quiet: bool = True, marks=()) -> RadarScan | None:
    """Download the C-band MAX-Z, decode it, and read its printed scan time."""
    try:
        from PIL import Image
    except ImportError:
        return None

    url = f"{CBAND_URL}?t={datetime.now():%Y%m%d%H%M%S}"
    published = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            published = _header_time(resp.headers.get("Last-Modified"))
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:                        # noqa: BLE001
        if not quiet:
            print(f"  ! C-band radar unavailable: {exc}")
        return None
    return decode_cband(im, published, quiet=quiet, marks=marks)


def decode_cband(im, published: datetime | None, *, quiet: bool = True,
                 marks=()) -> RadarScan | None:
    """Turn one C-band MAX-Z frame (a PIL image) into a RadarScan."""

    def give_up(why: str) -> None:
        if not quiet:
            print(f"  ! C-band radar not read ({why}) — layout may have changed")

    w, h = im.size
    # IMD crops each frame tightly, so the size wanders by a pixel or two
    # (3045 then 3044 wide on 21 Sep 2026). Everything below is located in
    # the frame rather than read from fixed coordinates.
    if (abs(w - CBAND_SIZE[0]) > 40 or abs(h - CBAND_SIZE[1]) > 40
            or im.mode != "P"):
        return give_up(f"{im.size} {im.mode}")
    pal = im.getpalette() or []
    rgb = [tuple(pal[i:i + 3]) for i in range(0, len(pal), 3)]
    idx = im.load()

    def dark(x: int, y: int) -> bool:
        return sum(rgb[idx[x, y]]) < 270

    # The crosshair through the site: the darkest row and column near where
    # the layout puts them. A dashed line measured 36% ink across the panel;
    # ordinary rows stay well under a quarter.
    ex, ey = CBAND_CENTRE
    xs = range(CBAND_MAP[0], CBAND_MAP[2])
    ys = range(CBAND_MAP[1], CBAND_MAP[3])
    n_row, cy = max((sum(1 for x in xs if dark(x, y)), y)
                    for y in range(ey - 20, ey + 21))
    n_col, cx = max((sum(1 for y in ys if dark(x, y)), x)
                    for x in range(ex - 20, ex + 21))
    if n_row < len(xs) / 4 or n_col < len(ys) / 4:
        return give_up("crosshair not found")
    centre = (cx, cy)
    ox, oy = cx - ex, cy - ey                      # shift of this frame
    map_box = (CBAND_MAP[0] + ox, CBAND_MAP[1] + oy,
               min(w, CBAND_MAP[2] + ox), min(h, CBAND_MAP[3] + oy))

    # The colour bar: the widest saturated run along a row through its blue
    # half (the middle of the bar is the white 39-40 dBZ band - no colour).
    bar_row = CBAND_BAR_Y[0] + (CBAND_BAR_Y[1] - CBAND_BAR_Y[0]) * 2 // 3 + oy
    sat = [x for x in range(w - 420, w)
           if max(rgb[idx[x, bar_row]]) - min(rgb[idx[x, bar_row]]) > 60]
    if len(sat) < 20:
        return give_up("colour bar not found")
    bar_x = (sat[0] + sat[-1]) // 2

    # Colour key, as the LOWER edge of each band. Anchored on the white band,
    # which is 39-40 dBZ: its top edge sits on the 40 tick. The swatch at each
    # end also fills the arrow beyond the scale, so both are clamped.
    runs: list[list] = []
    for y in range(CBAND_BAR_Y[0] + oy - 30, min(h, CBAND_BAR_Y[1] + oy + 30)):
        c = rgb[idx[bar_x, y]]
        if runs and runs[-1][2] == c:
            runs[-1][1] = y
        else:
            runs.append([y, y, c])
    runs = [r for r in runs if r[1] - r[0] >= 20 and sum(r[2]) >= 30]
    # The white BAND sits between two coloured ones; paper above or below
    # the bar is white too, and is never flanked like that.
    anchor = next((runs[k] for k in range(1, len(runs) - 1)
                   if runs[k][2] == CBAND_WHITE
                   and runs[k - 1][2] != CBAND_WHITE
                   and runs[k + 1][2] != CBAND_WHITE), None)
    if anchor is None:
        return give_up("white 39-40 dBZ band not found")
    key: dict[tuple[int, int, int], float] = {}
    for y0, y1, c in runs:
        v = 39.0 + (anchor[1] - y1) / CBAND_BAR_PX_PER_DBZ
        key[c] = round(min(58.0, max(20.0, v)), 1)
    if len(key) < 30:
        return give_up(f"colour key has {len(key)} bands")

    # Each palette entry -> dBZ by its NEAREST bar colour. Exact matching is
    # not enough: the echo is drawn lightly blended with the terrain beneath,
    # so much of it sits a little off the bar's own colours. The tolerance
    # keeps every bar colour and its blends while leaving out the nearest
    # background (light sea greys, 28 and more away; measured 21 Sep 2026).
    bar = [(c, v) for c, v in key.items()]
    by_index: dict[int, float] = {}
    whiteish: set[int] = set()
    for i, c in enumerate(rgb):
        d, c_near, v = min((math.dist(c, bc), bc, bv) for bc, bv in bar)
        if d > CBAND_COLOUR_TOL:
            continue
        if c_near == CBAND_WHITE:
            whiteish.add(i)            # also the label boxes - see below
        else:
            by_index[i] = v

    x0, y0, x1, y1 = map_box
    px_dbz: dict[tuple[int, int], float] = {}
    whites: list[tuple[int, int]] = []
    pw = x1 - x0
    for n, i in enumerate(im.crop(map_box).getdata()):
        if i in whiteish:
            whites.append((x0 + n % pw, y0 + n // pw))
        else:
            v = by_index.get(i)
            if v is not None and v >= MIN_RAIN_DBZ:
                px_dbz[(x0 + n % pw, y0 + n // pw)] = v
    white_dbz = key.get(CBAND_WHITE)
    if white_dbz is not None:
        for x, y in whites:
            around = sum(1 for dx in range(-2, 3) for dy in range(-2, 3)
                         if (x + dx, y + dy) in px_dbz)
            if around >= 3:
                px_dbz[(x, y)] = white_dbz

    # Summarise on ~1.1 km squares: each reports the reflectivity that at
    # least HALF of it reaches. At 0.28 km a pixel is finer than the radar's
    # own beam, and a maximum over a 16 km circle - ten thousand pixels - kept
    # landing on five-pixel flecks of 50 dBZ inside 33 dBZ rain. A real core
    # a kilometre or more across survives this; a fleck does not.
    cells: dict[tuple[int, int], list[float]] = {}
    for (x, y), v in px_dbz.items():
        cells.setdefault((x // CBAND_BLOCK, y // CBAND_BLOCK), []).append(v)
    half = (CBAND_BLOCK * CBAND_BLOCK + 1) // 2
    blocks = {k: sorted(vs, reverse=True)[half - 1]
              for k, vs in cells.items() if len(vs) >= half}
    # Despeckle: a square counts only when at least CBAND_SUPPORT of the nine
    # around it (itself included) carry echo too. Rain fills the ground around
    # it; clutter off hills and towers is grainy - on 22 Sep 2026 the Vashi
    # box was 15% echo, a third of it at 40-45 dBZ, and read as heavy rain.
    dbz = {(bx, by): v for (bx, by), v in blocks.items()
           if sum((bx + i, by + j) in blocks
                  for i in (-1, 0, 1) for j in (-1, 0, 1)) >= CBAND_SUPPORT}

    header = (w - (CBAND_SIZE[0] - CBAND_HEADER[0]), 0, w, CBAND_HEADER[3])
    scanned = _cband_time(im.convert("L"), published, header)
    if scanned is None and not quiet:
        print("  ! C-band scan time could not be read off the image")

    legend = tuple(sorted((v, "#%02x%02x%02x" % c) for c, v in key.items()))
    return RadarScan(
        # dbz is on the ~1.1 km squares, so the geometry is too: a square's
        # index is its pixels' index divided by the block, centre included.
        product="maxz", width=w, height=h,
        centre_px=((cx - (CBAND_BLOCK - 1) / 2) / CBAND_BLOCK,
                   (cy - (CBAND_BLOCK - 1) / 2) / CBAND_BLOCK),
        scanned_at=scanned, dbz=dbz, site=CBAND_SITE,
        site_lat=CBAND_LAT, site_lon=CBAND_LON,
        km_per_px=CBAND_KM_PER_PX * CBAND_BLOCK,
        time_exact=scanned is not None, published_at=published,
        image=_cband_picture(im, marks, centre, map_box), legend=legend,
    )


def effective_age(scan: RadarScan | None) -> float | None:
    """Minutes since the scan, padded when only the upload time is known."""
    if scan is None or scan.age_minutes is None:
        return None
    return scan.age_minutes + (0.0 if scan.time_exact
                               else PUBLISH_LAG_ALLOWANCE_MIN)


def fresh(scan: RadarScan | None) -> bool:
    age = effective_age(scan)
    return age is not None and age <= STALE_MINUTES


# The area both dishes cover well, for comparing how much each one sees.
COMMON_CENTRE = (19.02, 72.84)
COMMON_RADIUS_KM = 120.0
# Compared at 25 dBZ and up, which both colour keys resolve; the S-band's key
# starts at 22.9 and the C-band's at 20, so weaker echo is not comparable.
COMMON_MIN_DBZ = 25.0
# Below this share of the lead radar's echo area, the other one is not seeing
# the same sky and cannot be used to doubt it. On 22 Sep 2026 at 11:20 the
# C-band had an organised rain band over the sea WSW of the city and Colaba's
# S-band showed one speck in 250 km; at 18:00 the same day Colaba saw 31% of
# the C-band's echo and missed most of a Ghat storm complex - enough echo to
# pass a 15% bar, not enough to be trusted to say "nothing here".
MIN_CHECK_SHARE = 0.5
# Convective cells change completely in half an hour, so two scans further
# apart than this cannot confirm or doubt each other at all.
CHECK_MAX_GAP_MIN = 15.0
# Where a scan time is only an upload time (Colaba), the scan is taken to be
# this much earlier: the two gaps measured on 21-22 Sep 2026 were 11.5 and 16
# minutes.
UPLOAD_LAG_EST_MIN = 14.0
# ...unless the lead radar itself has little echo, when a small patch the
# other cannot see is exactly the case the check exists for.
CHECK_FLOOR_KM2 = 200.0


def echo_area_km2(scan: RadarScan) -> float:
    """Area of echo at COMMON_MIN_DBZ or more within the shared circle."""
    cx, cy = scan.centre_px
    k = scan.km_per_px
    clat, clon = COMMON_CENTRE
    oy = (scan.site_lat - clat) * 111.32
    ox = (scan.site_lon - clon) * 111.32 * math.cos(math.radians(clat))
    r2 = COMMON_RADIUS_KM ** 2
    n = sum(1 for (x, y), v in scan.dbz.items()
            if v >= COMMON_MIN_DBZ
            and ((x - cx) * k + ox) ** 2 + ((cy - y) * k + oy) ** 2 <= r2)
    return n * k * k


def scan_time_estimate(scan: RadarScan | None) -> datetime | None:
    """The scan time, or for an upload-time-only radar, a best estimate."""
    if scan is None or scan.scanned_at is None:
        return None
    if scan.time_exact:
        return scan.scanned_at
    from datetime import timedelta
    return scan.scanned_at - timedelta(minutes=UPLOAD_LAG_EST_MIN)


def can_check(scan: RadarScan, other: RadarScan | None) -> tuple[bool, str]:
    """Whether `other` is fit to confirm or doubt `scan` this run."""
    if not fresh(other):
        return False, ""
    t1, t2 = scan_time_estimate(scan), scan_time_estimate(other)
    if t1 is not None and t2 is not None:
        gap = abs((t1 - t2).total_seconds()) / 60.0
        if gap > CHECK_MAX_GAP_MIN:
            return False, (
                f"The two Mumbai radars' latest scans are about {gap:.0f} "
                f"minutes apart, too far for one to confirm or doubt the other "
                f"— showers form and die in less time than that.")
    lead, second = echo_area_km2(scan), echo_area_km2(other)
    if lead >= CHECK_FLOOR_KM2 and second < MIN_CHECK_SHARE * lead:
        return False, (
            f"The two Mumbai radars disagree widely on this scan: the "
            f"{scan.short_name} shows about {lead:,.0f} km² of rain echo "
            f"within {COMMON_RADIUS_KM:.0f} km of the city, the "
            f"{other.short_name} about {second:,.0f} km². One of them is not "
            f"seeing properly, so neither is used to confirm the other here "
            f"— compare the two on IMD's radar page before relying on either.")
    return True, ""


def fetch_best(*, quiet: bool = True,
               marks=()) -> tuple[RadarScan | None, RadarScan | None]:
    """(the radar to read "now" from, the other radar).

    Both Mumbai radars are fetched every run. The C-band leads when its
    printed time can be read and is recent; otherwise Colaba's S-band does.
    The other one is kept as a SECOND OPINION: ground clutter depends on where
    a radar stands and which hills its beam grazes, while rain shows on both,
    so an echo only one of them sees is doubtful (see belt_check). On 21 Sep
    2026 the C-band carried a bright, stationary ~1 km spot on the Sanjay
    Gandhi National Park hills, 10 km north of the dish.
    """
    c = fetch_cband(quiet=quiet, marks=marks)
    s = fetch("maxz", quiet=quiet)
    lead, other = _choose(c, s, quiet=quiet)
    if lead is not None:
        lead.check_ok, lead.check_note = can_check(lead, other)
        if lead.check_note and not quiet:
            print(f"  ! {lead.check_note}")
    return lead, other


def _choose(c: RadarScan | None, s: RadarScan | None, *,
            quiet: bool) -> tuple[RadarScan | None, RadarScan | None]:
    if c is not None and c.time_exact and fresh(c):
        return c, s
    if fresh(s):
        if c is None:
            s.note = ("The C-band radar could not be read this run, so the "
                      "present is read from Colaba's S-band.")
        elif not c.time_exact:
            s.note = ("The C-band image's scan time could not be read, so its "
                      "age is unknown and the present is read from Colaba's "
                      "S-band instead.")
        else:
            s.note = (f"The C-band's latest scan is from "
                      f"{c.scanned_at:%H:%M}, {c.age_minutes:.0f} minutes old, "
                      "so the present is read from Colaba's S-band instead.")
        if not quiet:
            print(f"  ! {s.note}")
        return s, c
    return (c, s) if c is not None else (s, None)


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


def belt_reading(scan: RadarScan,
                 belt) -> tuple[float, tuple[float, float] | None]:
    """Heaviest rate over a belt's points, and the heaviest echo near them.

    Returns (mm_h overhead, (mm_h, km) of the nearest-best echo or None).
    """
    here_max = 0.0
    near_best: tuple[float, float] | None = None   # (mm_h, km)
    for _nm, la, lo in belt.points:
        r = scan.sample(la, lo)
        here_max = max(here_max, r["mm_h_here"])
        if r["dbz_near"] is not None:
            cand = (r["mm_h_near"], r["near_km"] or 0.0)
            if near_best is None or cand[0] > near_best[0]:
                near_best = cand
    return here_max, near_best


# Older than this, a scan no longer describes "now" well enough to lead the
# page with. A convective cell can form and rain out inside three quarters of
# an hour.
STALE_MINUTES = 45.0

# One radar at moderate or heavier while the other sees nothing at all over
# the same place is not a difference of calibration; one of them is wrong.
DOUBT_MM_H = 7.5


def belt_check(scan: RadarScan, other: RadarScan | None, belt) -> dict:
    """A belt's reading from the lead radar, checked against the other one.

    `doubt` is "clutter" when only the lead radar sees a strong echo - hills
    near one dish are invisible to the other - and "blocked" when only the
    other one does, which for the C-band is the signature of its beam being
    weakened by heavy rain in between.
    """
    here, near = belt_reading(scan, belt)
    second = (belt_reading(other, belt)[0]
              if fresh(other) and scan.check_ok else None)
    doubt = ""
    if second is not None:
        if here >= DOUBT_MM_H and second < 0.5:
            doubt = "clutter"
        elif second >= DOUBT_MM_H and here < 0.5:
            doubt = "blocked"
    return {"here": here, "near": near, "second": second, "doubt": doubt}


def when_phrase(scan: RadarScan) -> str:
    """'scanned 20:45' when the time was read off the image; 'published' when
    it is only IMD's upload time, which runs ten to twenty-five minutes late."""
    if scan.scanned_at is None:
        return "of unknown time"
    verb = "scanned" if scan.time_exact else "published"
    return f"{verb} {scan.scanned_at:%H:%M}"


def _names(belts) -> str:
    shown = [b.name.removesuffix(" belt") for b in belts][:3]
    return (", ".join(shown[:-1]) + " and " + shown[-1]
            if len(shown) > 1 else shown[0])


def now_clause(scan: RadarScan | None, home, belts,
               other: RadarScan | None = None) -> str | None:
    """The radar's half of the top-of-page line: what the beam sees over home.

    None when there is no usable scan, so the caller falls back to the model.
    """
    if scan is None or home is None or not fresh(scan):
        return None
    src = f"the {scan.short_name} radar ({when_phrase(scan)})"

    chk = belt_check(scan, other, home)
    here, near = chk["here"], chk["near"]
    if chk["doubt"] == "clutter":
        return (f"{src} shows {now_band(here)} over the belt, but the other "
                "Mumbai radar sees nothing there — possibly hill clutter, so "
                "treat it as unconfirmed.")
    if chk["doubt"] == "blocked":
        return (f"{src} shows nothing here, but the other Mumbai radar has "
                f"{now_band(chk['second'])} — heavy rain in between may be "
                "hiding it from this one.")
    if here >= 0.5:
        return (f"{src} shows **{now_band(here)}** falling here now "
                f"(~{here:.0f} mm/hr).")

    first = f"nothing falling here on {src}"
    if near and near[0] >= 2.5 and near[1] > 3:
        return f"{first}, but {now_band(near[0])} is ~{near[1]:.0f} km away."

    elsewhere = []
    for b in belts:
        if b is home:
            continue
        c = belt_check(scan, other, b)
        if c["here"] >= 0.5 and c["doubt"] != "clutter":
            elsewhere.append(b)
    if elsewhere:
        return f"{first}; rain is falling over {_names(elsewhere)}."
    return f"{first}, and none anywhere else in the MMR."


def render_now(scan: RadarScan | None, belts,
               other: RadarScan | None = None) -> str:
    """The observed-conditions section: what radar has, belt by belt.

    This sits ABOVE the model belt table and outranks it for the present
    tense. The models answer how much over a day; only this answers whether
    it is raining on you right now.
    """
    if scan is None:
        return ""

    age = scan.age_minutes
    if age is None:
        age_txt = "**of unknown age — treat it with care**"
    elif scan.time_exact:
        age_txt = f"**{age:.0f} minute{'' if 0.5 <= age < 1.5 else 's'} old**"
    else:
        age_txt = (f"uploaded {age:.0f} minutes ago — the scan itself is "
                   "older, by ten to twenty-five minutes on the frames checked")

    out = "**What the radar can actually see right now**\n\n"
    out += (
        f"This is observation, not forecast. It reads IMD's **{scan.site}**, "
        f"{when_phrase(scan)}, {age_txt}. Everything else on this page is a "
        "model's opinion about the day; this is the beam reporting what is in "
        "the air over each place at that moment, so where the two disagree "
        "about **right now**, believe this one.\n\n"
    )
    if scan.note:
        out += f"> {scan.note}\n\n"

    has_second = fresh(other) and scan.check_ok
    if scan.check_note:
        out += f"> {scan.check_note}\n\n"
    rows: list[str] = []
    wet_names: list[str] = []
    for belt in belts:
        chk = belt_check(scan, other, belt)
        here_max, near_best = chk["here"], chk["near"]

        if here_max >= 0.5:
            if chk["doubt"] != "clutter":
                wet_names.append(belt.name)
            state = f"**{now_band(here_max).capitalize()}** (~{here_max:.0f} mm/hr)"
        elif here_max > 0:
            state = "Just detectable — a few spots at most"
        else:
            state = "Nothing overhead"
        if chk["doubt"] == "clutter":
            state += (" — *unconfirmed: the other radar sees nothing here, "
                      "possibly hill clutter*")
        elif chk["doubt"] == "blocked":
            state += (" — *the other radar has rain here; this beam may be "
                      "weakened by heavy rain in between*")

        if near_best and near_best[0] >= 2.5 and near_best[1] > 3:
            nearby = (f"{now_band(near_best[0])} ~{near_best[1]:.0f} km away")
        elif near_best and near_best[0] > 0:
            nearby = "only light echo nearby"
        else:
            nearby = "clear for 16 km around"
        row = f"| **{belt.name}** | {state} | {nearby} |"
        if has_second:
            sec = chk["second"]
            row += (f" {now_band(sec)} |" if sec is not None and sec >= 0.5
                    else " nothing |")
        rows.append(row)

    if wet_names:
        out += ("Rain is falling now over **" + ", ".join(wet_names)
                + "**.\n\n")
    else:
        out += "**No rain is reaching the ground anywhere in the MMR** on this scan.\n\n"

    head, rule = "| Area | Overhead now | Nearest echo |", "|---|---|---|"
    if has_second:
        head += f" {other.short_name} check |"
        rule += "---|"
    out += head + "\n" + rule + "\n" + "\n".join(rows) + "\n\n"

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
        "looks, or missed entirely underneath the beam."
    )
    if scan.short_name == "C-band":
        out += (
            " The C-band reads each ~1 km square at the level at least half "
            "of it reaches, so a single bright speck cannot set a belt's "
            "reading. Its known weakness is heavy rain: the beam loses power "
            "passing through it, so rain **behind** a heavy core, seen from "
            "Andheri, can read weaker than it is — the check column is the "
            "other radar's view of the same place."
        )
    out += "\n"
    return out
