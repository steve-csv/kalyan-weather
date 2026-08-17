# Kalyan West / Mumbai MMR weather agent

A rainfall forecasting agent that applies the meteorology in your two study
guides — and through them, Oxford Aviation Academy's ATPL Book 9 — to Kalyan
West and the Mumbai Metropolitan Region.

- **Daily** — a rain bulletin for Kalyan West, with a desktop notification
- **Weekly** — a 7-day MMR outlook built from wind-pattern analysis
- **Verification** — a forecast log and POD/FAR/CSI scoring, so the thing
  actually improves

Forecast reasoning follows `KNOWLEDGE.md`, which maps every threshold and rule
back to the passage it came from.

---

## Quick start

```bash
python -m wxagent daily
```

```bash
python -m wxagent weekly
```

Each run writes a dated markdown bulletin to `forecasts/`, regenerates the web
dashboard, and raises a Windows notification carrying the headline.

## The web dashboard

Double-click **`Open forecast.cmd`**, or bookmark
`forecasts/index.html` directly. Clicking the morning notification opens it too.

Two pages, cross-linked:

| Page | What it shows |
|---|---|
| `forecasts/index.html` | Today at Kalyan West — rain chance, IMD category, hour-by-hour chart, model comparison, coast→crest→rain-shadow gradient, days ahead, full ingredient table |
| `forecasts/mmr.html` | The MMR week — orographic forcing chart, monsoon trough track, an 11-site × 7-day rainfall matrix, day-by-day narratives |

Both are **single self-contained files** — no server, no build step, no external
requests. They work offline and rewrite themselves in place on every scheduled
run, so the bookmark is always current.

Design notes, since charts can mislead easily:

- Rainfall is *magnitude*, so it takes a single blue hue rather than
  categorical colors; the coast→crest gradient uses an ordinal ramp validated
  to clear the contrast floor in both light and dark mode. Pune, being the
  **control** rather than a step on that gradient, is drawn in neutral grey.
- IMD severity uses the reserved status palette and **always ships with an icon
  and a text label** — color never carries the meaning alone.
- The model chart shows three bars and no average bar, for the reason given
  below.
- Every chart has hover tooltips, and every number in a chart also appears in a
  table, so nothing is gated behind color or pointer precision.
- Light and dark are both explicitly styled; the toggle persists.

### Run it on a schedule

```powershell
powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1
```

Registers two tasks under your own user account (no admin needed):

| Task | When |
|---|---|
| Kalyan Weather Agent - Daily | 06:15 every day |
| Kalyan Weather Agent - Weekly | 07:00 every Sunday |

Both use `StartWhenAvailable`, so a run missed because the PC was off fires
shortly after you next log on. To remove them:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1 -Uninstall
```

---

## Where the data comes from

Windy **runs no model of its own** — Handbook Ch.20: *"it doesn't run its own
physics, it takes the output of models like ECMWF, GFS and ICON and renders it
as an interactive, animated map."*

So the agent reads those same models directly, at the pressure levels the
guides actually use:

| Source | What for |
|---|---|
| **ECMWF IFS, GFS, ICON** (via Open-Meteo) | the Ch.21 three-way comparison, RH at 925/850/700 hPa, winds by level, CAPE/CIN, dew point, precipitable water |
| **GFS ensemble, 31 members** | true probability of exceedance — Guide §13.2 prefers this over multi-model spread |
| **MSL pressure transects** | monsoon trough axis (20–32°N along 80°E) and the offshore trough (15–22.5°N along 72.2°E) |
| **windy.com deep-links** | every bulletin links the exact layer, level and location it's discussing, for the visual check |
| **Windy Point Forecast API** *(optional)* | supplementary cross-check only |

No API key is required for any of the above except the last.

### On the Windy API key

Free-tier Windy serves **GFS alone over India**, so it cannot support the
three-model comparison the guides require — that's why it's supplementary.

More importantly: **until a key has a "Project identification" set, Windy
returns deliberately scrambled data**, with the warning *"This data is randomly
shuffled and slightly modified."* That is worse than no data, because it looks
entirely plausible. The agent detects that warning and discards the response.

```bash
python -m wxagent windy-check
```

To fix a scrambled key: at <https://api.windy.com/keys>, edit the key, fill in
Project identification (e.g. `local.kalyan-wx-agent`), and re-run the check.
The **RESTRICTION MISSING** badge is the symptom.

The key lives in `wxagent/local_config.py`, or in a `WINDY_API_KEY`
environment variable if you prefer. Keep that file off any shared drive.

---

## What's in a daily bulletin

0. **⚡ Major weather shifts** — the transitions worth interrupting you for:
   sharp jumps in rain, surge onset, break onset, dry windows opening, heavy
   and very-heavy days, heatwave/cold criteria, approaching systems. A settled
   week produces none; the turn produces an alert. The most severe one also
   becomes the morning notification headline.
0b. **In plain English** — one line per day, no jargon, with what to actually
   do. Repeated sentences are suppressed so the days that differ stand out.
1. **Forecast** — probability, range, IMD category, character, window, and
   three separate confidence levels
2. **Mechanism** — the named regime and the physical chain producing it
3. **Synoptic setting** — monsoon trough position, offshore trough,
   cross-coast gradient
4. **Ingredients** — the Guide Appendix B checklist, filled in: moisture depth,
   terrain-normal wind, CAPE/CIN, shear, organisation
5. **Model comparison** — per-model totals and the range, deliberately with
   **no mean**
6. **Timing** — by daypart, plus an hourly strip
6b. **Now — next few hours** — live IMD Mumbai radar (MAX-Z and Surface Rain
   Intensity embedded, plus PPI/accumulation/Doppler links), a RainViewer
   animation, the model's next six hours, and Guide §16.1's five-question
   radar scan. It deliberately does **not** compute arrival times from radar
   images — see "Honest limits".
6c. **Systems** — low pressure areas tracked across a 2D pressure grid over the
   Arabian Sea, peninsula and Bay of Bengal, with movement, closest approach
   and what each means for the Konkan flow. Seasonal features (the heat low,
   the monsoon trough axis) are filtered out rather than reported as arriving
   systems. Offshore trough detected on a dedicated 0.5° coastal grid.
6d. **Heat and cold** — 7 days against IMD's exact criteria (Handbook Ch.18/19),
   including the two-consecutive-day rule, plus heat index because Ch.19 is
   explicit that humid coastal heat risk is real when no heatwave is declared.
7. **Terrain gradient** — coast → transition → crest → rain shadow, with an
   automatic verdict on whether the rain shadow is intact
8. **Decisions** — waterlogging, tide check, lightning, burst intensity
9. **Main uncertainty** — the actual dominant one, not a generic disclaimer
10. **Windy links** — worked in Ch.25's morning-routine order, pressure first,
    rain last

---

## Verification

The part that turns practice into skill (Guide §26, Handbook Ch.26). Every
forecast is logged before the event; forecast columns are never edited
afterwards.

```bash
python -m wxagent verify --date 2026-08-11 --mm 23.5 --character "intermittent moderate"
```

```bash
python -m wxagent score
```

Gives accuracy, POD, FAR, CSI and a probability-calibration table. Guide §26.1
is worth remembering: accuracy alone misleads, because in a dry spell "no rain"
every day scores well.

---

## Layout

```
wxagent/
  config.py        locations, thresholds, IMD bands — every constant cited
  sources.py       data acquisition; Windy testing-tier guard
  diagnostics.py   the meteorology as computation
  doctrine.py      forecast composition, confidence, uncertainty
  synoptic.py      trough diagnosis from the pressure field
  report.py        markdown rendering
  windy.py         layer deep-links
  web.py           the HTML dashboard (both page layouts)
  daily.py         Kalyan West bulletin
  weekly.py        MMR wind-pattern outlook
  verify.py        forecast log and scoring
  notify.py        Windows notification
  local_config.py  your Windy key — keep private
forecasts/         dated bulletins + index.html & mmr.html
logs/              forecast_log.csv
Open forecast.cmd  opens the dashboard in your browser
KNOWLEDGE.md       the doctrine, fully sourced
.claude/skills/    lets Claude Code answer ad-hoc questions to the same rules
```

---

## Honest limits

Taken directly from your sources, because they insist on it:

- **Nobody hits 99%** — not IMD, not ECMWF. Real skill is 85–95% on broad
  rain/no-rain 0–2 days out in a strong monsoon signal, good pattern confidence
  at 3–5 days, and possibilities rather than facts beyond 7–10 days.
- **This is independent interpretation, not an official IMD product.** For
  flooding, lightning, transport and emergency decisions, follow IMD nowcasts
  and local authority instructions.
- **The agent never invents observations.** It does not synthesise tide times,
  radar readings or station totals. Where a check matters and the data isn't
  available, it says so and links where to look.
- **No radar echo tracking.** Deriving reflectivity, motion vectors and arrival
  times from scraped radar rasters means inverting a colour scale and ignoring
  beam geometry — the output would look precise and be unreliable. Guide §16.2
  lists exactly why the raw image misleads. The bulletin shows the animation
  and the five questions; IMD's own nowcasts are the authority.
- **Heat/cold normals are ERA5-derived, not IMD's official normals**, so every
  departure figure carries that difference. IMD also needs the criteria at two
  or more stations in a subdivision before declaring — a single point can't
  make that call, and the agent says so rather than declaring one.
- **Storm and cyclone intensity labels are approximations** from 850 hPa wind,
  not IMD's surface-wind classification. Handbook Ch.17 is unambiguous that
  cyclone calls belong to IMD.
- **Before any commercial use** (Guide §29): build a transparent, verified
  60–90 day record first, publish misses as well as hits, and review Windy and
  data-provider licensing.
