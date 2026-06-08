# Day 6 Execution README
## PH Geospatial Intelligence Platform v2.2 — Day 6 Artifacts

**Gate status:** Day 5 PASSED — all 3 artifacts confirmed (Dockerfile, ci.yml, geo_pipeline_daily.py)  
**Day 6 window:** 6 hours (0–2h · 2–5h · 5–6h)  
**Halt conditions:** Post rejected or heavily downvoted → capture feedback, flag visual-quality debt.

---

## Artifact Inventory

| File | Time Block | Gate Criteria | Status |
|------|------------|---------------|--------|
| `reddit_posts.md` | 0–2h | Post live by 8 AM EST Sunday. Comment within 10 min. | GENERATE |
| `README.md` | 0–2h | Architecture overview present. PSA + NAMRIA attributions present. Stack listed. | GENERATE |
| `deckgl_animation_config.js` | 2–5h | Deck.gl renders H3 hexagons. Animation loops without errors. | GENERATE |
| `screen_recording_guide.md` | 2–5h | GIF/MP4 < 50 MB. r/dataisbeautiful post live with OC flair. | GENERATE |
| `RUNBOOK.md` | 5–6h | < 2 pages. Covers 5 scenarios. New engineer restartable from README alone. | GENERATE |

---

## Execution Gates (Confirm Before Proceeding)

### Gate 0–2h: r/MapPorn Post

Confirm feasibility before execution:

**REQUIRED — confirm these exist from previous days:**
- [ ] `day4_choropleth_4k.png` (3840×2160 PNG) exists and passes visual quality bar
- [ ] GitHub repo URL ready (README.md will be pushed here)
- [ ] Reddit account ready with posting permissions on r/MapPorn

**CONDITIONAL — if Day 1 data was geometry-only:**
- If no numeric attribute confirmed in Day 1 profile → use boundary-catalog title from `reddit_posts.md` Option B
- Do not fabricate indicator values

**NOT NEEDED for this gate:**
- The 4K PNG itself (generated Day 4) — this gate only posts it
- dbt (deferred to Day 7 contingency)
- TECH-DEBT-002 (never in this sprint)

---

### Gate 2–5h: Deck.gl Animation

**REQUIRED — confirm these are running:**
- [ ] `docker-compose up` stack healthy (`curl http://localhost:8002/health/ready` → 200)
- [ ] `/geo/v1/h3/5` returns GeoJSON with `jenks_class` and `h3_index` properties
- [ ] `/geo/v1/metadata` endpoint exists (for vintage year detection)

**Mode auto-selection logic (in `deckgl_animation_config.js`):**
```
/geo/v1/metadata returns vintage_years.length > 1
  → MULTI_VINTAGE: time-series animation
  → else FLY_THROUGH: single-vintage camera orbit
```

**Do not implement dbt or Section 27 during this block.** Animation only.

**FEASIBILITY CHECK — if API is not ready:**
The animation config is a standalone HTML/JS — it can be opened against a mock JSON file for recording purposes. Replace `GEO_API` constant with a local static JSON file path.

---

### Gate 5–6h: RUNBOOK.md

**Criteria:**
- [ ] Word count: target < 800 words (this version: ~620 words — PASSES)
- [ ] All 5 scenarios covered: quarantine > 5%, Martin OOM, DuckDB lock, data correction, SCD Type 2 ✓
- [ ] "Full Stack Restart" section enables cold-start from README alone ✓
- [ ] No TECH-DEBT-002 content ✓
- [ ] No dbt content ✓

---

## Folder Structure (this ZIP)

```
day6/
├── README.md                     # GitHub repo README (architecture + attributions + stack)
├── reddit_posts.md               # r/MapPorn + r/dataisbeautiful post templates
├── deckgl_animation_config.js    # Deck.gl animation (MULTI_VINTAGE / FLY_THROUGH)
├── screen_recording_guide.md     # OBS → ffmpeg → MP4/GIF pipeline, size verification
├── RUNBOOK.md                    # Operational runbook — 5 scenarios
└── day-6-README.md               # This file — execution gates for gatekeepers
```

---

## Day 7 Contingency Trigger

Day 7 is contingency only. Activates if:

| Trigger | Task |
|---------|------|
| Either post rejected/downvoted | Capture feedback. Flag visual-quality debt. Do NOT restart sprint. |
| Day 5 gate actually failed (unlikely — gate PASSED) | Return to Day 5 before any Day 7 work |
| >2h slack after Day 6 completion | Optional: dbt semantic layer target (Section 27) — no production runs |

**Do not add new architectural scope in Day 7.**

---

## Constraint Checklist

- [x] TECH-DEBT-002 not rewritten
- [x] dbt (Section 27) not implemented
- [x] No post-Day-5 artifacts modified (Dockerfile, ci.yml, geo_pipeline_daily.py left as-is)
- [x] RUNBOOK.md < 2 pages
- [x] Reddit posts include [OC] flair
- [x] PSA + NAMRIA attributions in README.md
- [x] Stack comment prepared (portfolio signal — as important as the post per Section 20 Day 14)
