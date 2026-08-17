---
name: mumbai-forecast
description: Forecast rain for Kalyan West, Mumbai MMR, the Western Ghats or Pune using the ATPL-derived doctrine in KNOWLEDGE.md. Use whenever the user asks about rain, monsoon, weather, a forecast, whether it will rain, trekking/site/event weather in Mumbai, Thane, Kalyan, Karjat, Matheran, Lonavala, Badlapur, Panvel, Vasai, Navi Mumbai or Pune — or asks to interpret a Windy screenshot, a model disagreement, or an IMD alert for this region.
---

# Mumbai / Kalyan rainfall forecasting

Apply the doctrine in `KNOWLEDGE.md`. Read it before answering anything
non-trivial — it carries the thresholds, the IMD bands and the confidence
ceilings, all sourced to the two guides.

## First: is there already a bulletin?

Check `forecasts/` for today's file before fetching anything. If
`forecasts/YYYY-MM-DD_kalyan_west.md` exists and the question is about today,
read it and answer from it rather than re-running the pipeline.

## Generating a forecast

```bash
python -m wxagent daily --no-notify
```

```bash
python -m wxagent weekly --no-notify
```

For a location the agent doesn't already cover, add a `Site` to
`MMR_SITES` in `wxagent/config.py` with the correct `zone`
(`coastal` / `transition` / `ghat` / `leeward`) — the zone is not cosmetic, it
decides whether a westerly is read as upslope or as descent.

For ad-hoc analysis, import the modules directly rather than reimplementing the
maths:

```python
from wxagent import config as C
from wxagent.sources import fetch_point, fetch_ensemble
from wxagent.diagnostics import diagnose_day
from wxagent.doctrine import assess_confidence
```

## Non-negotiable rules

These come from the source guides and are not stylistic preferences.

1. **Mechanism before outcome.** Never lead with a rainfall number. State
   where the moisture is, what is lifting it, whether it is stable, and whether
   the rain is organised — then give the amount. (Guide §0)

2. **Never average models.** Give the range and the median with per-model
   figures. If ECMWF says 20, GFS 70 and ICON 5, the honest answer is that the
   outcome hinges on something small and badly resolved — say what, and
   separate "does it rain" from "how much". (Guide Case Study F)

3. **Report the IMD category, not the millimetre figure.** Category agreement
   is defensible; a specific number is not. (Handbook Ch.8)

4. **Separate the confidences.** Occurrence, amount and timing get their own
   confidence levels. They are routinely different. (Guide Appendix E Q12)

5. **Respect the lead-time ceiling.** 0–2 days: commit on category. 3–5 days:
   trend and risk window, hedged. 6+ days: scenario only, confidence explicitly
   low. Never give suburb-level or hourly claims beyond ~48 h. (Guide §18)

6. **Zone-aware terrain.** A westerly is upslope at Matheran and *descending*
   at Pune. Getting this backwards inverts the rain shadow. (Handbook Ch.14)

7. **CAPE is potential, not certainty.** High CAPE with no trigger produces
   nothing. Mumbai gets heavy organised rain on modest CAPE because the lifting
   is orographic, not convective. (Handbook Ch.24)

8. **Never claim 99% accuracy or a guaranteed dry window.** Nobody achieves it.
   Say what real skill looks like instead. (Handbook §2, Guide §25)

9. **Never contradict an IMD warning.** Add local context around it; defer to
   it. For flooding, lightning and emergencies, point to IMD and stop.
   (Handbook Ch.25, Guide §29.5)

10. **Never invent observations.** No tide times, no radar readings, no station
    totals unless actually fetched. If a check matters and the data isn't
    available, say so and link where to look.

## Writing the forecast

Follow Guide §27's components in order: location, valid period, probability of
any rain, most likely character, heavy-spell probability, most likely window,
confidence, reasoning, main uncertainty.

Name the *actual* dominant uncertainty — model spread, convective initiation,
band placement, moisture depth or lead time — not a generic disclaimer.

## Verification

After a forecast day passes, offer to record what happened:

```bash
python -m wxagent verify --date 2026-08-11 --mm 23.5 --character "intermittent moderate"
```

```bash
python -m wxagent score
```

Guide §26: accuracy alone misleads — a dry spell rewards always saying "no
rain". POD, FAR and CSI together are the honest picture, and calibration
matters more than sounding certain.

## Windy

Every bulletin emits deep-links per layer. Windy renders ECMWF/GFS/ICON — it
runs no model of its own (Handbook Ch.20), which is why the agent reads those
models directly and uses Windy for the visual check.

If asked about the Windy API key: `python -m wxagent windy-check`. The free
tier returns deliberately scrambled data until the key has a Project
identification set, and the agent refuses that data rather than let it corrupt
a bulletin.
