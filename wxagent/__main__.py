"""
Command line entry point.

    python -m wxagent daily           # Kalyan West rain bulletin for today
    python -m wxagent weekly          # Mumbai MMR 7-day wind outlook
    python -m wxagent verify --date 2026-08-10 --mm 23.5
    python -m wxagent score           # POD / FAR / CSI scorecard
    python -m wxagent windy-check     # is the Windy key serving real data?
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from . import config as C


def _publish_after(quiet: bool) -> int:
    """Push the refreshed pages to GitHub Pages.

    Called by the scheduled runs so the public page updates itself. A publish
    failure is reported but does not fail the run: the bulletin has already
    been written and notified, and losing that to a transient network or git
    error would be the worse outcome.
    """
    from .ghpages import PublishError, publish
    try:
        print(publish(message=None, quiet=quiet))
        return 0
    except PublishError as exc:
        print(f"\npublish skipped: {exc}")
        return 0
    except Exception as exc:                      # noqa: BLE001
        print(f"\npublish failed: {exc}")
        return 0


def cmd_daily(args) -> int:
    from .daily import run
    run(date.fromisoformat(args.date) if args.date else None,
        quiet=args.quiet, no_notify=args.no_notify)
    if getattr(args, "publish", False):
        return _publish_after(args.quiet)
    return 0


def cmd_weekly(args) -> int:
    from .weekly import run
    run(date.fromisoformat(args.start) if args.start else None,
        days=args.days, quiet=args.quiet, no_notify=args.no_notify)
    if getattr(args, "publish", False):
        return _publish_after(args.quiet)
    return 0


def cmd_verify(args) -> int:
    from .verify import auto_fill, record_actual, render_scorecard

    if args.auto:
        filled, pending = auto_fill()
        print(f"Verified {filled} forecast(s) against ERA5 observations."
              + (f" {pending} still awaiting data." if pending else ""))
        print()
        print(render_scorecard(args.site))
        return 0

    if not args.date or args.mm is None:
        print("Give --date and --mm to record an observation by hand, "
              "or --auto to fill everything from the archive.")
        return 2
    n = record_actual(date.fromisoformat(args.date), args.site, args.mm,
                      character=args.character or "", lesson=args.lesson or "")
    if n == 0:
        print(f"No logged forecast found for {args.date} at {args.site}.")
        return 1
    print(f"Recorded {args.mm} mm for {args.date} ({n} row(s) updated).\n")
    print(render_scorecard(args.site))
    return 0


def cmd_score(args) -> int:
    from .verify import render_scorecard
    print(render_scorecard(args.site))
    return 0


def cmd_publish(args) -> int:
    """
    Build the single-page version for publishing as a shareable link.

    Combines the latest daily and weekly runs into one tabbed page with no
    document wrapper, ready to hand to the Artifact publisher. Note this is a
    SNAPSHOT - a published page cannot fetch new data, so it shows whatever the
    last runs produced.
    """
    from dataclasses import dataclass
    from . import web

    daily = web.load_payload(web.DAILY_CACHE)
    weekly = web.load_payload(web.WEEKLY_CACHE)
    links_blob = web.load_payload(C.CACHE_DIR / "links.json")

    missing = [n for n, v in (("daily", daily), ("weekly", weekly)) if v is None]
    if missing:
        print(f"Missing cached {' and '.join(missing)} data.")
        print("Run these first:")
        for m in missing:
            print(f"    python -m wxagent {m}")
        return 1

    @dataclass
    class _Link:
        title: str
        url: str
        why: str

    windy = [_Link(**d) for d in (links_blob or {}).get("windy", [])]
    models = [_Link(**d) for d in (links_blob or {}).get("models", [])]

    html = web.render_artifact(daily, weekly, windy, models)
    out = C.FORECAST_DIR / "artifact.html"
    out.write_text(html, encoding="utf-8")

    print(f"Wrote {out} ({len(html):,} bytes)")
    print(f"  today  : {daily.get('validDate')}  (issued {daily.get('issued')})")
    print(f"  week   : {weekly.get('validDate')} (issued {weekly.get('issued')})")
    print("\nThis is a snapshot - a published page cannot fetch new data.")
    return 0


def cmd_backtest(args) -> int:
    from .backtest import main as backtest_main
    argv: list[str] = []
    if args.start:
        argv += ["--start", args.start]
    if args.end:
        argv += ["--end", args.end]
    argv += ["--site", args.site, "--threshold", str(args.threshold)]
    if args.no_terrain:
        argv.append("--no-terrain")
    if args.deep:
        argv += ["--deep", "--years", args.years]
    if args.no_zones:
        argv.append("--no-zones")
    if args.cached:
        argv.append("--cached")
    return backtest_main(argv)


def _reword(daily: dict | None) -> int:
    """
    Regenerate the plain-language day text in place from stored facts.

    Returns how many days were re-worded. Days saved before facts were
    captured are left exactly as they are - there is nothing to rebuild them
    from, and inventing a description would be worse than showing the old one.
    """
    if not daily:
        return 0
    from datetime import date as _date
    from . import plain

    n = 0
    for row in daily.get("plainWeek", []) or []:
        facts = row.get("facts")
        iso = row.get("iso")
        if not facts or not iso:
            continue
        try:
            p = plain.describe_from_facts(_date.fromisoformat(iso), facts)
        except (ValueError, KeyError):
            continue
        row["headline"] = p.headline
        row["detail"] = p.detail
        row["advice"] = p.advice
        row["confidence"] = p.confidence
        row["icon"] = p.icon
        n += 1
    return n


def cmd_render(args) -> int:
    """
    Rebuild the HTML pages from the last cached payloads, fetching nothing.

    Useful whenever the presentation changes but the data has not - and
    essential when the API quota is exhausted, since it needs no network at all.
    """
    from dataclasses import dataclass
    from . import web

    daily = web.load_payload(web.DAILY_CACHE)
    weekly = web.load_payload(web.WEEKLY_CACHE)
    links_blob = web.load_payload(C.CACHE_DIR / "links.json") or {}

    if daily is None and weekly is None:
        print("No cached payloads. Run `python -m wxagent daily` first.")
        return 1

    @dataclass
    class _Link:
        title: str
        url: str
        why: str

    windy = [_Link(**d) for d in links_blob.get("windy", [])]
    models = [_Link(**d) for d in links_blob.get("models", [])]
    written = []

    reworded = _reword(daily)

    # Drivers are computed by the weekly run; attach them to both payloads so
    # either page can show them.
    drivers = web.load_payload(C.CACHE_DIR / "drivers.json")
    if drivers:
        if daily is not None:
            daily["drivers"] = drivers
        if weekly is not None:
            weekly["drivers"] = drivers

    # Week summary spans past + future, so it is rebuilt on every render: the
    # observed portion grows by a day each day even when the forecast has not
    # been refreshed.
    if daily:
        from datetime import date as _d, timedelta as _td
        from . import summary as _sm
        try:
            today = _d.today()
            ws = _sm.build(daily, weekly, C.HOME,
                           start=today - _td(days=2),
                           end=today + _td(days=4), today=today)
            daily["summary"] = _sm.to_payload(ws)
            if weekly is not None:
                weekly["summary"] = daily["summary"]
        except Exception as exc:                  # noqa: BLE001
            print(f"  ! week summary unavailable: {exc}")

    if daily is not None:
        p = C.FORECAST_DIR / "index.html"
        p.write_text(web.render(daily, windy, models), encoding="utf-8")
        written.append(p.name)
    if weekly is not None:
        p = C.FORECAST_DIR / "mmr.html"
        p.write_text(web.render(weekly, windy, []), encoding="utf-8")
        written.append(p.name)
    if daily is not None and weekly is not None:
        p = C.FORECAST_DIR / "artifact.html"
        p.write_text(web.render_artifact(daily, weekly, windy, models),
                     encoding="utf-8")
        written.append(p.name)

    print(f"Re-rendered from cache (no API calls): {', '.join(written)}")
    if daily:
        print(f"  data from: {daily.get('issued')}")
    if reworded:
        print(f"  re-worded {reworded} day(s) from stored facts")
    elif daily and daily.get("plainWeek"):
        print("  wording unchanged — this build predates stored facts, so the "
              "text can only refresh on the next full run")
    return 0


def cmd_gh_setup(args) -> int:
    from .ghpages import setup_text
    print(setup_text())
    return 0


def cmd_gh_publish(args) -> int:
    from .ghpages import PublishError, publish
    try:
        print(publish(message=args.message, quiet=args.quiet))
    except PublishError as exc:
        print(f"\n{exc}")
        return 1
    return 0


def cmd_serve(args) -> int:
    from .serve import serve
    return serve(port=args.port, tunnel=args.tunnel,
                 allow_sleep=args.allow_sleep, hours=args.hours)


def cmd_windy_check(args) -> int:
    """
    Report whether the configured Windy key returns real forecast data.

    Windy's testing tier returns data that is, in its own words, "randomly
    shuffled and slightly modified". That is worse than no data, because it
    looks entirely plausible. This command tells you which you are getting.
    """
    from .sources import fetch_windy_point, windy_data_is_real, windy_units_note

    if not C.WINDY_API_KEY:
        print("No Windy API key configured.")
        print("Set the WINDY_API_KEY environment variable, or put it in "
              "wxagent/local_config.py.")
        print("\nThe agent works fully without one - ECMWF, GFS and ICON all "
              "come from Open-Meteo.")
        return 1

    masked = C.WINDY_API_KEY[:4] + "..." + C.WINDY_API_KEY[-4:]
    print(f"Windy key configured: {masked}")
    print("Calling the Point Forecast API...\n")

    payload = fetch_windy_point(C.HOME, strict=False)
    if payload is None:
        print("Call failed. Check connectivity and the key.")
        return 1

    real, warning = windy_data_is_real(payload)
    if real:
        print("REAL DATA. Windy returned no testing-tier warning.")
        print(f"  timesteps: {len(payload.get('ts', []))}")
        print(f"  {windy_units_note()}")
        return 0

    print("TESTING-TIER DATA - NOT USABLE FOR FORECASTING.")
    print(f"  Windy says: {warning.strip()}")
    print("\nThis data is deliberately scrambled. The agent discards it rather")
    print("than let it silently corrupt a bulletin.")
    print("\nTo fix, at https://api.windy.com/keys:")
    print("  1. Edit the key and fill in 'Project identification'")
    print("     (e.g. local.kalyan-wx-agent) - the RESTRICTION MISSING badge")
    print("     is the symptom.")
    print("  2. Optionally add a domain restriction if you host it anywhere.")
    print("  3. Re-run: python -m wxagent windy-check")
    print("\nNothing else depends on this. ECMWF, GFS and ICON come from")
    print("Open-Meteo and are unaffected.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wxagent",
        description="Rainfall forecasting agent for Kalyan West and the "
                    "Mumbai Metropolitan Region.")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daily", help="Kalyan West rain bulletin")
    d.add_argument("--date")
    d.add_argument("--quiet", action="store_true")
    d.add_argument("--no-notify", action="store_true")
    d.add_argument("--publish", action="store_true",
                   help="push the refreshed pages to GitHub Pages afterwards")
    d.set_defaults(func=cmd_daily)

    w = sub.add_parser("weekly", help="Mumbai MMR 7-day wind outlook")
    w.add_argument("--start")
    w.add_argument("--days", type=int, default=7)
    w.add_argument("--quiet", action="store_true")
    w.add_argument("--no-notify", action="store_true")
    w.add_argument("--publish", action="store_true",
                   help="push the refreshed pages to GitHub Pages afterwards")
    w.set_defaults(func=cmd_weekly)

    v = sub.add_parser("verify", help="record what actually happened")
    v.add_argument("--date", help="the day being verified")
    v.add_argument("--mm", type=float, help="observed 24h mm")
    v.add_argument("--auto", action="store_true",
                   help="fill every past forecast from the ERA5 archive "
                        "instead of entering one by hand")
    v.add_argument("--site", default=C.HOME.key)
    v.add_argument("--character", help="e.g. 'intermittent moderate'")
    v.add_argument("--lesson", help="one sentence on the main error")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("score", help="POD / FAR / CSI scorecard")
    s.add_argument("--site", default=None)
    s.set_defaults(func=cmd_score)

    p = sub.add_parser("publish",
                       help="build the single-page snapshot for sharing")
    p.set_defaults(func=cmd_publish)

    b = sub.add_parser("backtest",
                       help="score the agent against archived runs + ERA5")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--site", default=C.HOME.key)
    b.add_argument("--threshold", type=float, default=C.MEASURABLE_RAIN_MM)
    b.add_argument("--no-terrain", action="store_true")
    b.add_argument("--deep", action="store_true",
                   help="multi-season run with bootstrap confidence intervals, "
                        "a threshold sweep and probabilistic scores")
    b.add_argument("--years", default="2024,2025,2026")
    b.add_argument("--no-zones", action="store_true")
    b.add_argument("--cached", action="store_true",
                   help="reuse saved deep-backtest records, fetching nothing")
    b.set_defaults(func=cmd_backtest)

    gs = sub.add_parser("gh-setup",
                        help="one-time steps to put this on GitHub Pages")
    gs.set_defaults(func=cmd_gh_setup)

    gp = sub.add_parser("gh-publish",
                        help="build the public pages and push them to GitHub")
    gp.add_argument("--message")
    gp.add_argument("--quiet", action="store_true")
    gp.set_defaults(func=cmd_gh_publish)

    rn = sub.add_parser("render",
                        help="rebuild the HTML from cache, fetching nothing")
    rn.set_defaults(func=cmd_render)

    sv = sub.add_parser("serve",
                        help="serve the dashboard on your WiFi for phone access")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--tunnel", action="store_true",
                    help="also expose a public https link (needs cloudflared) "
                         "so someone outside your home network can open it")
    sv.add_argument("--allow-sleep", action="store_true",
                    help="don't hold off sleep while serving (by default the "
                         "machine is kept awake, since a sleeping laptop "
                         "cannot serve anything)")
    sv.add_argument("--hours", type=float, default=2.0,
                    help="stop automatically after this many hours so the "
                         "laptop can sleep again (default 2; use 0 to run "
                         "until you close the window)")
    sv.set_defaults(func=cmd_serve)

    k = sub.add_parser("windy-check",
                       help="check whether the Windy key returns real data")
    k.set_defaults(func=cmd_windy_check)

    return ap


def main(argv: list[str] | None = None) -> int:
    from .sources import QuotaExhausted

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except QuotaExhausted as exc:
        # Caught centrally so every command degrades the same way. A scheduled
        # run that hits the quota should say so plainly and leave the previous
        # bulletin intact rather than half-writing a new one.
        print(f"\nCannot fetch data: {exc}")
        print("Existing files in forecasts/ are unchanged.")
        print("\nIf this keeps happening, the request load is too high — the "
              "backtest is by far the most expensive command, so run it "
              "sparingly rather than alongside the daily and weekly builds.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
