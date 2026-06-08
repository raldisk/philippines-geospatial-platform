# Screen Recording Setup Guide
## PH Geospatial Platform — Day 6 Animation Capture

**Target output:** GIF/MP4 < 50 MB  
**Deck.gl animation:** `deckgl_animation_config.js`  
**Tool stack:** OBS Studio → ffmpeg → optional GIF via Gifski

---

## 1. Browser Setup (Pre-Recording)

```bash
# 1. Start FastAPI and Martin
docker-compose -f docker-compose.prod.yml up -d geo-service martin

# 2. Verify H3 endpoint returns data
curl "http://localhost:8002/geo/v1/h3/5" | python -m json.tool | head -40

# 3. Serve animation HTML
# If using a dev server:
npx serve . -l 3000
# Then open: http://localhost:3000/index.html

# 4. Browser: open DevTools → confirm no console errors
# Expected log output:
#   [geo-platform] Detecting vintage years...
#   [geo-platform] MULTI_VINTAGE mode — years: 2015, 2018, 2021
#     OR
#   [geo-platform] FLY_THROUGH mode — single vintage
#   [geo-platform] Loaded N hexagons
```

**Browser settings before recording:**
- Fullscreen the browser window (F11)
- Hide browser chrome: `Ctrl+Shift+F` (Firefox) or fullscreen mode (Chrome)
- Set browser zoom to 100% (`Ctrl+0`)
- Close all other tabs

---

## 2. OBS Studio Configuration (Windows — MPC-HC compatible output)

**Scene setup:**

```
Scene: "PH Geo Animation"
Sources:
  - [Window Capture]  → select Chrome/Firefox window
    - Crop: none (capture full window)
  - (optional) [Audio Output Capture] → mute for Reddit posts
```

**Output settings:**

```
Output Mode: Advanced
Recording:
  Encoder:     x264 (or NVENC if GPU available)
  Rate Control: CRF
  CRF:         18        (high quality; reduce to 23 if file too large)
  Preset:      veryfast  (fast encode; use slow for final render)
  Profile:     high
  Keyframe Interval: 2

Video:
  Base Resolution:  1920×1080  (or 2560×1440 if machine handles it)
  Output Resolution: 1920×1080
  FPS: 30

Output path: C:\recordings\ph_geo_animation_raw.mp4
```

**Recording flow:**
1. Let animation play through at least one full cycle before starting recording.
2. Click "Start Recording" when animation begins a new cycle (year label resets or fly-through restarts).
3. Record 1–2 full cycles.
4. Click "Stop Recording".

---

## 3. ffmpeg Post-Processing

### Option A — MP4 (preferred, smallest file)

```bash
# Input: raw OBS recording
# Output: web-optimized MP4 < 50 MB

ffmpeg -i ph_geo_animation_raw.mp4 \
  -vf "scale=1280:720:flags=lanczos,fps=30" \
  -c:v libx264 \
  -crf 23 \
  -preset slow \
  -movflags +faststart \
  -an \
  ph_geo_animation.mp4

# Check file size
ls -lh ph_geo_animation.mp4

# If > 50 MB, increase CRF or reduce resolution:
# -crf 26  →  smaller file, lower quality
# scale=960:540  →  smaller resolution
```

### Option B — GIF (for direct Reddit upload, worse quality)

```bash
# Step 1: Generate color palette for high-quality GIF
ffmpeg -i ph_geo_animation_raw.mp4 \
  -vf "fps=15,scale=800:-1:flags=lanczos,palettegen=stats_mode=diff" \
  palette.png

# Step 2: Apply palette
ffmpeg -i ph_geo_animation_raw.mp4 -i palette.png \
  -lavfi "fps=15,scale=800:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5" \
  ph_geo_animation.gif

# Check size
ls -lh ph_geo_animation.gif
# GIF at 800px wide, 15fps is typically 20–45 MB for 18s content.
# If > 50 MB: reduce fps to 10 or width to 640.
```

### Option C — Gifski (best quality GIF, requires install)

```bash
# Install: https://gif.ski/
# Windows: winget install gifski

# Export frames from ffmpeg first
mkdir frames
ffmpeg -i ph_geo_animation_raw.mp4 -vf "fps=15,scale=800:-1:flags=lanczos" frames/frame%04d.png

# Convert with gifski
gifski --fps 15 --quality 85 --width 800 -o ph_geo_animation.gif frames/frame*.png

ls -lh ph_geo_animation.gif
```

---

## 4. Size Verification Checklist

```bash
# Required: < 50 MB
ls -lh ph_geo_animation.mp4
ls -lh ph_geo_animation.gif

# If MP4 > 50 MB:
ffprobe -v error -select_streams v:0 -show_entries stream=bit_rate \
  -of default=noprint_wrappers=1 ph_geo_animation.mp4
# Adjust -crf upward by 2 until file fits.

# If GIF > 50 MB:
# Reduce to fps=10 and scale=640 — GIF at 640px/10fps is ~25–35 MB.
```

---

## 5. Reddit Upload Notes

**r/dataisbeautiful accepts:**
- GIF: direct upload (≤ 100 MB site limit; keep < 50 MB for fast load)
- MP4: link post to GitHub release asset, Streamable, or Imgur video
- OC flair required — tick "OC" before submitting

**Recommended flow:**
1. Upload `ph_geo_animation.gif` directly to Reddit as image post (if < 50 MB).
2. If MP4: upload to Streamable (free, no account required for short clips).
3. Post as Link post with Streamable URL.
4. Add [OC] to title.
5. Post stack comment (from `reddit_posts.md`) within 10 minutes.

---

## 6. Animation Duration Reference

| Mode | Recommended Duration | File Size Estimate |
|------|---------------------|-------------------|
| MULTI_VINTAGE (3 years) | 12–15s | MP4 ~8–15 MB, GIF ~20–35 MB |
| FLY_THROUGH | 18–20s | MP4 ~12–20 MB, GIF ~30–45 MB |

Both fit within the 50 MB gate at 1280×720 / CRF 23 / 30fps for MP4.
