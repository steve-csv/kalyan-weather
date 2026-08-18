# Forecasting doctrine — Kalyan West & Mumbai MMR

The rules this agent applies, and where each comes from. Sources:

- **Guide** — *Practical Rainfall Forecasting: Mumbai • Western Ghats • Pune*
- **Handbook** — *The Weather Interpreter's Handbook: Reading the Skies Over Mumbai, Pune and the Western Ghats*
- Both are distillations of **CAE Oxford Aviation Academy, ATPL Ground Training Series, Book 9: Meteorology (2014)** — principally chapters 1, 2, 4–7, 10–14, 19–21 and 24.

---

## 0. The learning rule — the order everything happens in

> "Never begin with the rain layer. First identify the weather mechanism. Ask: Where is the moisture? What is lifting the air? Is the atmosphere stable or unstable? Is the rain organised enough to persist? Only then examine the model rainfall output." — Guide §0

This is enforced structurally in the code, not left to discipline. `diagnostics.py` computes moisture → lift → instability → organisation, and `daily_rain_spread` runs **last**. Every bulletin prints the ingredients before the rainfall table.

---

## 1. The four questions

### Where is the moisture?

| Signal | Threshold used | Source |
|---|---|---|
| RH at 700 hPa ≥ 70% **and** 850 hPa ≥ 75% | **deep** moisture | Guide §4.4 |
| RH at 700 hPa ≥ 50% | **moderate** | Guide §4.4 |
| Moist at 850 but dry at 700 | **shallow** — cloud stays shallow or entrains dry air | Guide §4.4 |
| Dew point | 24–26 °C = "muggy, classic peak-monsoon Mumbai"; >26 °C = any lift will produce heavy rain | Handbook Ch.3 |
| T − Td spread ≤ 3 °C | air is on the edge of condensing | Guide §4.3 |
| Precipitable water ≥ 55 mm high, ≥ 65 mm very high | abundant *potential* moisture, not guaranteed rain | Guide §4.4 |

Dew point is preferred over RH throughout, because RH swings with temperature while dew point tracks actual water-vapour content (Handbook Ch.3).

Convective cloud base ≈ **125 × (T − Td)** metres — Guide §4.3, valid only for surface-based convection.

### What is lifting the air?

Four mechanisms (Guide §7): orographic, convective, convergence, large-scale ascent.

The agent scores the orographic one numerically, because it dominates this coastline:

**Terrain-normal component** = `wind_speed_850 × cos(wind_direction − 260°)`

260° is taken as the upslope normal for the Sahyadri crest behind Mumbai (roughly N–S ridge axis). Guide §11.1: *"'strong wind' is not enough; examine its direction relative to the terrain."*

| Component | Class | Reading |
|---|---|---|
| ≥ 14.0 m/s | very strong | engine running hard |
| ≥ 10.3 m/s (≈20 kt) | strong | loaded, repeated spells |
| ≥ 7.7 m/s (≈15 kt) | moderate | Handbook Ch.22 Step 4's confirmation threshold |
| ≥ 4.0 m/s | weak | terrain alone won't sustain rain |
| ≥ 0 | negligible | flow parallel to ridge — little lift regardless of speed |
| < 0 | offshore | descent, warming, actively rain-suppressing |

**Critical sign convention.** The same westerly is *upslope* at Matheran and *descending* at Pune. The code takes each site's `zone` (`coastal` / `transition` / `ghat` / `leeward`) and labels the component accordingly — a raw positive number at a leeward site would invert the rain-shadow logic of Handbook Ch.14.

### Is the atmosphere stable or unstable?

CAPE bands — Handbook Ch.24:

| J/kg | Meaning |
|---|---|
| < 500 | fairly stable |
| 500–1500 | moderately unstable — ordinary storms plausible **with a trigger** |
| 1500–2500 | strongly unstable — heavy rain, gusts, hail possible |
| > 2500 | extreme — severe/organised storms possible if a trigger fires |

Shear (925→500 hPa vector difference) decides *what kind* of storm — Handbook Ch.24:

- **< 8 m/s** — pulse cells; intense but collapse on their own outflow within an hour or two
- **8–15 m/s** — longer-lived, can travel, regenerate on outflow boundaries
- **> 15 m/s** — organised, squall-line potential

> **CAPE is potential, not certainty.** High CAPE with no trigger produces nothing; Mumbai gets heavy organised monsoon rain on *modest* CAPE because the lifting is orographic and synoptic, not convective. (Guide §6.4, Handbook Ch.24 — both state this twice, deliberately.)

### Is it organised enough to persist?

Derived from the hourly precipitation shape (Guide §8.1):

- ≥6 h unbroken run, modest rates → **continuous / steady** (deep layered cloud, broad ascent)
- ≥4 h run with ≥12 mm/h peak → **organised with embedded heavy bursts** — the daily total will be dominated by a few intense bursts
- ≥3 wet hours → **intermittent spells**
- otherwise → **isolated showers** (least predictable at suburb level)

---

## 2. Synoptic setting — checked before anything local

Handbook Ch.25 puts the pressure layer first: *"This single layer, checked first, tells you more about the day's character than almost anything else."*

**Monsoon trough** — located as the latitude of minimum MSL pressure along an 80°E transect sampled at 1° from 20–32°N:

- axis ≥ 28°N → **break-leaning**: rain concentrates in the sub-Himalayan belt; the west coast can turn dry for a week (Handbook Ch.13)
- 25–28°N → **transitional**: mixed regime, models disagree most on timing, lean on observations
- < 25°N → **active-leaning**: widespread, often heavy west-coast rain

**Offshore trough** — north–south low along 72.2°E from 15–22.5°N. Depth ≥0.6 hPa relative to the ends of the line counts as present. Guide §12.1: promotes coastal convergence, organises convection, maintains onshore flow. Handbook Ch.13 calls it a more locally specific Mumbai signal than the national onset headline.

**Cross-coast gradient** — mean sea-line pressure minus mean inland-line pressure. Positive means pressure falling inland, strengthening westerly moisture transport (Guide §2.3).

**Somali (Findlater) jet** — the cross-equatorial low-level jet that carries the monsoon's moisture. Measured at 850 hPa in two places: the source core off Somalia (10°N 52°E, 8°N 55°E, 12°N 50°E) and, more importantly, the **mid-Arabian Sea corridor** (14°N 62°E, 17°N 68°E) — the branch that actually reaches the Konkan. Bands are quintiles of the 2024–26 monsoon distribution:

| Corridor jet | Rain days | Mean at Kalyan | Heavy days (of 12) |
|---|---|---|---|
| < 9.1 m/s — very weak | 56% | 8.6 mm | 1 |
| 9.1–13.2 — weak | 70% | 9.9 mm | 0 |
| 13.2–16.0 — moderate | 86% | 13.7 mm | 1 |
| 16.0–19.5 — strong | 98% | 20.4 mm | 3 |
| ≥ 19.5 — very strong | 100% | 32.5 mm | **7** |

The top quintile holds 7 of the 12 heavy days in three monsoons; the top two hold 10 of 12. A slack source behind a fast corridor means a pulse already fading.

**Mid-level dry-air intrusion** — air subsiding over Arabia and the Thar is desert-dry between 700 and 500 hPa. Drawn over the MMR, it is entrained by rising parcels and kills their buoyancy. This is the mechanism behind §1's "moist at 850 but dry at 700 → shallow". Sampled upstream at 22°N 68°E, 24°N 71°E, 20°N 65°E; advection judged by the **600 hPa** flow (not 850 — the monsoon westerly below says nothing about where the dry layer above is going).

| Upstream 600–500 hPa RH | Rain days | Mean at Kalyan | Heavy days (of 12) |
|---|---|---|---|
| < 26.7% — very dry | 60% | 7.7 mm | **0** |
| 26.7–45.5 — dry | 73% | 13.3 mm | 2 |
| 45.5–59.1 — middling | 83% | 15.2 mm | 2 |
| 59.1–72.9 — moist | 95% | 25.3 mm | 5 |
| ≥ 72.9 — very moist | 98% | 23.7 mm | 3 |

The driest quintile produced **no heavy-rain day in 316 days**. When the upstream mid-level airmass is in its driest fifth and being steered our way, a heavy-rain forecast deserves suspicion however good the low-level moisture looks.

**Zone sensitivity (measured, not assumed).** Rank correlation of the corridor jet against observed rainfall, 2024–26: ghat **+0.63** (Malshej +0.68, Igatpuri +0.65, Lonavala +0.61, Matheran +0.59), coastal **+0.59**, transition **+0.55** (Kalyan West), leeward **+0.29** (Pune). A strengthening jet widens the crest-to-shadow gap rather than wetting everywhere equally.

> Both indices peak at **lag 0**, not at the 2–3 days the advection reasoning predicts (corridor: +0.55, +0.47, +0.39, +0.32, +0.26, +0.20 at lags 0–5). They describe the state of the whole monsoon circulation, so they are read from the *forecast* field for each target day — never from today's value projected forward. Neither adjusts any rainfall number; they indicate which way the models are likely to be wrong.

---

## 3. Regime classification

Named **before** the outcome, per Guide §12's system-thinking rule: *system → wind response → moisture transport → lifting zone → expected rain footprint.*

**Monsoon season:**

| Regime | Trigger |
|---|---|
| ACTIVE MONSOON SURGE | terrain-normal ≥ 10.3 m/s **and** deep moisture |
| MODERATE ONSHORE FLOW | ≥ 7.7 m/s with moderate+ moisture |
| BREAK-PHASE CONVECTIVE | < 4 m/s but CAPE ≥ 800 |
| MONSOON BREAK / LULL | < 4 m/s, low CAPE |
| MIXED MONSOON | everything else |

**Other seasons** run on conditional instability plus a trigger (Handbook Ch.4) — CONVECTIVE / THUNDERSTORM RISK, ISOLATED CONVECTION POSSIBLE, SYNOPTIC / NON-SEASONAL RAIN, or DRY / SETTLED.

---

## 4. Models — and the averaging trap

Handbook Ch.21: **ECMWF** is the default highest-trust read (best overall, specifically strong on precipitation); **GFS** updates 4×/day and catches fast-evolving situations sooner but trails on precipitation; **ICON** is a genuine tie-breaker.

Resolution matters here specifically: global models run 9–27 km grids, and the entire windward-wet / leeward-dry contrast across the Ghats plays out over barely a couple of cells. Disagreement between models over Mumbai vs Pune is often *physical*, not random.

> **The trap, stated explicitly.** Guide Case Study F: *"Avoid this: publishing the arithmetic mean of the three model totals as though it were a probability-weighted forecast."*

So the agent **never prints a mean**. It prints the range, the median, and per-model totals. When spread exceeds `max(10 mm, 60% of the high)` it raises a scenario warning explaining that the disagreement usually hinges on something small and badly resolved — an offshore vortex track, or where convection fires.

**Occurrence and amount confidence are computed separately.** Guide Appendix E Q12: agreement mainly increases confidence in *shared features*; exact totals can still be uncertain.

Probability of rain comes from a **31-member GFS ensemble**, not from counting deterministic models — Guide §13.2 is explicit that a true ensemble beats multi-model spread as an uncertainty estimate.

---

## 5. IMD rainfall categories (24-hour totals)

| Category | mm | Practical meaning |
|---|---|---|
| Light | 2.5–15.5 | umbrella weather |
| Moderate | 15.6–64.4 | minor waterlogging in poor-drainage areas |
| Heavy | 64.5–124.4 | genuine disruption — Orange-alert territory |
| Very heavy | 124.5–244.4 | serious flooding risk — Red-alert territory |
| Extremely heavy | ≥ 244.5 | historic-flood level (26 Jul 2005: ~944 mm) |

**Report the category, not the millimetre figure.** Model agreement on category is far more defensible than agreement on a number.

---

## 6. Confidence by lead time — the honest ceiling

Handbook Ch.27 and Guide §18:

| Lead | What is defensible |
|---|---|
| 0–3 h | radar and observations outrank models; specific timing window |
| 3–12 h | rain windows and spell character; exact suburb uncertain |
| 12–48 h | probability, broad timing, footprint, intensity category — genuinely reliable in an active signal |
| 3–5 days | trend and risk window only; hedge the language |
| 6+ days | scenario outlook only; confidence explicitly low |

> **Nobody hits 99%.** Not IMD, not ECMWF. What real skill looks like: 85–95% on broad rain/no-rain 0–2 days out in a strong monsoon signal; good pattern confidence 3–5 days; possibilities rather than facts beyond 7–10 days. (Handbook §2, Guide §25.)

The code enforces this — `assess_confidence` steps every confidence level down as lead time grows, and convective regimes are stepped down again because initiation location is genuinely unpredictable.

---

## 7. Local factors specific to this region

**Kalyan's position.** Guide §11 places the Thane–Kalyan–Karjat belt as "highly variable; can receive coastal bands plus terrain enhancement" — the transition from coast to foothills, with local channelling. In practice Kalyan usually runs *between* the Santacruz and Matheran figures rather than tracking either, which is why the daily bulletin prints the whole gradient.

**Tide compounding.** Handbook Ch.15: Mumbai's gravity-fed stormwater outfalls are throttled or reversed at high tide, so heavy rain landing near high tide is materially more serious than the same total at low tide. **The agent does not synthesise tide times** — fabricating them would breach the honesty doctrine. It raises the check and links a tide table when totals reach the heavy band.

**Sea breeze.** Even during active monsoon flow the daily sea-breeze cycle adds convergence near the coast in the afternoon (Handbook Ch.6, Ch.15) — one reason showers can freshen along the coast on a day that started only moderately wet.

**Cyclone windows.** April–June and October–December (Handbook Ch.17). The bulletin flags these months. Handbook Ch.17 is unambiguous that for anything beyond casual tracking, IMD's official bulletins are authoritative and this kind of tool is not.

---

## 8. Verification — the part that actually builds skill

Guide §26 / Handbook Ch.26. Every forecast is logged **before** the event with its probability, category and confidence, and the forecast columns are never edited afterwards (Guide §15 Step 6).

- Accuracy = (hits + correct dry) / all — *misleading alone*: in a dry spell, always saying "no rain" scores well
- POD = hits / (hits + misses) — how many real events you caught
- FAR = false alarms / (hits + false alarms) — how often your rain call failed
- CSI = hits / (hits + misses + false alarms) — the honest headline number

**Calibration** (Guide §26.2) matters more than sounding certain: of all the days you said 70%, rain should occur about 70% of the time.

---

## 9. Limits — stated because the sources insist on it

- This is **independent interpretation, not an official IMD product**.
- For flooding, lightning, transport and emergency decisions, follow IMD nowcasts, warnings and local authority instructions (Guide §29.5).
- Do not claim 99% local accuracy or guaranteed dry windows.
- Before any commercial use, review Windy and data-provider licensing (Guide §29.5) and build a transparent, verified 60–90 day record first (Guide §29.1).
