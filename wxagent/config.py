"""
Configuration for the Kalyan/MMR weather forecasting agent.

Every constant here traces to a specific passage in the two source guides,
which in turn derive from Oxford Aviation Academy ATPL Book 9: Meteorology.
Citations are given as (Guide = Practical Rainfall Forecasting;
Handbook = The Weather Interpreter's Handbook).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
FORECAST_DIR = ROOT / "forecasts"
LOG_DIR = ROOT / "logs"
CACHE_DIR = ROOT / ".cache"
FORECAST_LOG = LOG_DIR / "forecast_log.csv"

for _d in (FORECAST_DIR, LOG_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

TIMEZONE = "Asia/Kolkata"


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Site:
    key: str
    name: str
    lat: float
    lon: float
    zone: str          # coastal | transition | ghat | leeward - RAINFALL role
    elevation_m: int
    note: str = ""
    # IMD station category for the HEAT/COLD thresholds of Handbook Ch.18/19:
    # coastal | plains | hilly. Deliberately a separate field from `zone`,
    # because the two answer different questions. Karjat sits in the rainfall
    # "transition" belt but is 75 km inland with no sea-breeze protection, so
    # it is judged against the 40C plains threshold, not the 37C coastal one.
    # Inferring this from `zone` put Karjat on the coastal threshold and would
    # have under-called heat there every pre-monsoon.
    imd_type: str = "coastal"


# The home location - the daily alert is written for this point.
HOME = Site(
    key="kalyan_west",
    name="Kalyan West",
    lat=19.2437,
    lon=73.1305,
    zone="transition",
    elevation_m=10,
    note=(
        "Thane-Kalyan-Karjat belt. Guide s11: 'Highly variable; can receive "
        "coastal bands plus terrain enhancement' - transition from coast to "
        "foothills with local channeling."
    ),
)

# The four-location laboratory of Guide s28.1, extended to cover the MMR
# properly. Sea -> coast -> transition -> ghat crest -> rain shadow.
MMR_SITES: tuple[Site, ...] = (
    Site("arabian_sea", "Arabian Sea (offshore W of Mumbai)", 19.05, 72.30,
         "coastal", 0, "Upstream moisture/organisation check - Guide s11."),
    Site("colaba", "Mumbai - Colaba", 18.9067, 72.8147, "coastal", 11,
         "IMD reference station, south Mumbai."),
    Site("santacruz", "Mumbai - Santacruz", 19.0896, 72.8656, "coastal", 14,
         "IMD reference station, suburban Mumbai."),
    Site("vasai_virar", "Vasai-Virar", 19.3919, 72.8397, "coastal", 9,
         "Northern MMR coastal belt."),
    Site("borivali", "Mumbai - Borivali", 19.2307, 72.8567, "coastal", 12,
         "Northern western suburbs, in the lee of Sanjay Gandhi NP."),
    Site("chembur", "Mumbai - Chembur", 19.0522, 72.9005, "coastal", 15,
         "Eastern suburbs / harbour side."),
    Site("thane", "Thane", 19.2183, 72.9781, "transition", 11,
         "Creek belt, urban heat + coastal convergence."),
    Site("bhiwandi", "Bhiwandi", 19.2813, 73.0483, "transition", 15,
         "Warehousing belt just west of Kalyan - logistics-relevant."),
    Site("kalyan_west", "Kalyan West", 19.2437, 73.1305, "transition", 10,
         "HOME. Coast-to-foothill transition."),
    Site("dombivli", "Dombivli", 19.2183, 73.0868, "transition", 10,
         "Kalyan-Dombivli twin township."),
    Site("panvel", "Navi Mumbai - Panvel", 18.9894, 73.1175, "transition", 12,
         "Southern MMR, Matheran foothill approach."),
    Site("alibag", "Alibag", 18.6411, 72.8722, "coastal", 5,
         "Raigad coast, southern edge of the MMR. Nisarga landfall zone."),
    Site("palghar", "Palghar", 19.6967, 72.7656, "coastal", 10,
         "Northern MMR district."),
    Site("badlapur", "Badlapur", 19.1552, 73.2650, "transition", 30,
         "Ulhas valley, immediate foothill approach.", imd_type="plains"),
    Site("karjat", "Karjat", 18.9107, 73.3233, "transition", 41,
         "Foothill belt at the base of the Bhor Ghat approach. ~75 km inland - "
         "outside the sea breeze's reach, so judged on plains thresholds.",
         imd_type="plains"),
    Site("igatpuri", "Igatpuri", 19.6967, 73.5620, "ghat", 600,
         "Thal Ghat, the NORTHERN crest. Routinely among the wettest places "
         "in Maharashtra - the pass carrying the Mumbai-Nashik route sits "
         "square-on to the monsoon westerly.", imd_type="hilly"),
    Site("matheran", "Matheran", 18.9866, 73.2707, "ghat", 800,
         "Windward Ghat crest - cloud immersion reference. Guide s11.",
         imd_type="hilly"),
    Site("malshej", "Malshej Ghat", 19.3333, 73.7667, "ghat", 700,
         "The pass on the Kalyan-Ahmednagar route. Furthest inland of the crest "
         "sites - first to lose out when the moist layer is shallow.",
         imd_type="hilly"),
    Site("lonavala", "Lonavala", 18.7546, 73.4062, "ghat", 622,
         "Bhor Ghat pass. Handbook Ch.14 - a gap in the Ghat wall.",
         imd_type="hilly"),
    Site("pune", "Pune", 18.5204, 73.8567, "leeward", 560,
         "Rain shadow control. Handbook Ch.14/16.", imd_type="plains"),
)

SITES_BY_KEY = {s.key: s for s in MMR_SITES}

# Sites that make up the headline "MMR" weekly product.
MMR_CORE_KEYS = (
    "colaba", "santacruz", "vasai_virar", "thane",
    "kalyan_west", "panvel", "badlapur", "karjat",
)


# --------------------------------------------------------------------------
# MMR areas
# --------------------------------------------------------------------------
# Grouped and named the way people here actually refer to them, so a forecast
# reads as "Thane-Kalyan-Dombivli belt" rather than a list of grid points.
# Rain across the MMR is not uniform - Guide s11 puts Kalyan in a transition
# belt that behaves differently from both the island city and the Ghat face -
# so the weekly product forecasts per area rather than one blanket number.

@dataclass(frozen=True)
class Area:
    key: str
    name: str
    sites: tuple[str, ...]
    character: str


MMR_AREAS: tuple[Area, ...] = (
    Area("island_city", "Mumbai island city", ("colaba",),
         "Colaba to Dadar. Most sea-moderated part of the region — least "
         "temperature swing, and usually a touch less rain than the suburbs."),
    Area("suburbs", "Mumbai suburbs", ("santacruz", "borivali", "chembur"),
         "Bandra to Dahisar and the eastern suburbs. Santacruz is the IMD "
         "reference gauge most headline Mumbai rainfall figures come from."),
    Area("thane_belt", "Thane & Bhiwandi", ("thane", "bhiwandi"),
         "Creek belt. Urban heat plus coastal convergence; Bhiwandi matters "
         "for warehousing and transport decisions."),
    Area("kalyan_belt", "Kalyan–Dombivli", ("kalyan_west", "dombivli"),
         "HOME belt. Guide s11's Thane–Kalyan–Karjat transition — picks up "
         "coastal bands AND terrain enhancement, so it often runs between the "
         "suburbs and the Ghats rather than tracking either."),
    Area("navi_mumbai", "Navi Mumbai & Panvel", ("panvel",),
         "Harbour side and the approach to the Matheran foothills."),
    Area("badlapur_belt", "Ambernath–Badlapur–Karjat", ("badlapur", "karjat"),
         "Ulhas valley into the foothills. First to feel Ghat enhancement, and "
         "the belt where pre-monsoon storms most often fire."),
    Area("north_mmr", "Vasai–Virar & Palghar", ("vasai_virar", "palghar"),
         "Northern MMR. Frequently the wettest coastal stretch when the "
         "monsoon current is aimed slightly north of the city."),
    Area("south_mmr", "Alibag & Raigad coast", ("alibag",),
         "Southern edge of the region. Nisarga's 2020 landfall zone — the "
         "stretch most exposed in an Arabian Sea cyclone."),
    # The crest is split three ways rather than averaged. A single "Ghats" row
    # hides the thing that matters most about them: the same westerly does very
    # different work at the northern passes, the central crest and the eastern
    # gap, and which one is wettest tells you where the current is really aimed.
    Area("ghats_north", "Igatpuri (northern Ghats)", ("igatpuri",),
         "Thal Ghat. Squarely in the path of the monsoon westerly and among "
         "the wettest places in the state — when the current is aimed slightly "
         "north, this is where it lands hardest."),
    Area("ghats", "Matheran–Lonavala (central Ghats)", ("matheran", "lonavala"),
         "The central crest and the Bhor Ghat gap. Where the orographic engine "
         "does its classic work; trekking and hill-station calls hang here."),
    Area("ghats_east", "Malshej", ("malshej",),
         "Furthest inland of the crest sites. Needs a deeper moist layer to get "
         "properly wet, so it is the first of the three to fall away when the "
         "moisture is shallow — a useful read on depth, not just strength."),
)

AREAS_BY_KEY = {a.key: a for a in MMR_AREAS}

# The home area, used by the daily product for neighbourhood context.
HOME_AREA = "kalyan_belt"


# --------------------------------------------------------------------------
# Synoptic sampling grids
# --------------------------------------------------------------------------
# Handbook Ch.13 / Guide s12: the monsoon trough position and the offshore
# trough are the two pressure features that set the week's character. Neither
# is available from a single point, so we sample a coarse grid of MSL pressure
# and diagnose the features from the field.

# North-south transect used to locate the monsoon trough axis.
# Handbook Ch.13: trough hugging the Himalayan foothills => BREAK;
# trough back over the Gangetic plain => ACTIVE.
#
# Sampled at 1 degree. Coarser spacing makes the diagnosed axis jump in large
# steps and read as artificially static across a week, which understates real
# movement of the trough - the very thing Ch.13 says to watch.
MONSOON_TROUGH_TRANSECT = tuple(
    (20.0 + i, 80.0) for i in range(13)          # 20N .. 32N
)

# North-south line just off the Konkan coast, used to detect the offshore
# trough (Guide s12.1, Handbook Ch.13): an elongated low running roughly
# Goa -> south Gujarat, parallel to the coast.
OFFSHORE_TROUGH_LINE = tuple(
    (lat, 72.2) for lat in (15.0, 16.5, 18.0, 19.5, 21.0, 22.5)
)

# Inland comparison line at the same latitudes, to measure the cross-coast
# pressure gradient that drives the onshore flow (Guide s2.3).
INLAND_REFERENCE_LINE = tuple(
    (lat, 75.5) for lat in (15.0, 16.5, 18.0, 19.5, 21.0, 22.5)
)


# --------------------------------------------------------------------------
# Terrain geometry
# --------------------------------------------------------------------------
# Guide s7.1 / s11.1: "A west or west-southwest wind aimed directly at the
# Ghats maximises upslope lifting... a wind nearly parallel to the mountain
# chain produces less direct lift."
#
# The Sahyadri crest behind Mumbai runs approximately N-S (ridge axis ~350/170
# degrees). The upslope-normal is therefore a wind blowing FROM about 260 deg
# (WSW-W). We score orographic forcing as the component of the wind along this
# bearing.
GHAT_UPSLOPE_NORMAL_DEG = 260.0

# Pressure level treated as the steering/moisture-transport layer.
# Guide s10.1: "On Windy, 850 hPa wind is a useful first approximation of this
# current, while 925 hPa shows the very-low-level flow."
STEERING_LEVEL = "850hPa"
LOW_LEVEL = "925hPa"
MID_LEVEL = "700hPa"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
# Handbook Ch.21 names exactly these three. ECMWF is the default highest-trust
# read; GFS is the more frequently updated second opinion; ICON is the
# tie-breaker.

@dataclass(frozen=True)
class Model:
    key: str            # Open-Meteo model id
    label: str
    trust: float        # weight used ONLY for occurrence confidence, never
                        # for mechanically averaging rainfall totals.
    note: str


MODELS: tuple[Model, ...] = (
    Model("ecmwf_ifs025", "ECMWF", 1.00,
          "Handbook Ch.21: best global model overall, specifically strong for "
          "precipitation and cloud. Default read 2-7 days out."),
    Model("gfs_seamless", "GFS", 0.85,
          "Handbook Ch.21: updates 4x daily, catches fast-evolving situations "
          "sooner, but historically trails ECMWF on precipitation."),
    Model("icon_seamless", "ICON", 0.90,
          "Handbook Ch.21: strong competitive third model, used as a genuine "
          "tie-breaker rather than just a second opinion."),
)

# Ensemble used for true probability of exceedance. Guide s13.2: "If most
# ensemble members support rain, confidence in rain occurrence is higher."
ENSEMBLE_MODEL = "gfs025"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Windy Point Forecast API - a supplementary cross-check only.
#
# Free-tier Windy serves GFS alone over India, so it cannot drive the three-way
# ECMWF/GFS/ICON comparison of Handbook Ch.21; that comes from Open-Meteo. What
# it does give is the exact numbers windy.com itself would display for a point,
# which is useful when reconciling a bulletin against what you see on the map.
#
# Resolution order: environment variable, then local_config.py (untracked).
WINDY_API_KEY = os.environ.get("WINDY_API_KEY", "").strip()
if not WINDY_API_KEY:
    try:
        from .local_config import WINDY_API_KEY as _LOCAL_KEY
        WINDY_API_KEY = _LOCAL_KEY.strip()
    except ImportError:
        WINDY_API_KEY = ""

WINDY_POINT_URL = "https://api.windy.com/api/point-forecast/v2"

# Windy MAP Forecast API - a browser-side embed, not a data source. It returns
# no numbers to the agent; it renders the interactive Windy map inside a page.
# Needs two external scripts, so it cannot work anywhere a CSP blocks external
# hosts (the published artifact); the locally-served pages are fine.
WINDY_MAP_KEY = os.environ.get("WINDY_MAP_KEY", "").strip()
if not WINDY_MAP_KEY:
    try:
        from .local_config import WINDY_MAP_KEY as _MAP_KEY
        WINDY_MAP_KEY = _MAP_KEY.strip()
    except ImportError:
        WINDY_MAP_KEY = ""

# Windy's free tier is non-commercial. Guide s29.5: review Windy and
# data-provider licensing before redistributing screenshots, data or API
# outputs commercially.
WINDY_FREE_TIER_DAILY_LIMIT = 500


# --------------------------------------------------------------------------
# IMD rainfall categories (Handbook Ch.8, 24-hour totals in mm)
# --------------------------------------------------------------------------

IMD_BANDS: tuple[tuple[float, float, str, str], ...] = (
    (0.0,    2.5,   "No / trace rain",     "Nothing to plan around."),
    (2.5,    15.6,  "Light rain",          "Umbrella weather, no disruption expected."),
    (15.6,   64.5,  "Moderate rain",       "Steady accumulation, minor waterlogging in poor-drainage areas."),
    (64.5,   124.5, "Heavy rain",          "Genuine disruption likely - waterlogging, slowed traffic. IMD Orange territory."),
    (124.5,  244.5, "Very heavy rain",     "Serious flooding risk, especially near high tide. IMD Red territory."),
    (244.5,  1e9,   "Extremely heavy rain","Rare, dangerous, historic-flood-level totals."),
)

# Threshold used for the binary rain / no-rain forecast that gets verified.
# Guide s26: the event must be defined before the forecast is scored.
MEASURABLE_RAIN_MM = 2.5          # over the 24h valid period
HEAVY_SPELL_MM_PER_H = 12.0       # an "intense burst" hourly rate
HEAVY_DAY_MM = 64.5               # IMD heavy-rain threshold


# --------------------------------------------------------------------------
# CAPE bands (Handbook Ch.24)
# --------------------------------------------------------------------------

CAPE_BANDS: tuple[tuple[float, float, str, str], ...] = (
    (0,    500,  "Fairly stable",
     "Ordinary thunderstorm development unlikely without an unusually strong trigger."),
    (500,  1500, "Moderately unstable",
     "Garden-variety afternoon thunderstorms plausible, given a clear trigger."),
    (1500, 2500, "Strongly unstable",
     "Vigorous storms plausible - heavy rain, gusty winds, hail possible."),
    (2500, 1e9,  "Extreme instability",
     "Severe, potentially organised storms possible if a trigger fires."),
)


# --------------------------------------------------------------------------
# Moisture-depth thresholds (Guide s4.4)
# --------------------------------------------------------------------------
# "If the air is humid near the surface but dry at 700 hPa, clouds may remain
# shallow or entrain dry air and weaken. If humidity is high through 700 hPa,
# widespread and persistent rain becomes more plausible."

RH_DEEP_700 = 70.0        # >= this at 700hPa => deep moisture
RH_MODERATE_700 = 50.0    # >= this => moderate depth
RH_MOIST_850 = 75.0       # 850hPa considered moist at/above this

# Precipitable water (kg/m2 == mm). Peak-monsoon Konkan values run 55-70mm.
PWAT_HIGH = 55.0
PWAT_VERY_HIGH = 65.0


# --------------------------------------------------------------------------
# Dew point interpretation (Handbook Ch.3)
# --------------------------------------------------------------------------

DEWPOINT_BANDS: tuple[tuple[float, float, str], ...] = (
    (-99, 15.0, "Comfortable, dry-feeling air"),
    (15.0, 20.0, "Pleasant"),
    (20.0, 24.0, "Noticeably humid"),
    (24.0, 26.0, "Muggy, oppressive - classic peak-monsoon Mumbai air"),
    (26.0, 99.0, "Extremely humid - any lifting mechanism will readily produce heavy rain"),
)

# Guide s4.3: spread below this means the air is close to saturation.
NEAR_SATURATION_SPREAD = 3.0


# --------------------------------------------------------------------------
# Orographic forcing thresholds (m/s of terrain-normal 850hPa wind)
# --------------------------------------------------------------------------
# Handbook Ch.22 Step 4: "A steady 15-20 knot onshore (westerly-to-
# southwesterly) flow at both levels confirms the orographic engine is loaded."
# 15 kt ~ 7.7 m/s, 20 kt ~ 10.3 m/s.

OROG_WEAK = 4.0
OROG_MODERATE = 7.7
OROG_STRONG = 10.3
OROG_VERY_STRONG = 14.0


# --------------------------------------------------------------------------
# Shear thresholds (m/s, 925hPa -> 500hPa vector difference) - Handbook Ch.24
# --------------------------------------------------------------------------

SHEAR_LOW = 8.0        # below => pulse storms, collapse on own outflow
SHEAR_MODERATE = 15.0  # above => organised / longer-lived / squall lines


# --------------------------------------------------------------------------
# Seasons (Handbook Ch.22 Step 1) - month -> regime
# --------------------------------------------------------------------------

SEASONS = {
    1: "winter", 2: "winter",
    3: "pre_monsoon", 4: "pre_monsoon", 5: "pre_monsoon",
    6: "monsoon", 7: "monsoon", 8: "monsoon", 9: "monsoon",
    10: "post_monsoon",
    11: "winter", 12: "winter",
}

SEASON_LABELS = {
    "winter": "Winter / dry season",
    "pre_monsoon": "Pre-monsoon",
    "monsoon": "Southwest monsoon",
    "post_monsoon": "Post-monsoon transition",
}

SEASON_FRAMEWORK = {
    "monsoon": (
        "Monsoon mechanisms are the primary framework: monsoon trough position, "
        "offshore trough, onshore 850hPa flow and orographic lift against the "
        "Ghats. Convective triggers are secondary."
    ),
    "pre_monsoon": (
        "Conditional instability plus a trigger is the primary framework: "
        "sea-breeze convergence, foothill heating, CAPE and shear. Classic "
        "thunderstorm and hail season for the interior."
    ),
    "post_monsoon": (
        "Transitional. Retreating southwest flow can still deliver meaningful "
        "Konkan rain into early-to-mid October, and this is one of the two "
        "Arabian Sea cyclone windows."
    ),
    "winter": (
        "Largely dry. Watch for western disturbances aloft, inversions, haze "
        "and fog rather than rain. Rain here is unusual and worth explaining."
    ),
}

# Arabian Sea cyclone watch windows (Handbook Ch.17).
CYCLONE_WATCH_MONTHS = {4, 5, 6, 10, 11, 12}


# --------------------------------------------------------------------------
# Confidence by lead time (Handbook Ch.27, Guide s18)
# --------------------------------------------------------------------------

LEAD_TIME_GUIDANCE: tuple[tuple[int, int, str, str], ...] = (
    (0, 3, "Nowcast",
     "Radar and current observations outrank model rainfall output. Give a "
     "specific timing window; update frequently."),
    (3, 12, "Short range",
     "Rain windows and likely spell character. Exact suburb still uncertain."),
    (12, 48, "Day 1-2",
     "Probability, broad timing, spatial footprint and intensity category are "
     "all defensible. Genuinely reliable in an active monsoon signal."),
    (48, 120, "Day 3-5",
     "Trend and risk window only. Broad pattern is reliable enough to plan "
     "around, but hedge the language. Avoid exact hourly or suburb claims."),
    (120, 10**6, "Day 6+",
     "Scenario outlook only. Confidence should be explicitly low."),
)


# --------------------------------------------------------------------------
# Output / notification
# --------------------------------------------------------------------------

NOTIFY_ENABLED = True
NOTIFY_APP_NAME = "Kalyan Weather Agent"

# Tide compounding (Handbook Ch.15). We deliberately do NOT synthesise tide
# times - fabricating them would breach the honesty doctrine of Ch.27. Instead
# the bulletin raises the check when rain reaches the heavy band.
TIDE_TABLE_URL = "https://www.imd.gov.in/pages/tide_tables.php"
TIDE_CHECK_THRESHOLD_MM = HEAVY_DAY_MM

IMD_URLS = {
    "Mumbai radar": "https://mausam.imd.gov.in/imd_latest/contents/index_radar.php",
    "Nowcast / warnings": "https://mausam.imd.gov.in/",
    "Rainfall information": "https://mausam.imd.gov.in/responsive/rainfallinformation.php",
}
