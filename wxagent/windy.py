"""
Windy deep-links.

The bulletins name a mechanism, then hand you the exact Windy view that shows
it, so the map check stays the two-minute routine of Handbook Ch.15 rather than
a hunt through the layer menu.

Windy URL shape used here:

    https://www.windy.com/?[model,]overlay[,level],lat,lon,zoom

Windy's URL scheme is not a published API and can change; if a link ever opens
the wrong layer, set it by hand from the layer menu - the analysis in the
bulletin is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config as C

ZOOM_LOCAL = 9        # Kalyan / MMR detail
ZOOM_REGIONAL = 7     # Konkan + Ghats + Deccan
ZOOM_SYNOPTIC = 5     # Arabian Sea + peninsula
ZOOM_CONTINENTAL = 4  # monsoon trough across north India

# Windy model ids as they appear in the URL.
WINDY_MODEL = {
    "ecmwf_ifs025": "ecmwf",
    "gfs_seamless": "gfs",
    "icon_seamless": "icon",
}


def link(overlay: str, lat: float, lon: float, *, zoom: int = ZOOM_LOCAL,
         level: str | None = None, model: str | None = None) -> str:
    parts: list[str] = []
    if model:
        parts.append(WINDY_MODEL.get(model, model))
    parts.append(overlay)
    if level:
        parts.append(level)
    parts += [f"{lat:.3f}", f"{lon:.3f}", str(zoom)]
    return "https://www.windy.com/?" + ",".join(parts)


def meteogram(lat: float, lon: float) -> str:
    """Point detail panel - the meteogram of Handbook Ch.20."""
    return f"https://www.windy.com/{lat:.3f}/{lon:.3f}?{lat:.3f},{lon:.3f},10"


@dataclass(frozen=True)
class LayerLink:
    title: str
    url: str
    why: str


def diagnostic_links(lat: float, lon: float, *,
                     season: str = "monsoon") -> list[LayerLink]:
    """
    The layer set the guides actually use, in the order Handbook Ch.25's
    morning routine works through them - pressure first, rain last.
    """
    links = [
        LayerLink(
            "Pressure - monsoon trough (north India)",
            link("pressure", 24.0, 80.0, zoom=ZOOM_CONTINENTAL, model="ecmwf_ifs025"),
            "Handbook Ch.13: trough hugging the Himalayan foothills = break "
            "signal; trough back over the plains = active signal. Check this "
            "first - it tells you more about the week's character than "
            "anything else.",
        ),
        LayerLink(
            "Pressure - offshore trough (Konkan coast)",
            link("pressure", 18.5, 72.5, zoom=ZOOM_SYNOPTIC, model="ecmwf_ifs025"),
            "Guide s12.1: an elongated low parallel to the west coast promotes "
            "convergence, organises coastal convection and maintains onshore "
            "flow.",
        ),
        LayerLink(
            "Wind at 850 hPa - the moisture-transport layer",
            link("wind", lat, lon, zoom=ZOOM_REGIONAL, level="850h",
                 model="ecmwf_ifs025"),
            "Guide s10.1: 850 hPa wind is the useful first approximation of "
            "the monsoon low-level jet. Check the angle against the Ghats, not "
            "just the speed.",
        ),
        LayerLink(
            "Wind at 925 hPa - very-low-level flow",
            link("wind", lat, lon, zoom=ZOOM_REGIONAL, level="925h",
                 model="ecmwf_ifs025"),
            "Guide s10.1: shows the flow closest to the surface without the "
            "friction distortion of the 10 m wind.",
        ),
        LayerLink(
            "Relative humidity at 700 hPa - moisture depth",
            link("rh", lat, lon, zoom=ZOOM_REGIONAL, level="700h",
                 model="ecmwf_ifs025"),
            "Guide s4.4: the single best check on whether cloud can grow deep. "
            "Humid at the surface but dry here means shallow cloud and "
            "disappointing rain.",
        ),
        LayerLink(
            "Relative humidity at 850 hPa",
            link("rh", lat, lon, zoom=ZOOM_REGIONAL, level="850h",
                 model="ecmwf_ifs025"),
            "The middle rung of the moisture-depth ladder.",
        ),
        LayerLink(
            "CAPE - convective potential",
            link("cape", lat, lon, zoom=ZOOM_REGIONAL, model="ecmwf_ifs025"),
            "Handbook Ch.24: how much energy is available IF something "
            "triggers it. Never read CAPE as a rainfall amount.",
        ),
        LayerLink(
            "Radar - what is actually happening now",
            link("radar", lat, lon, zoom=ZOOM_LOCAL),
            "Guide s16: for the next 0-3 hours this outranks every model. "
            "Animate it - a single frame is not a forecast.",
        ),
        LayerLink(
            "Satellite - cloud texture and growth",
            link("satellite", lat, lon, zoom=ZOOM_REGIONAL),
            "Guide s17: smooth sheets mean layered cloud; bright bubbling "
            "towers mean growing convection.",
        ),
        LayerLink(
            "Rain accumulation - checked LAST",
            link("rainAccu", lat, lon, zoom=ZOOM_REGIONAL, model="ecmwf_ifs025"),
            "Guide learning rule: identify the mechanism first. The rain layer "
            "is the output of everything above, not the starting point.",
        ),
        LayerLink(
            "Point meteogram for this location",
            meteogram(lat, lon),
            "Handbook Ch.20: where the precise numbers live - dew point "
            "against temperature, wind by level, hourly precipitation.",
        ),
    ]

    if season in ("pre_monsoon", "post_monsoon"):
        links.insert(7, LayerLink(
            "Wind at 500 hPa - shear check",
            link("wind", lat, lon, zoom=ZOOM_REGIONAL, level="500h",
                 model="ecmwf_ifs025"),
            "Handbook Ch.24: compare against the low-level wind. Low shear = "
            "short-lived pulse storms; notable shear = organised, longer-lived, "
            "faster-moving storms.",
        ))
        links.insert(8, LayerLink(
            "Waves / swell - Arabian Sea cyclone context",
            link("waves", 17.0, 70.0, zoom=ZOOM_SYNOPTIC),
            "Handbook Ch.17: this is one of the two Arabian Sea cyclone "
            "windows. Scan the pressure layer south of ~15N for any organising "
            "closed low.",
        ))
    return links


def model_comparison_links(lat: float, lon: float) -> list[LayerLink]:
    """One rain-accumulation link per model - the Ch.21 three-way comparison."""
    return [
        LayerLink(
            f"{m.label} rain accumulation",
            link("rainAccu", lat, lon, zoom=ZOOM_REGIONAL, model=m.key),
            m.note,
        )
        for m in C.MODELS
    ]
