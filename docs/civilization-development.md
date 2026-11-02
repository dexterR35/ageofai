# Continuous civilization development

This is the first build of the expanded Age of AI, agreed on 1 November 2026.
It is a civilization simulation for spectators, separate from any future AoE-style project.

## Agreed direction

- Cities, discoveries and history remain as societies advance. A Wonder does not reset this mode.
- Four civilizations begin on a 256×256 world, four times the classic map's area.
- Normal pace is five real minutes per game hour (12×), revised from the initial 60× setting. Without adaptive slowdowns, one game day takes two real hours and one calendar year about 30.4 real days while running.
- An AI leader has separate economy, research and government offices staffed by actual citizens. Experience, happiness, aging and departure affect appointments. Competent officeholders can remain after review.
- Models can create fictional traditions and short cultural works. Real-world religions are not the starting template.
- Councils normally meet every six game hours (30 real minutes at 12×). Important events bring meetings forward, with a ten-game-minute cooldown. One council is prepared at a time across the world; urgent kingdoms go first, then overdue kingdoms. Calls stay asynchronous on the existing shared AI worker. Measured thinking time can extend the council interval.

## Running this build

```powershell
.\.venv\Scripts\python.exe cli.py serve --profile civilization
```

Or run `scripts/start_civilization.ps1`. Add `--port 8001` to the Python command if another world is using port 8000.

Select installed Ollama models independently for each civilization:

```powershell
.\.venv\Scripts\python.exe cli.py serve --profile civilization --model north=qwen3:8b --model east=gemma3:12b --model south=gemma3:12b --model west=qwen3:8b
```

Any compatible model served by the existing Ollama client can be selected; this does not download models. Other model providers need a client adapter. The first build uses launch options, not an in-game model picker.

The mode has its own database (`data/civilization.db`) and saves (`data/saves/civilization`). It saves every real minute and resumes its own world with a normal pace of 12×. It pauses while closed. Classic saves are not converted or overwritten. `--no-save` is available for disposable tests.

### Adaptive time and decision freshness

The base clock does not slow for every model request. A pending council, diplomatic decision or character adjustment gets three real minutes at normal pace. Beyond that it slows to 3×; after ten real minutes it slows to 1×. During active combat, fires or a food emergency, a decision pending for 30 real seconds triggers 1×. These thresholds are configurable in `simulation.adaptive_clock`.

Speed changes integrate elapsed time at its previous rate. Waiting time is not replayed as a later acceleration burst. Simulation backlog is capped at one game minute. Once pressure clears, the clock waits 15 real seconds and gradually returns toward its configured speed. The viewer shows actual speed and its reason; the tooltip shows pending decisions.

Council snapshots are prepared one at a time, rather than queuing four old snapshots. Answers older than three game hours or 15 real minutes are discarded. A council answer is also discarded if an important situation event occurred while it was thinking. A new council is scheduled using current facts; expired diplomatic offers fall back to the current tactical decision. Failed or cancelled requests release their pending slots.

Existing civilization saves receive the revised runtime pacing and decision-freshness settings on load, while retaining their cities and simulation balance. This mode remains paused while closed even if its normal speed is overridden to 1×.

Tactical movement, combat and survival continue while the model thinks. These measures reduce late decisions; they do not guarantee that a slow model will always keep up or eliminate simulation bugs. Focused tests now cover clock thresholds, recovery, backlog limits and saved-world recovery. A complete autonomous run with live models, including decision freshness under sustained load, remains unverified.

The classic two-lineage CMA-ES tournament is disabled in this four-civilization mode until its training scenarios and scoring are adapted. Its old bandit camp is also disabled until regional camps are balanced. Neither limitation disables council decisions, research experiments or the existing classic mode.

## Working foundation

### World and extraction

The four starting regions are rotated copies with equal resource deposits. Rivers separate regions; shallow crossings keep all capitals reachable. Existing stone quarries, iron mines and gold mines retain physical worker trips, finite deposits, tools and depot upgrades. A Miners Lodge improves extraction of stone, ore and gold.

### Institutions, materials and buildings

Seventeen additional building types have engine effects and costs:

| Area | Buildings and current effects |
| --- | --- |
| Education | School, Library, Academy, University, Research Laboratory: education and experiment speed |
| Culture | Tavern and Shrine: happiness, fictional traditions and model-written verses |
| Administration | Town Hall and Civic Office: construction efficiency |
| Skilled production | Miners Lodge and Guild Hall: gathering efficiency |
| Manufacturing | Brickyard makes bricks from stone and wood; Foundry makes steel from ore and wood; Glassworks makes glass; Factory turns steel and wood into components |
| Urban housing | Apartments and Skyscraper provide additional real housing |

Construction and upgrades use the normal engine's time, resources and building levels. Later buildings also require population, a development era, prerequisite institutions and manufactured materials. Production consumes inputs and coins; materials are saved and can be spent on construction. Institutions with upkeep do not provide their service effects while unfunded.

These are functional abstract buildings drawn by the existing renderer, not a finished modern-city art set. Institutions and workshops currently use abstract capacity rather than a full employer/employee job market.

### Research and invention

Each model may propose a name, field, method (`process`, `tools` or `logistics`) and an explanation of what to test. Supported fields are food, wood, stone, ore, gold, construction, education and preservation. The engine determines costs, time, prerequisites and limits; the model cannot grant itself free resources or run code.

Experiments spend resources before work begins and persist through saves. Production experiments compare candidate techniques with baseline collection formulas, including travel and carrying capacity, over controlled trials. Prototype quality is stochastic. Other fields use a bounded abstract trial model. A candidate must pass at least 75% of trials and a minimum measured improvement; failed experiments keep their findings but apply no bonus.

Successes provide capped effects to actual production, construction, research or spoilage. The model sees its civilization's recent successful and failed experiments. A tactical fallback can run the same experiments when a model has not proposed one.

This is a bounded invention prototype. Prose does not create arbitrary mechanics, new executable code, or entirely new building types. The current `process` and `tools` methods share a production-rate primitive; logistics changes carrying capacity. Richer designs and experimental variables are a next step.

### People and government

Economy, research and government offices belong to existing adult citizens. The leader can appoint eligible citizens by ID; one citizen cannot hold two offices. Automatic weekly reviews fill vacancies and consider experience, age and happiness. Death or departure triggers replacement without waiting a week; retirement and poor performance at a term review can change staff. A competent incumbent can stay.

Economy experience improves manufacturing yield, research experience improves experiment speed, and government experience reduces service costs, with bounded effects. The accounts record research, industry and service spending separately. This is an administrative foundation, not yet elections, private wealth, political parties or independent government AI agents.

### Continuous eras and observation

Settlement → civic → industrial → modern advancement depends on castle development, population, education and validated discoveries. Progress does not clear cities or knowledge. The spectator cards show institutions' officeholders, education, current experiments, results, material stocks and cultural works. Diplomacy displays all six pairs of civilizations.

The classic castle tiers and military units remain underneath this first build. Long-term advancement from a fresh village to a modern city has not yet been balanced or demonstrated through a complete autonomous run.

## Next development stages

1. **Research depth:** parameterized invention designs, more resource-specific experimental models, competing hypotheses, adoption costs, knowledge transfer through trade and espionage, and measured strategy benchmarks.
2. **Geography and settlements:** surveying and prospecting, richer deposits, renewable supply options, mines opening or closing, regional settlements, bridges, shipping and territorial administration.
3. **Economy and work:** companies, employers, named professions, vacancies, wages, household money, tax policy, operating budgets and transport contracts. Make institution staffing use actual worker time.
4. **Politics and culture:** succession, government forms, laws, legitimacy, competing institutions, fictional religions and cultural exchange, tavern activity, poets and recorded works.
5. **Modern development:** deeper material chains, energy, sanitation, infrastructure and modern construction, each backed by engine mechanics and observable tradeoffs.
6. **Model comparison:** an in-game controller picker, provider adapters, fair multi-civilization training and model-vs-PPO evaluation on fixed scenarios.

The engine defines the world's capabilities. Models choose and experiment inside them; learning is measured through outcomes rather than claimed intelligence.
