# Working on this repository

A rainfall forecasting agent for Kalyan West and the Mumbai Metropolitan
Region. `README.md` explains how to run it; `KNOWLEDGE.md` holds the
meteorological doctrine it reasons from. This file is for whoever — or
whatever — edits the code next.

## What this is, and what it is not

It runs **no weather model of its own**. Every number comes from ECMWF, GFS
and ICON via Open-Meteo, with ERA5 reanalysis as the verification truth. What
this repository contributes is the diagnostic layer on top: naming the
mechanism, applying terrain, splitting confidence, phrasing it for a
non-specialist, and scoring itself afterwards.

That framing matters when you change something. The value is not in producing
a number — anyone can read a number off Windy. It is in the number arriving
with an honest account of where it came from and how often it has been wrong.

## Ground rules

These are not style preferences. Each one exists because breaking it produced
a confidently wrong forecast at some point.

1. **Never average the models.** Report the spread. Three models disagreeing
   is information; their mean is a number no model predicted.
2. **Never invent precision.** If the underlying data cannot support a claim,
   do not make the claim more specific than the data. Two live examples are
   in "Traps" below.
3. **Never verify against a model's own analysis.** Truth is ERA5, pinned
   explicitly. Anything else is marking your own homework.
4. **Publish the misses.** The track record exists to be checkable. Do not add
   filtering that quietly improves it.
5. **State limits where the reader will see them**, not only in comments. The
   page carries its own caveats deliberately.
6. **No new dependencies.** Standard library only — that is what lets the
   GitHub Actions run need no install step. There is a hand-written PNG
   decoder (`pngread.py`) rather than Pillow, for exactly this reason.

## Layout

```
wxagent/
  config.py       sites, zones, thresholds. Zone drives RAINFALL role;
                  imd_type drives HEAT/COLD role. They differ on purpose.
  sources.py      all Open-Meteo access. Multi-site fetches are batched.
  diagnostics.py  moisture / lift / stability / organisation, burst risk
  systems.py      pressure field, low tracking, basins, offshore trough
  upstream.py     Somali jet, mid-level dry-air intrusion
  belts.py        named-belt rain arrivals in plain language
  nowcast.py      IMD radar products, the five-question scan
  plain.py        the forecaster-register wording layer
  recent.py       rolling forecast-vs-actual record
  backtest.py     deep multi-season scoring with bootstrap intervals
  web.py          the entire HTML/CSS/JS template
  ghpages.py      public build into docs/, secret guard, publish
docs/             GENERATED. Never hand-edit; gh-publish overwrites it.
forecasts/        GENERATED and gitignored. Working output.
.cache/           gitignored. Holds ERA5 normals — see Performance.
```

## Traps

Every one of these was a real defect that shipped or nearly shipped.

**Terrain sign is zone-dependent.** The terrain-normal component is
`speed × cos(dir − 260°)`, but the same westerly is *upslope* at Matheran and
*descending* at Pune. An unsigned value inverts the rain shadow and reports
Pune as the wettest place in the region. Use `orographic_reading(component,
zone)`, never the raw number.

**Basins follow the coastline, not a rectangle.** `lon > 80 and lat < 23`
calls Chhattisgarh the Bay of Bengal, and the page then announces a marine
system forming while the low sits over central India. `_basin()` interpolates
the real coast.

**ERA5 must be requested explicitly.** The archive default is "seamless": it
serves ERA5 where ERA5 exists and silently splices in ECMWF's own IFS
analysis for recent days. Verifying an ECMWF forecast against an ECMWF
analysis inflates the score exactly where a reader is most likely to look.
`fetch_truth` passes `models=era5`; keep it.

**ERA5 arrives in patches, not with a fixed lag.** On 27 Aug 2026 the archive
held 1–12 Aug and 21 Aug and nothing in between. Any fixed short window can
land inside a hole. `recent.py` scores over 35 days and lists 14.

**`minutely_15` is not real here.** ECMWF has no native 15-minute output for
this location, so Open-Meteo spreads the hourly value across four steps —
measured at Lonavala, 12 of 13 hours had all four steps byte-identical. It
was used, then removed. Quoting an arrival time to the quarter-hour off that
is invented precision.

**A 25 km grid cannot place a convective cell.** 20 mm on one town in fifteen
minutes averages to drizzle across the box. When CAPE, low-level moisture,
dry mid-levels and upslope flow line up, `burst_risk()` says the hourly rates
will understate the experience. The models are not wrong about the total;
they are useless about the afternoon.

**Radar pixels are not available.** RainViewer's tile CDN serves a static
placeholder image to this network — identical bytes for every tile, timestamp
and city. IMD's GIF is reachable but publishes no georeferencing, so mapping
pixels to places would be guesswork presented as an alert. Nothing here reads
a radar pixel; the belts come from the model field and say so.

**Two publishers race.** The laptop task and the GitHub Actions run both push
`docs/`. On a refused push, `publish()` moves to the remote tip and re-commits
rather than rebasing — replaying an identical build produces an empty
cherry-pick that halts git mid-rebase waiting for a `--skip` no unattended run
sends.

**Low model probability does not mean dry.** Measured over 2024–26: in the
0–20% band the ensemble averaged 4% and it rained on 24% of those days. All
the members share one blind spot for locally forced convection. The wording
layer says "nothing organised expected", never "dry", during the monsoon.

## Performance

`.cache/` holds ERA5 climate normals — ten years per site, from the slow
archive API. A cold machine re-downloads them and the weekly outlook can run
past a CI timeout; the workflow caches the directory for that reason.

Multi-site fetches are batched (`SITES_PER_REQUEST`). Nineteen sites in one
request set take about 6 seconds; one request per site took minutes.

## Checking your work

There is no test suite. Verification is by running it:

```bash
python -m py_compile wxagent/*.py
python -m wxagent daily --no-notify        # writes forecasts/ + index.html
python -m wxagent weekly                   # the MMR page
python -m wxagent render                   # rebuild pages from cache, no API
python -m wxagent backtest --deep --cached # re-score without refetching
```

`render` and `--cached` cost no API calls — use them while iterating on
wording or layout.

For page changes, open `forecasts/index.html` in a browser and check the
thing you changed actually rendered. Several bugs here looked correct in the
markup and wrong on screen: an SVG scaling animation whose cells sank through
the ground because `transform-origin` defaults to `0 0`, and labels that
rendered at an effective 4.6 px on a phone. Check both themes and a narrow
viewport.

## Publishing

`python -m wxagent gh-publish` builds the public copy into `docs/`, scans the
**staged content** for secrets, commits and pushes. GitHub Pages serves
`docs/` — allow a few minutes, plus CDN cache.

Never commit `wxagent/local_config.py` (API keys), `logs/`, `forecasts/` or
`.cache/`. The Windy map key is stripped from the public build; the free
Windy point API returns deliberately scrambled data and is never used as a
data source.
