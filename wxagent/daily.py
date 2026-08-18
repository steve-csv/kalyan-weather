"""
Daily rain alert for Kalyan West.

Runs the Guide s15 workflow end to end:
  Step 1  define the forecast (location, valid period, threshold)
  Step 2  observe reality        -> Windy radar/satellite links
  Step 3  diagnose ingredients   -> moisture, lift, instability, organisation
  Step 4  compare models         -> ECMWF vs GFS vs ICON, no averaging
  Step 5  apply terrain and bias -> coast/transition/ghat gradient
  Step 6  verify                 -> logged for later scoring
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import html as _html
import re

from . import config as C
from . import (
    belts as beltmod,
    recent as recentmod,
    nowcast, observed, plain, report, synoptic, systems, thermal,
    upstream, web,
)
from .diagnostics import (
    compass, day_slices, diagnose_day, lift_profile, orographic_reading,
    season_for, window_indices,
)
from .doctrine import assess_confidence, daypart_breakdown, headline
from .notify import notify
from .sources import QuotaExhausted, fetch_ensemble, fetch_point, fetch_sites
from .verify import auto_fill, log_forecast
from .windy import diagnostic_links, model_comparison_links

PRIMARY_MODEL = "ecmwf_ifs025"

# Guide s28.1 four-location laboratory, trimmed to the sites that show the
# coast -> transition -> crest gradient around Kalyan.
GRADIENT_KEYS = ("santacruz", "thane", "kalyan_west", "badlapur",
                 "igatpuri", "matheran", "malshej", "lonavala", "pune")


def _synoptic_html(markdown: str) -> str:
    """
    Minimal markdown -> HTML for the synoptic block on the web page.

    Escapes first, then re-applies only the inline emphasis the synoptic text
    actually uses, so nothing from the data can inject markup.
    """
    out = _html.escape(markdown)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    paragraphs = [p.strip() for p in out.split("\n\n") if p.strip()]
    rendered = []
    for para in paragraphs:
        para = para.replace("\n", " ")
        if para.startswith("&gt;"):
            body = para[4:].strip()
            rendered.append(f'<div class="quote">{body}</div>')
        else:
            rendered.append(f"<p style='margin:0 0 10px'>{para}</p>")
    return "".join(rendered)


def gradient_payload(day: date, forecasts: dict) -> tuple[list[dict], str]:
    """Same gradient data as the markdown table, shaped for the web page."""
    rows: list[dict] = []
    ghat_total = lee_total = None

    for key in GRADIENT_KEYS:
        pf, site = forecasts.get(key), C.SITES_BY_KEY.get(key)
        if pf is None or site is None:
            continue
        idx = window_indices(pf.times, day, 0, 24)
        if not idx:
            continue
        totals = [sum(ms.at("precipitation", i) or 0.0 for i in idx)
                  for ms in pf.models.values()]
        ms = pf.models.get(PRIMARY_MODEL) or next(iter(pf.models.values()))
        lp = lift_profile(ms, idx)
        terrain, _ = orographic_reading(lp.orographic_850, site.zone)

        median_total = sorted(totals)[len(totals) // 2]
        if site.zone == "ghat":
            ghat_total = max(ghat_total or 0.0, median_total)
        elif site.zone == "leeward":
            lee_total = median_total

        rows.append({
            "name": site.name, "zone": site.zone,
            "lo": round(min(totals), 1), "hi": round(max(totals), 1),
            "wind": f"{compass(lp.wind_850_dir)} {lp.wind_850_speed or 0:.1f} m/s",
            "terrain": terrain, "home": key == C.HOME.key,
        })

    verdict = ""
    if ghat_total is not None and lee_total is not None:
        ratio = ghat_total / lee_total if lee_total > 0.5 else float("inf")
        if ratio >= 3:
            verdict = (f"Rain shadow intact — the Ghat crest is running about "
                       f"{ratio:.0f}x the leeward total. Classic westerly regime.")
        elif ratio >= 1.5:
            verdict = (f"Rain shadow weakening — ghat-to-lee ratio about "
                       f"{ratio:.1f}x. Some spillover east of the crest.")
        else:
            verdict = ("Rain shadow overwhelmed — ghat and leeward totals are "
                       "comparable. The signature of broad ascent and deep "
                       "moisture from an inland system, not a coastal "
                       "orographic event.")
    return rows, verdict


def outlook_payload(pf, ens, today: date, n_days: int = 5) -> list[dict]:
    out: list[dict] = []
    for offset in range(1, n_days + 1):
        d = today + timedelta(days=offset)
        dd = diagnose_day(pf, d, ens=ens, primary_model=PRIMARY_MODEL)
        if dd is None:
            continue
        if dd.ensemble and dd.ensemble.p_measurable is not None:
            chance = f"{dd.ensemble.p_measurable:.0%}"
        else:
            chance = f"{dd.rain.agree_on_occurrence}/{dd.rain.n_models}"
        out.append({
            "day": d.strftime("%a %d %b"), "short": d.strftime("%a"),
            "regime": dd.regime.title(),
            "lo": round(dd.rain.lo, 1), "hi": round(dd.rain.hi, 1),
            "chance": chance,
        })
    return out


def terrain_gradient_section(day: date, forecasts: dict) -> str:
    """
    Guide s15 Step 5: apply terrain. A broad westerly rain signal typically
    under-does windward Ghat enhancement and over-does uniform rain east of the
    crest. Showing the gradient makes the terrain correction visible instead of
    implicit.
    """
    rows = ["| Site | Zone | 24 h rain (range) | 850 hPa wind | Terrain effect |",
            "|---|---|---|---|---|"]
    ghat_total = lee_total = None

    for key in GRADIENT_KEYS:
        pf = forecasts.get(key)
        site = C.SITES_BY_KEY.get(key)
        if pf is None or site is None:
            continue
        idx = window_indices(pf.times, day, 0, 24)
        if not idx:
            continue
        totals = [
            sum(ms.at("precipitation", i) or 0.0 for i in idx)
            for ms in pf.models.values()
        ]
        ms = pf.models.get(PRIMARY_MODEL) or next(iter(pf.models.values()))
        lp = lift_profile(ms, idx)
        value, _note = orographic_reading(lp.orographic_850, site.zone)

        median_total = sorted(totals)[len(totals) // 2]
        if site.zone == "ghat":
            ghat_total = max(ghat_total or 0.0, median_total)
        elif site.zone == "leeward":
            lee_total = median_total

        marker = " **(home)**" if key == C.HOME.key else ""
        rows.append(
            f"| {site.name}{marker} | {site.zone} | "
            f"{min(totals):.0f}–{max(totals):.0f} mm | "
            f"{compass(lp.wind_850_dir)} {lp.wind_850_speed or 0:.1f} m/s | "
            f"{value} |"
        )

    out = "\n".join(rows) + "\n\n"
    out += (
        "> The terrain-effect column is signed relative to **which side of the "
        "crest each site sits on**, which is the whole of Handbook Ch.14. The "
        "same westerly is *upslope* at Matheran and *descending* at Pune — a "
        "raw positive number would invert the rain-shadow logic, so the label "
        "says which is happening.\n\n"
    )

    if ghat_total is not None and lee_total is not None:
        ratio = ghat_total / lee_total if lee_total > 0.5 else float("inf")
        if ratio >= 3:
            verdict = (
                f"**Rain shadow intact.** Ghat crest is running about "
                f"{ratio:.0f}× the leeward total. This is the classic westerly "
                "regime of Handbook Ch.14 — moisture is being wrung out on the "
                "windward slope and Pune stays comparatively dry."
            )
        elif ratio >= 1.5:
            verdict = (
                f"**Rain shadow weakening.** Ghat-to-lee ratio is about "
                f"{ratio:.1f}×. Some spillover east of the crest — moisture is "
                "deep enough to survive the crossing (Guide s11.2)."
            )
        else:
            verdict = (
                "**Rain shadow overwhelmed.** Ghat and leeward totals are "
                "comparable. Guide Case Study E: this is the signature of "
                "broad ascent and deep moisture from an inland system rather "
                "than a coastal orographic event — expect widespread, "
                "longer-lasting rain across the whole region including Pune."
            )
        out += f"> {verdict}\n\n"

    out += (
        "> Guide s11: Kalyan sits in the Thane–Kalyan–Karjat transition belt "
        "and can pick up coastal bands *plus* terrain enhancement, so it "
        "commonly runs between the Santacruz and Matheran figures rather than "
        "tracking either.\n"
    )
    return out


def outlook_section(pf, ens, today: date, n_days: int = 4) -> str:
    """Short forward look so the daily alert is not blind past midnight."""
    rows = ["| Day | Regime | Rain (range) | Rain chance |", "|---|---|---|---|"]
    for offset in range(1, n_days + 1):
        d = today + timedelta(days=offset)
        dd = diagnose_day(pf, d, ens=ens, primary_model=PRIMARY_MODEL)
        if dd is None:
            continue
        if dd.ensemble and dd.ensemble.p_measurable is not None:
            chance = f"{dd.ensemble.p_measurable:.0%}"
        else:
            chance = f"{dd.rain.agree_on_occurrence}/{dd.rain.n_models} models"
        rows.append(
            f"| {d:%a %d %b} | {dd.regime.title()} | "
            f"{dd.rain.lo:.0f}–{dd.rain.hi:.0f} mm | {chance} |"
        )
    out = "\n".join(rows) + "\n\n"
    out += ("> Guide s18: at 3–5 days give the trend and risk window only — "
            "avoid exact hourly or suburb-level claims at this range.\n")
    return out


def run(target_day: date | None = None, *, quiet: bool = False,
        no_notify: bool = False) -> tuple[str, str]:
    issued = datetime.now()
    today = target_day or issued.date()
    season = season_for(today)

    if not quiet:
        print(f"Kalyan West daily bulletin for {today:%Y-%m-%d}")
        print("  fetching multi-model point data...")

    pf = fetch_point(C.HOME, days=7)
    ens = fetch_ensemble(C.HOME, days=7)

    if not quiet:
        print("  fetching terrain-gradient sites...")
    gradient_sites = [C.SITES_BY_KEY[k] for k in GRADIENT_KEYS
                      if k in C.SITES_BY_KEY]
    forecasts = fetch_sites(gradient_sites, days=3)
    forecasts[C.HOME.key] = pf

    if not quiet:
        print("  fetching synoptic pressure field...")
    trough_t, offshore_t, inland_t = synoptic.fetch_synoptic(days=7)
    sp = synoptic.build(trough_t, offshore_t, inland_t, today, season)

    if not quiet:
        print("  tracking low pressure systems...")
    sys_pic = systems.analyse(days=7, today=today, quiet=quiet)

    if not quiet:
        print("  sampling upstream drivers (Somali jet, dry-air intrusion)...")
    up = upstream.fetch(days=7, quiet=quiet)

    if not quiet:
        print("  checking which belts are wet...")
    belt_status = beltmod.fetch(now=issued, quiet=quiet)

    if not quiet:
        print("  scoring the last fortnight against what happened...")
    track = recentmod.assess(C.HOME, quiet=quiet)

    dd = diagnose_day(pf, today, ens=ens, primary_model=PRIMARY_MODEL,
                      now=issued)
    if dd is None:
        raise SystemExit(f"no forecast data available for {today}")

    conf = assess_confidence(dd)
    windows = daypart_breakdown(pf, today, PRIMARY_MODEL)
    links = diagnostic_links(C.HOME.lat, C.HOME.lon, season=season)

    # ---- 7-day sequence, thermal outlook, shift alerts -------------------
    week_days = [today + timedelta(days=i) for i in range(7)]
    week_diag = [d for d in
                 (diagnose_day(pf, wd, ens=ens, primary_model=PRIMARY_MODEL,
                               now=issued) for wd in week_days)
                 if d is not None]

    if not quiet:
        print("  building heat/cold outlook...")
    thermal_out = thermal.analyse(C.HOME, pf, week_days,
                                  primary_model=PRIMARY_MODEL, quiet=quiet)

    alerts = plain.detect_shifts(week_diag, thermal_outlook=thermal_out,
                                 systems_picture=sys_pic)

    plain_days = []
    for wd in week_diag:
        wconf = assess_confidence(wd)
        wwin = daypart_breakdown(pf, wd.day, PRIMARY_MODEL)
        plain_days.append(plain.describe_day(wd, wconf, wwin,
                                             site_name=C.HOME.name))

    short = nowcast.short_range(pf, primary_model=PRIMARY_MODEL, now=issued)

    if not quiet:
        print("  scanning upstream for approaching rain...")
    scan = nowcast.scan(C.HOME, now=issued, quiet=quiet)

    if not quiet:
        print("  sampling what fell over the last IMD day...")
    obs = observed.fetch_observed(now=issued, quiet=quiet)

    body = report.render_daily(
        dd, conf, C.HOME, pf, windows, links,
        synoptic.render(sp), PRIMARY_MODEL, issued,
    )

    # ---- nowcast line first: the one line worth reading on a phone ------
    home_area = plain.summarise_areas(
        forecasts, [today], [C.AREAS_BY_KEY[C.HOME_AREA]])
    alert_block = report.h(2, "Right now")
    alert_block += plain.nowcast_line(short, home_area, issued,
                                      home_area=C.HOME_AREA) + "\n\n"
    vline = plain.verification_line()
    if vline:
        alert_block += vline + "\n\n"

    alert_block += report.h(2, "⚡ Major weather shifts — next 7 days")
    alert_block += plain.render_alerts(alerts) + "\n"

    weekend = plain.summarise_weekend(plain_days, week_diag)
    plain_block = ""
    if weekend is not None:
        plain_block += report.h(2, "This weekend")
        plain_block += plain.render_weekend(weekend)

    plain_block += report.h(2, "In plain English — day by day")
    plain_block += plain.render_plain_week(plain_days)
    plain_block += ("> Everything below this line is the technical working "
                    "behind these seven lines. The plain version is the "
                    "forecast; the rest is why.\n\n---\n\n")

    body = body.replace("---\n\n## Forecast",
                        "---\n\n" + alert_block + plain_block + "## Forecast", 1)

    # Extra sections specific to the daily product.
    extra = report.h(2, "What fell — last complete IMD day")
    extra += observed.render(obs) if obs else "_Not available this run._\n"
    extra += "\n" + report.h(2, "Now — next few hours")
    extra += nowcast.render(short)
    extra += nowcast.render_scan(scan)
    extra += beltmod.render(belt_status)
    extra += "\n" + report.h(2, "Track record — forecast against outcome")
    extra += recentmod.render(track)
    extra += upstream.render(up, zone=C.HOME.zone, day=today)
    extra += report.h(2, "Low pressure systems, troughs and storms")
    extra += systems.render(sys_pic)
    extra += report.h(2, "Heat and cold — next 7 days")
    extra += thermal.render(thermal_out) + "\n"
    extra += report.h(2, "Terrain gradient — coast to crest to rain shadow")
    extra += terrain_gradient_section(today, forecasts)
    extra += "\n" + report.h(2, "Next four days")
    extra += outlook_section(pf, ens, today)
    extra += "\n" + report.h(2, "Model comparison on Windy")
    for lk in model_comparison_links(C.HOME.lat, C.HOME.lon):
        extra += f"- **[{lk.title}]({lk.url})** — {lk.why}\n"
    extra += "\n"

    marker = "## Check it on Windy"
    body = body.replace(marker, extra + marker, 1)

    path = C.FORECAST_DIR / f"{today:%Y-%m-%d}_kalyan_west.md"
    path.write_text(body, encoding="utf-8")

    # ---- web dashboard ---------------------------------------------------
    grad_rows, grad_verdict = gradient_payload(today, forecasts)
    payload = web.build_payload(
        dd, conf, C.HOME, pf, windows, PRIMARY_MODEL, issued,
        gradient=grad_rows,
        outlook=outlook_payload(pf, ens, today),
        synoptic_text=_synoptic_html(synoptic.render(sp)),
    )
    payload["gradientVerdict"] = grad_verdict
    payload["alerts"] = [
        {"severity": a.severity, "icon": a.icon, "title": a.title,
         "body": a.body, "label": plain.SEVERITY_LABEL.get(a.severity, "")}
        for a in alerts
    ]
    # Each day carries BOTH the finished sentences and the facts they were
    # built from. The facts let `wxagent render` regenerate the wording with no
    # network access at all - without them, every phrasing change waited on a
    # full rebuild and therefore on the API budget.
    day_fact_map = {}
    for wd in week_diag:
        wconf = assess_confidence(wd)
        wwin = daypart_breakdown(pf, wd.day, PRIMARY_MODEL)
        day_fact_map[wd.day] = plain.day_facts(wd, wconf, wwin)

    payload["plainWeek"] = [
        {"day": p.day.strftime("%A %d %B"), "short": p.day.strftime("%a"),
         "iso": p.day.isoformat(),
         "icon": p.icon, "headline": p.headline, "detail": p.detail,
         "advice": p.advice, "confidence": p.confidence,
         "isWeekend": p.day.weekday() in (5, 6),
         "facts": day_fact_map.get(p.day, {})}
        for p in plain_days
    ]
    # Area figures are carried on the daily payload too, so Sam can
    # answer "what about Thane" without the reader switching pages.
    #
    # The daily run only fetches the terrain-gradient sites, so this covers a
    # SUBSET of the nine MMR areas. It is flagged as partial: without that,
    # "wettest place this week" would confidently name a winner from an
    # incomplete field. The full breakdown comes from the weekly run.
    area_rows = plain.summarise_areas(forecasts, week_days, C.MMR_AREAS)
    payload["areas"] = [
        {"key": a.key, "name": a.name, "character": a.character,
         "weekMm": round(a.week_mm, 1), "accumulation": a.accumulation,
         "wettestDay": a.wettest_day.strftime("%a %d") if a.wettest_day else "",
         "plain": a.plain, "rank": a.rank, "home": a.key == C.HOME_AREA}
        for a in area_rows
    ]
    payload["areasPartial"] = len(area_rows) < len(C.MMR_AREAS)
    payload["areasTotal"] = len(C.MMR_AREAS)

    weekend = plain.summarise_weekend(plain_days, week_diag)
    payload["weekend"] = ({
        "verdict": weekend.verdict, "plans": weekend.plans,
        "days": [d.day.strftime("%A %d %B") for d in weekend.days],
    } if weekend else None)
    payload["thermal"] = {
        "headline": thermal_out.headline,
        "caveat": thermal_out.caveat,
        "stationType": thermal_out.station_type,
        "days": [
            {"day": t.day.strftime("%a %d %b"),
             "tmax": t.tmax, "tmin": t.tmin,
             "depMax": t.departure_max, "depMin": t.departure_min,
             "heatIndex": t.heat_index,
             "heatFlag": t.heat_flag, "coldFlag": t.cold_flag,
             "note": t.note}
            for t in thermal_out.days
        ],
    }
    payload["track"] = {
        "available": bool(track and track.verified),
        "summary": track.summary if track else "",
        "verified": len(track.verified) if track else 0,
        "correct": track.n_correct if track else 0,
        "bandPct": (round(track.band_accuracy * 100)
                    if track and track.band_accuracy is not None else None),
        "maeWet": (round(track.mae_wet, 1)
                   if track and track.mae_wet is not None else None),
        "baseRate": (round(track.base_rate * 100)
                     if track and track.base_rate is not None else None),
        "days": [
            {"day": d.day.isoformat(), "label": d.day.strftime("%a %d"),
             "fc": d.forecast_mm, "obs": d.observed_mm,
             "verdict": d.verdict or "", "band": d.band_match}
            for d in (track.days if track else [])
            if d.forecast_mm is not None or d.observed_mm is not None
        ],
    }
    payload["belts"] = {
        "available": bool(belt_status),
        "headline": beltmod.headline(belt_status) if belt_status else "",
        "list": [
            {"name": b.belt.name, "zone": b.belt.zone, "key": b.belt.key,
             "state": b.state, "sentence": b.sentence,
             "nowMmH": round(b.now_mm_h, 1), "peakMmH": round(b.peak_mm_h, 1),
             "place": b.wettest_place, "eta": b.eta_hours,
             "hours": [round(v, 1) for v in b.hours]}
            for b in (belt_status or [])
        ],
    }
    up_today = up.for_day(today) if up else None
    payload["upstream"] = {
        "available": bool(up_today),
        "jet": up_today.jet_label if up_today else "",
        "jetNote": up_today.jet_note if up_today else "",
        "jetSpeed": (round(up_today.corridor_speed)
                     if up_today and up_today.corridor_speed is not None else None),
        "jetDir": (upstream.compass(up_today.corridor_dir)
                   if up_today else ""),
        "coreSpeed": (round(up_today.core_speed)
                      if up_today and up_today.core_speed is not None else None),
        "sourceSupports": (up_today.source_supports if up_today else None),
        "dry": up_today.dry_label if up_today else "",
        "dryNote": up_today.dry_note if up_today else "",
        "midRh": (round(up_today.mid_rh)
                  if up_today and up_today.mid_rh is not None else None),
        "rh700": (round(up_today.rh700)
                  if up_today and up_today.rh700 is not None else None),
        "advection": up_today.advection_state if up_today else "",
        "reading": upstream.zone_reading(up_today, C.HOME.zone) if up_today else "",
        "week": [
            {"day": d.day.isoformat(),
             "jet": d.jet_label,
             "jetSpeed": round(d.corridor_speed) if d.corridor_speed is not None else None,
             "dry": d.dry_label,
             "midRh": round(d.mid_rh) if d.mid_rh is not None else None,
             "advection": d.advection_state}
            for d in (up.days if up else [])
        ],
    }
    payload["systems"] = {
        "trough": sys_pic.trough.note if sys_pic else "",
        "troughPresent": bool(sys_pic and sys_pic.trough.present),
        "cycloneWindow": bool(sys_pic and sys_pic.cyclone_window),
        "list": [
            {"relevance": a.relevance, "headline": a.headline,
             "reasoning": a.reasoning,
             "distanceKm": round(a.track.closest_approach.distance_km),
             "pressure": round(a.track.peak.pressure, 1),
             "movedKm": round(a.track.moved_km)}
            for a in (sys_pic.significant if sys_pic else [])
        ],
    }
    # Rain split by time of day, for today (donut) and across the week
    # (heatmap). Both answer "when in the day", which the daily total hides.
    payload["dayparts"] = [
        {"label": w.label, "mm": round(w.total_mm, 1)} for w in windows
    ]
    heat_days, heat_cells = [], []
    part_labels = [lbl for lbl, _lo, _hi in plain.DAYPART_ORDER]
    for wd in week_diag:
        heat_days.append(wd.day.strftime("%a %d"))
    for lbl, lo, hi in plain.DAYPART_ORDER:
        row = []
        for wd in week_diag:
            idx = window_indices(pf.times, wd.day, lo, hi)
            ms = pf.models.get(PRIMARY_MODEL) or next(iter(pf.models.values()))
            row.append(round(sum(ms.at("precipitation", i) or 0.0
                                 for i in idx), 1))
        heat_cells.append(row)
    payload["heatmap"] = {"days": heat_days, "parts": part_labels,
                          "cells": heat_cells}

    payload["nowcast"] = {
        "verdict": short.verdict if short else "",
        "trend": short.trend if short else "",
        "hours": [{"t": dt.strftime("%H:%M"), "mm": round(v, 1)}
                  for dt, v in (short.hours if short else [])],
        "radar": [{"url": nowcast.radar_url(p), "name": n, "why": w}
                  for p, n, w in nowcast.IMD_RADAR_PRODUCTS],
        "radarPage": nowcast.IMD_RADAR_PAGE,
        "rainviewer": nowcast.RAINVIEWER_MAP,
        "questions": list(nowcast.RADAR_SCAN_QUESTIONS),
        "scan": ([{"q": q, "a": a} for q, a in scan.answers] if scan else []),
        "scanAt": (scan.generated.strftime("%H:%M") if scan else ""),
        "scanEta": (round(scan.eta_hours, 1)
                    if scan and scan.eta_hours else None),
        # Drives the cross-section animation: the steering speed sets how fast
        # the cells cross it, so a racing band and a crawling one look
        # different on the page rather than both drifting at some default.
        "scanSteerKmh": (round(scan.steer_speed_kmh) if scan else None),
        "scanFrom": (nowcast.compass_from(scan.upstream_bearing)
                     if scan else ""),
        "limits": list(nowcast.RADAR_LIMITS),
    }
    html = web.render(payload, links,
                      model_comparison_links(C.HOME.lat, C.HOME.lon))
    page = C.FORECAST_DIR / "index.html"
    page.write_text(html, encoding="utf-8")

    # Cached so `python -m wxagent publish` can combine the latest daily and
    # weekly views into one page without re-fetching either.
    web.cache_payload(payload, web.DAILY_CACHE)

    # Keep artifact.html in step with every scheduled run. Without this it was
    # only rebuilt when `render`/`publish` was invoked by hand, so the shared
    # link silently drifted days behind the local pages.
    weekly_cached = web.load_payload(web.WEEKLY_CACHE)
    if weekly_cached is not None:
        try:
            (C.FORECAST_DIR / "artifact.html").write_text(
                web.render_artifact(
                    payload, weekly_cached, links,
                    model_comparison_links(C.HOME.lat, C.HOME.lon)),
                encoding="utf-8")
        except Exception as exc:                  # noqa: BLE001
            print(f"  ! could not refresh artifact.html: {exc}")
    web.cache_payload(
        {"windy": [{"title": l.title, "url": l.url, "why": l.why} for l in links],
         "models": [{"title": l.title, "url": l.url, "why": l.why}
                    for l in model_comparison_links(C.HOME.lat, C.HOME.lon)]},
        C.CACHE_DIR / "links.json")

    # The notification leads with the most severe shift alert when there is
    # one - a "sharp jump in rain on Friday" is worth interrupting someone for
    # in a way that today's millimetre range is not.
    head = headline(dd, conf, C.HOME.name)
    if alerts and alerts[0].severity in ("critical", "warning"):
        head = f"{alerts[0].icon} {alerts[0].title}. {head}"

    log_forecast(dd, conf, C.HOME.key, issued)

    # Close the loop automatically. Guide s26 makes the log the thing that
    # turns practice into skill, but a log that needs a manual entry per day
    # simply stays empty - so past forecasts are scored against the ERA5
    # archive as soon as the day is old enough, without being asked.
    try:
        filled, pending = auto_fill(today=today, quiet=quiet)
        if filled and not quiet:
            print(f"  verified {filled} past forecast(s) against observations"
                  + (f"; {pending} still awaiting data" if pending else ""))
    except Exception as exc:                      # noqa: BLE001
        if not quiet:
            print(f"  ! auto-verification skipped: {exc}")

    if not quiet:
        print(f"\n{head}\n")
        print(f"  written: {path}")
        print(f"  webpage: {page}")

    # Clicking the toast opens the dashboard, not the raw markdown.
    if not no_notify:
        notify(f"Rain outlook — {C.HOME.name}", head,
               launch=page.resolve().as_uri())

    return head, str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Daily rain bulletin for Kalyan West.")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)

    day = date.fromisoformat(args.date) if args.date else None
    # QuotaExhausted is handled centrally in __main__.main() so every command
    # degrades identically; nothing to catch here.
    run(day, quiet=args.quiet, no_notify=args.no_notify)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
