"""
Sam's notes: the explanations behind the forecast, in Sam's own voice.

The forecast payload tells Sam WHAT the weather is doing. This tells him WHY,
and how to read the page - the things worked out while this agent was being
built and argued over, which otherwise lived only in a conversation:

  * why the radar outranks the models for the present tense, and why the two
    Mumbai radars can disagree (clutter, a beam weakened by heavy rain, a
    radar that is simply not seeing properly);
  * the kinds of rain - monsoon westerly, easterly storms, the withdrawal's
    heat-built storms, low pressure systems, western disturbances - and why
    the same millimetres mean different weather from each;
  * how lows are tracked, grouped into one event, and told apart;
  * what the percentages, confidence words and scorecard actually mean,
    including where this agent is known to be weak.

Used two ways. The rule-based Sam (GitHub Pages, offline, or when a viewer
declines) matches a question against `keys` and answers with `a`. On the
claude.ai page, the whole set goes to Claude as reference alongside the
day's data, so the same knowledge answers questions no rule anticipated.

Rules for writing an entry: plain English, first person as Sam, no citations,
no invented numbers. Anything true only on a date says the date. Nothing
personal about anyone - this ships in a public page.

`keys` are JavaScript regular-expression sources, matched case-insensitively
against the reader's question.
"""

from __future__ import annotations

KB: list[dict] = [
    # ---- how Sam works --------------------------------------------------
    {
        "id": "about_sam",
        "keys": [r"\bwho are you\b", r"\bwhat are you\b", r"how do you work",
                 r"are you (an )?(ai|bot|human|real)", r"\bsam\b.*\b(accurate|trust)"],
        "t": "Who Sam is and how he answers",
        "a": "I'm Sam — I read this page's forecast so you don't have to dig "
             "through it. Everything I say comes from the data on this page "
             "(the models, the radar, the systems tracker) and from notes on "
             "how to read it. On the claude.ai copy I think with Claude, if "
             "you allow it; elsewhere I answer from built-in rules. Either "
             "way I'm not IMD: for official warnings, IMD and the civic "
             "authorities are the word that counts.",
    },
    {
        "id": "snapshot",
        "keys": [r"\b(old|stale|outdated|updated|refresh|latest|current)\b.*\b(page|forecast|data)\b",
                 r"when was (this|the page|it) (made|built|updated|issued)"],
        "t": "The page is a snapshot",
        "a": "This page is a snapshot, built at the issue time shown at the "
             "top. It doesn't update itself, so the older it is, the less "
             "the 'right now' parts mean — the radar especially. The day-by-"
             "day outlook ages more gently than the present tense does.",
    },

    # ---- radar ----------------------------------------------------------
    {
        "id": "radar_vs_models",
        "keys": [r"why (trust|believe|use) (the )?radar", r"radar (vs|versus|or) (the )?model",
                 r"model(s)? (say|said) .*radar", r"radar.*(different|disagree).*model"],
        "t": "Why radar outranks the models for right now",
        "a": "For the next hour or two, trust the radar over any model. The "
             "radar is an observation — the beam is reporting what's in the "
             "air right now. A model is an opinion about the whole day, drawn "
             "on a grid about 25 km across, so it smears a shower over a big "
             "box and often misses one that has just formed. On 12 Sep the "
             "model said nothing for the hour while the radar had rain over "
             "Kalyan. The models earn their keep for later today and the "
             "coming days; the radar owns the present.",
    },
    {
        "id": "cband",
        "keys": [r"c[- ]?band", r"s[- ]?band", r"veravali", r"which radar",
                 r"two radars", r"colaba radar", r"dwr"],
        "t": "The two Mumbai radars",
        "a": "Mumbai has two IMD Doppler weather radars. The C-band at "
             "Veravali, in Andheri, is the one I lead with: it's about 30 km "
             "from Kalyan against Colaba's 50, its picture is roughly three "
             "times sharper, and its colour scale steps in single dBZ rather "
             "than big 15-dBZ jumps, so light and moderate rain are told "
             "apart. Colaba's S-band uses a longer wavelength. "
             "I keep it as a second opinion and as the fallback when the "
             "C-band can't be read or its scan is out of date.",
    },
    {
        "id": "dbz",
        "keys": [r"\bdbz\b", r"reflectivity", r"mm ?/ ?h(ou)?r", r"rain rate",
                 r"colou?r(s)? (on|in) (the )?radar", r"what do the colou?rs"],
        "t": "Reading the radar colours",
        "a": "The colours are reflectivity, in dBZ — how strongly the rain "
             "bounces the beam back. I turn that into a rain rate with the "
             "standard Marshall-Palmer formula: about 20 dBZ is 0.6 mm/hr, "
             "30 is 3, 40 is 12 and 50 is nearly 50 mm/hr. Navy and blue are "
             "light rain, white is moderate to heavy, yellow and orange are "
             "downpours. The formula is an average over many storms, so trust "
             "the where and the whether far more than the exact millimetres.",
    },
    {
        "id": "radar_time",
        "keys": [r"(radar|scan).*(old|time|age|when)", r"how (old|recent|fresh).*radar",
                 r"scanned", r"published"],
        "t": "How old the radar picture is",
        "a": "IMD's file time is not the scan time. Both Mumbai radars' files "
             "appear 10 to 25 minutes after the scan, and the C-band file gets "
             "re-uploaded unchanged every few minutes, so a radar that stopped "
             "hours ago can look fresh. I read the scan time printed on the "
             "C-band image itself. For Colaba I can only see the upload time, "
             "so the page says 'published' there, not 'scanned'. Past 45 "
             "minutes I stop treating a scan as 'now'.",
    },
    {
        "id": "clutter",
        "keys": [r"clutter", r"unconfirmed", r"false echo", r"hill.*radar",
                 r"radar.*hill", r"fake rain", r"not really raining"],
        "t": "Clutter: echoes that aren't rain",
        "a": "Clutter is the radar seeing hills, towers or buildings instead "
             "of rain. Near the Veravali dish it shows up as grainy specks "
             "over the Borivali national park hills and the Parsik hills "
             "near Vashi — they don't move between scans, and rain always "
             "does. I read the C-band in roughly 1 km squares and only count "
             "a square if the ones around it carry echo too, which removes "
             "the specks but keeps any real shower 2 km or more across. When "
             "only one radar sees something strong, the page calls it "
             "unconfirmed rather than rain.",
    },
    {
        "id": "attenuation",
        "keys": [r"attenuat", r"blocked", r"weaken(ed)? beam", r"behind (the |a )?(storm|core)"],
        "t": "The C-band's weak spot",
        "a": "The C-band's weakness is heavy rain itself. Its beam loses "
             "power passing through a downpour, so rain behind a heavy core, "
             "as seen from Andheri, can look weaker than it is. Colaba's "
             "longer-wavelength S-band barely suffers from that, which is why it's kept as the "
             "check. When Colaba shows rain the C-band misses, the page says "
             "the C-band's beam may be blocked.",
    },
    {
        "id": "radars_disagree",
        "keys": [r"radars? (disagree|differ|don.?t match)", r"colaba.*(nothing|empty|blind)",
                 r"why (does|is) .*s[- ]?band"],
        "t": "When the two radars disagree",
        "a": "Sometimes one radar simply isn't seeing properly. On 22 Sep, "
             "Colaba showed one speck in 250 km while the C-band had an "
             "organised rain band over the sea. That evening Colaba saw only "
             "a third of the C-band's echo and missed most of a storm complex "
             "over the Ghats. So I only let one radar confirm or doubt the "
             "other when it sees at least half as much rain and their scans "
             "are within 15 minutes of each other. Otherwise the page just "
             "tells you they disagree.",
    },

    # ---- kinds of rain --------------------------------------------------
    {
        "id": "rain_types",
        "keys": [r"(kind|type|sort)s? of rain", r"what (drives|causes|is driving)",
                 r"\bdriver\b", r"where.*rain.*come from"],
        "t": "The kinds of rain",
        "a": "Twelve millimetres from the monsoon and twelve from a "
             "thunderstorm are different days. Monsoon rain comes in on a "
             "westerly wind and falls steadily for hours, hardest on the "
             "windward Ghats. Easterly rain comes the other way, mostly as "
             "afternoon and evening thunderstorms, and hits the inland belts "
             "first. A low pressure system gives widespread, long rain that "
             "even reaches Pune. When the monsoon pulls back, a northerly "
             "brings heat-built Ghat storms. In winter a western disturbance "
             "can bring cloud but rarely rain here. The 'What kind of rain' "
             "card names today's driver.",
    },
    {
        "id": "sw_monsoon",
        "keys": [r"south ?west monsoon", r"\bsw (monsoon|wind)", r"westerl(y|ies)",
                 r"monsoon wind", r"onshore"],
        "t": "Southwest monsoon rain",
        "a": "The monsoon's own wind blows in off the Arabian Sea from the "
             "west or southwest, loaded with moisture. When it meets the "
             "Western Ghats it's forced upward, cools, and rains — so the "
             "windward face above Kalyan, Igatpuri and Matheran gets the most, "
             "the coast gets a lot, and Pune, behind the crest, stays "
             "comparatively dry. It's usually steady rain that lasts for "
             "hours rather than short bursts.",
    },
    {
        "id": "easterly",
        "keys": [r"easterl(y|ies)", r"east wind", r"from the east"],
        "t": "Easterly rain",
        "a": "An easterly is the monsoon wind running backwards, and the "
             "terrain works backwards with it. The east face of the Ghats "
             "becomes the windward side and the Konkan — Mumbai, Thane, "
             "Kalyan — sits in the lee. So the storms usually build over the "
             "inland belts (Karjat, Badlapur, the plateau) in the afternoon "
             "and evening, short, heavy, very local and with lightning, while "
             "the coast can stay dry until something drifts down to it.",
    },
    {
        "id": "withdrawal",
        "keys": [r"withdraw", r"pull(ing)? back", r"monsoon (end|over|leav|retreat)",
                 r"northerl(y|ies)", r"october"],
        "t": "The monsoon pulling back",
        "a": "In late September and October the wind a kilometre and a half "
             "up turns northerly — off the land instead of in from the sea. "
             "That's the monsoon withdrawing. Nothing blows rain in any more, "
             "but the air is often still humid and the sun is strong, so rain "
             "gets built in place: storms fire over the Ghats in the "
             "afternoon and drift out in the evening, heavy where they land "
             "and dry in between. When the air finally dries out too, the "
             "rain stops and the October heat arrives.",
    },
    {
        "id": "thunderstorm",
        "keys": [r"thunder", r"lightning", r"storm cell", r"cloudburst", r"hail",
                 r"\bcape\b", r"instab"],
        "t": "Thunderstorms",
        "a": "A thunderstorm needs moist air, energy (CAPE, the fuel for "
             "rising air) and something to set it off — daytime heating, the "
             "hills, or colliding winds. Lots of energy is a risk, not a "
             "promise: plenty of high-energy days produce nothing. When "
             "storms do fire they're patchy — one suburb gets twenty minutes "
             "of downpour while the next stays dry — and lightning is the "
             "real danger. With weak upper winds they're short-lived 'pulse' "
             "storms that die on their own outflow within an hour or two.",
    },
    {
        "id": "lightning_safety",
        "keys": [r"lightning safe", r"(safe|safety).*(storm|thunder|lightning)",
                 r"struck", r"what (should|do) i do.*(storm|thunder)"],
        "t": "Staying safe from lightning",
        "a": "If you can hear thunder, you're close enough to be struck. Get "
             "inside a proper building or a hard-top car and wait about half "
             "an hour after the last thunder. Stay off open ground, rooftops "
             "and ridges, away from lone trees, water and metal fences. On a "
             "trek, get down off the summit and exposed ridges early — Ghat "
             "storms build fast in the afternoon.",
    },
    {
        "id": "western_disturbance",
        "keys": [r"western disturbance", r"\bwd\b", r"winter rain"],
        "t": "Western disturbances",
        "a": "A western disturbance is a winter storm system moving east "
             "across north India. It brings cloud, cooler days and sometimes "
             "light rain there, but it almost never reaches this coast. The "
             "page can only infer one from the high-level winds, because the "
             "pressure grid it reads stops at 28°N and these systems sit "
             "north of that.",
    },

    # ---- terrain --------------------------------------------------------
    {
        "id": "rain_shadow",
        "keys": [r"rain ?shadow", r"\bpune\b.*(dry|less|why)", r"why.*\bpune\b",
                 r"leeward", r"\blee\b"],
        "t": "The rain shadow",
        "a": "Air rising up the western face of the Ghats drops its rain "
             "there; once it's over the crest it sinks, warms and dries out. "
             "That's why Pune, behind the hills, gets far less than Matheran "
             "or Lonavala on a monsoon day. Two things break the pattern: a "
             "low pressure system lifts air over a whole region, so Pune gets "
             "caught up too, and an easterly flips it, making the east face "
             "the wet side.",
    },
    {
        "id": "orographic",
        "keys": [r"orograph", r"terrain", r"\bghats?\b.*(more|wetter|why)",
                 r"why.*\bghats?\b", r"upslope", r"windward"],
        "t": "Why the hills get more rain",
        "a": "When wind blows straight at a mountain range, the air has to "
             "climb, and rising air cools and rains. The stronger and more "
             "head-on the westerly, the more the Ghats squeeze out. A wind "
             "running along the range instead of into it gives almost no "
             "lift, however strong it is. That's why the page shows a "
             "'terrain-normal' wind: the part blowing straight at the hills.",
    },
    {
        "id": "kalyan_zone",
        "keys": [r"kalyan.*(zone|belt|different|special)", r"transition (belt|zone)"],
        "t": "Where Kalyan sits",
        "a": "Kalyan is in the transition belt — between the coast and the "
             "Ghats. It gets less than the crest in a westerly but more than "
             "the island city on many days, and it's close enough to the "
             "hills that Ghat storms drifting out in the evening often reach "
             "it. Badlapur and Karjat, a little further in, usually feel the "
             "hills' effect first.",
    },

    # ---- lows and systems ----------------------------------------------
    {
        "id": "lpa",
        "keys": [r"\blpa\b", r"low pressure", r"depression", r"cyclon",
                 r"well[- ]marked", r"\bsystem\b"],
        "t": "Lows, depressions and cyclones",
        "a": "A low pressure area is a patch where air is rising and pressure "
             "is lower than around it. As the winds circling it strengthen, "
             "IMD upgrades it: a depression at 17–27 knots, a deep depression "
             "at 28–33, a cyclonic storm from 34. A low doesn't need to come "
             "close to matter. A Bay of Bengal system crossing central India "
             "can pull the monsoon westerlies harder onto this coast from "
             "800 km away. For anything cyclone-related, IMD's bulletins are "
             "the authority.",
    },
    {
        "id": "event_grouping",
        "keys": [r"same (low|lpa|system)", r"(multiple|several|many|two|different) (lows|lpas|systems)",
                 r"one event", r"sub[- ]?point", r"possibilit"],
        "t": "Why one low shows as one event",
        "a": "One low can be detected several times — at different distances, "
             "on different days, as its centre wobbles. Listing each detection "
             "would make one system look like three. So the page groups "
             "detections into a single event and lists the possibilities under "
             "it as sub-points. Two centres only count as separate lows when "
             "there's a ridge of higher pressure, at least 1 hPa, between "
             "them — being far apart isn't enough on its own.",
    },
    {
        "id": "two_lows",
        "keys": [r"gujarat", r"two trajector", r"separate (low|system)",
                 r"\bcol\b", r"ridge between"],
        "t": "Telling two lows apart",
        "a": "The test is whether pressure rises between them. If you can walk "
             "from one centre to the other without climbing, it's one "
             "stretched system; if there's a ridge in between, they're two. "
             "On 20 Sep that's how the Gujarat and Maharashtra lows came out "
             "as two separate systems: a 2–3 hPa ridge sat between them. They "
             "weren't one low taking two possible paths.",
    },
    {
        "id": "system_motion",
        "keys": [r"stationary", r"not moving", r"(which way|where) is (it|the low) (moving|going)",
                 r"track(ing)?", r"\bmoving\b"],
        "t": "How confident a track is",
        "a": "The pressure grid I track lows on has points about 210 km "
             "apart, so a centre that seems to shift less than that may just "
             "be rounding. I only call a low 'moving' when it has travelled "
             "clearly more than one grid step, fairly directly, over at least "
             "18 hours. Otherwise the page says slow, wandering, or that the "
             "motion can't be resolved yet.",
    },
    {
        "id": "monsoon_trough",
        "keys": [r"monsoon trough", r"\btrough\b", r"active (spell|phase|monsoon)",
                 r"\bbreak\b", r"\bactive\b"],
        "t": "Active and break monsoon",
        "a": "The monsoon trough is a line of low pressure across north "
             "India. When it sits in its normal place or further south, the "
             "sea winds blow hard onto the west coast — an active spell, with "
             "widespread and often heavy rain. When it shifts north to the "
             "Himalayan foothills, the engine idles — a break: sunshine, "
             "patchy afternoon showers, long dry gaps. An offshore trough along "
             "the coast squeezes sea air together and can turn a damp wind "
             "into real rain.",
    },
    {
        "id": "upstream",
        "keys": [r"somali", r"\bjet\b", r"dry air", r"mid[- ]level", r"upstream"],
        "t": "What feeds the monsoon",
        "a": "Two things upstream decide how much the coast can get. The "
             "Somali jet is the low-level river of wind that carries Arabian "
             "Sea moisture to us — when it's weak, heavy rain almost never "
             "happens. Dry air a few kilometres up chokes clouds before they "
             "grow tall, even when the ground feels muggy. The page reads "
             "both each day.",
    },
    {
        "id": "climate_drivers",
        "keys": [r"el ni[nñ]o", r"la ni[nñ]a", r"\benso\b", r"\biod\b", r"indian ocean dipole",
                 r"\bmjo\b", r"\bmiso\b", r"madden"],
        "t": "The background climate drivers",
        "a": "El Niño, the Indian Ocean Dipole and the MJO tilt the odds over "
             "weeks to a whole season; they don't decide any single day. A "
             "strong El Niño leans against our monsoon, and a positive IOD "
             "leans for it. The MJO and MISO are pulses of cloud that travel "
             "east and north over a few weeks and can switch an active or "
             "break spell on. Read them as a tilt, not a verdict.",
    },

    # ---- reading the numbers -------------------------------------------
    {
        "id": "probability",
        "keys": [r"(chance|probability|percent|%)", r"\bmembers?\b", r"ensemble"],
        "t": "What the rain chance means",
        "a": "The percentage is the share of 31 runs of the GFS ensemble, "
             "each started slightly differently, that give at least 2.5 mm at Kalyan West "
             "that day — IMD's line for a rainy day. It's about whether it "
             "rains measurably at that point, not how much or for how long. "
             "At the top of the scale it's trustworthy: near-unanimous runs "
             "verified about 98% of the time in three past monsoons.",
    },
    {
        "id": "low_chance",
        "keys": [r"(said|forecast).*(dry|unlikely).*(rain|rained)", r"why did it rain",
                 r"low chance", r"isolated showers possible", r"nothing organised"],
        "t": "Why a low chance can still mean rain",
        "a": "In the monsoon a low number means 'nothing organised', not 'dry'. "
             "Over three seasons, on days the models gave under 20%, it still "
             "rained on 24% of them. The reason is a shared blind spot: all "
             "the models see the same big-picture flow, so when rain is "
             "forced locally — a sea-breeze line, one cell anchored on a hill "
             "— they all miss it together, and agreement gets mistaken for "
             "certainty. Outside the monsoon, a low number really does mean "
             "dry.",
    },
    {
        "id": "no_average",
        "keys": [r"average", r"\bmean\b", r"(models?) (disagree|differ)", r"\becmwf\b",
                 r"\bgfs\b", r"\bicon\b", r"which model"],
        "t": "Why the page never averages the models",
        "a": "ECMWF, GFS and ICON are compared side by side and never "
             "averaged. When they disagree, it usually means the outcome "
             "hinges on something small — exactly where a storm fires, or the "
             "track of an offshore swirl — and averaging hides that. The "
             "honest reading is: trust the part they agree on (does it rain) "
             "more than the part they don't (how much). None of them is "
             "reliably best here; ECMWF actually had the most false alarms at "
             "Kalyan in the backtest.",
    },
    {
        "id": "confidence",
        "keys": [r"confiden", r"how sure", r"certain", r"reliable"],
        "t": "The three confidences",
        "a": "The page gives three separate confidences: whether it rains, "
             "how much, and when. They often differ — 'high confidence on "
             "rain, low on amount' is common on convective days, because the "
             "models agree something will fall but not where the heavy cells "
             "land. Plan around the one that matters to you.",
    },
    {
        "id": "imd_bands",
        "keys": [r"light rain", r"moderate rain", r"heavy rain", r"very heavy",
                 r"how much is", r"imd (scale|categor|band)"],
        "t": "IMD's rain bands",
        "a": "IMD's daily bands: light rain is 2.5–15.5 mm, moderate "
             "15.6–64.4, heavy 64.5–115.5, very heavy 115.6–204.4, and "
             "extremely heavy above that. The page treats the band as the "
             "forecast and the millimetres as a guide — a model's exact "
             "figure is never as solid as it looks.",
    },
    {
        "id": "character",
        "keys": [r"steady", r"continuous", r"intermittent", r"spells",
                 r"burst", r"showers field"],
        "t": "Steady rain or showers",
        "a": "The same total can fall as hours of drizzle or one violent "
             "quarter-hour. I judge the character from how much of the day's "
             "rain lands in its busiest hour. Around half in one hour is a "
             "shower; a small share means steady layer cloud. The models' own "
             "'showers' label can't be used for this: ECMWF reports zero "
             "showers always and GFS and ICON put nearly everything there.",
    },
    {
        "id": "alerts_logic",
        "keys": [r"why.*(alert|warning).*(monday|tuesday|wednesday|thursday|friday|saturday|sunday|day)",
                 r"(alert|warning).*wrong day", r"surge"],
        "t": "Why an alert can name a different day from the wettest",
        "a": "A pattern alert — a monsoon surge, say — is triggered by the "
             "winds and moisture changing, not by the rain total. The pattern "
             "can switch on a day before the heaviest rain arrives, so the "
             "alert names when it starts and says separately which day is "
             "wettest. If the two look out of step, that's the reason.",
    },

    # ---- accuracy and data ---------------------------------------------
    {
        "id": "accuracy",
        "keys": [r"accura", r"track record", r"how good", r"hit rate",
                 r"(right|wrong) (last|yesterday)", r"\bscore\b", r"miss(ed)?\b"],
        "t": "How accurate this forecast is",
        "a": "Every forecast is checked against what actually fell, misses "
             "included — the scorecard on the page shows the last month. Over "
             "May–August 2026 it called rain or no-rain right most of the "
             "time, and beat 'same as yesterday' mainly by raising fewer "
             "false alarms. Its known weak spot is heavy rain: it caught only "
             "2 of 5 heavy days in that test. So lean on it for 'will it "
             "rain', and be careful with 'how hard'.",
    },
    {
        "id": "what_fell",
        "keys": [r"how much (did it|has it) rain", r"(rain|rainfall) (yesterday|last night)",
                 r"what fell", r"\bgauge", r"observed"],
        "t": "The 'what fell' figures",
        "a": "The 'what fell' table is model analysis, not rain gauges. IMD's "
             "gauge network needs a data agreement and the city's gauges have "
             "no open feed. The analysis gets the pattern right but runs low "
             "on convective days — on 11 Aug 2026 gauges had Borivali at "
             "84 mm in 12 hours while the analysis said 29 mm for the whole "
             "day. Read the ranking, not the millimetres; any real gauge "
             "beats it.",
    },
    {
        "id": "sources",
        "keys": [r"(data|forecast) (come from|source)", r"where do you get", r"\bwindy\b",
                 r"open[- ]?meteo", r"\bmodels?\b.*\buse"],
        "t": "Where the data comes from",
        "a": "The forecast uses the three big global models — ECMWF, GFS and "
             "ICON — plus the 31-member GFS ensemble for the rain chance, and "
             "ERA5 reanalysis to check past days. The present tense comes from "
             "IMD's two Mumbai radars. Windy shows these same models; it "
             "doesn't run one of its own.",
    },

    # ---- practical ------------------------------------------------------
    {
        "id": "heat",
        "keys": [r"feels like", r"heat ?index", r"heatwave", r"humid", r"sultry",
                 r"muggy"],
        "t": "Humid heat and heatwaves",
        "a": "In a humid coastal city, feels-like and heatwave are different "
             "things. IMD declares a heatwave on the actual temperature (37°C "
             "on the coast, 40°C inland, well above normal), so a 33°C day "
             "with sticky air isn't one — but it can feel like 42°C and wear "
             "you down. That's why the page warns about oppressive humid heat "
             "even when there's no heatwave.",
    },
    {
        "id": "tide",
        "keys": [r"\btide", r"waterlog", r"flood", r"high tide"],
        "t": "Rain and high tide",
        "a": "Mumbai's storm drains empty into the sea, so heavy rain at high "
             "tide can't get away and the streets flood far faster. Watch the "
             "combination, not either one alone. IMD's tide table is linked "
             "on the page. For flooding, follow the civic authorities' "
             "updates.",
    },
    {
        "id": "trek",
        "keys": [r"\btrek", r"\bhike", r"hiking", r"fort\b", r"waterfall", r"picnic"],
        "t": "Planning a trek",
        "a": "For a Ghat trek, go early. On storm days the hills fire first, "
             "usually from early afternoon, so aim to be off exposed tops and "
             "ridges by then. Streams can rise within minutes after a cell "
             "upstream, even if it's dry where you are, so don't cross fast "
             "water. Check the day's rain type on the page: steady monsoon "
             "rain means a wet but predictable day, while thunderstorms mean "
             "a dry morning and a dangerous afternoon.",
    },
    {
        "id": "official",
        "keys": [r"\bimd\b.*(alert|warning|orange|red|yellow)", r"(orange|red|yellow) alert",
                 r"official"],
        "t": "Official warnings",
        "a": "I'm not an official source. IMD's colour-coded warnings — "
             "yellow, orange, red — and the civic authorities' instructions "
             "are what to act on for safety. The page's own alerts are a "
             "forecaster's read of the pattern, meant to help you plan, not "
             "to replace them.",
    },
]
