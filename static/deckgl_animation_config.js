/**
 * deckgl_animation_config.js
 * PH Geospatial Intelligence Platform v2.2 — Day 6 Animation
 *
 * Supports two modes (auto-detected at runtime):
 *   MULTI_VINTAGE  — time-series animation across PSA vintage years (2015, 2018, 2021)
 *   FLY_THROUGH    — single-vintage camera orbit over Philippines
 *
 * Requirements:
 *   npm install deck.gl @luma.gl/core maplibre-gl h3-js
 */

import { Deck, MapView } from "@deck.gl/core";
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { ScatterplotLayer } from "@deck.gl/layers";
import maplibregl from "maplibre-gl";

// ── Constants ────────────────────────────────────────────────────────────────

/** Philippine bounding box [minLon, minLat, maxLon, maxLat] */
const PH_BBOX = [116.87, 4.59, 126.60, 21.12];

/** Robinson-equivalent center for Deck.gl (WebMercator approximation) */
const PH_CENTER = { longitude: 121.77, latitude: 12.88 };

/** Jenks class count — must match Gold generation output */
const JENKS_CLASSES = 5;

/** H3 resolution stored in geo_schema_registry — fetch from /geo/v1/metadata */
const H3_RESOLUTION = 5; // override at runtime from API

/** FastAPI base URL */
const GEO_API = "http://localhost:8002/geo/v1";

/** PMTiles source (Martin tile server) */
const PMTILES_SOURCE = "http://localhost:3002/tiles/{dataset}/{z}/{x}/{y}";

// ── Color ramp (YlOrRd, 5-class Jenks) ─────────────────────────────────────
// Format: [R, G, B, A] — alpha 220 for slight transparency
const COLOR_RAMP = [
  [255, 255, 178, 220], // class 1 — lowest
  [254, 204, 92, 220],  // class 2
  [253, 141, 60, 220],  // class 3
  [240, 59, 32, 220],   // class 4
  [189, 0, 38, 220],    // class 5 — highest
];

/** Map Jenks class (1–5) to RGB. Handles edge cases gracefully. */
function jenksToColor(jenksClass) {
  const idx = Math.max(0, Math.min(jenksClass - 1, COLOR_RAMP.length - 1));
  return COLOR_RAMP[idx];
}

// ── View State ───────────────────────────────────────────────────────────────

const INITIAL_VIEW_STATE = {
  longitude: PH_CENTER.longitude,
  latitude: PH_CENTER.latitude,
  zoom: 5.2,
  pitch: 0,
  bearing: 0,
};

/**
 * Fly-through keyframes — used when MULTI_VINTAGE data is unavailable.
 * Each keyframe: { longitude, latitude, zoom, pitch, bearing, durationMs }
 */
const FLY_THROUGH_KEYFRAMES = [
  // Start: national overview
  { longitude: 121.77, latitude: 12.88, zoom: 5.2, pitch: 0, bearing: 0, durationMs: 0 },
  // Pan to Luzon — tilted for drama
  { longitude: 121.00, latitude: 17.50, zoom: 6.5, pitch: 45, bearing: -10, durationMs: 3000 },
  // Zoom into NCR / Metro Manila
  { longitude: 121.05, latitude: 14.55, zoom: 9.0, pitch: 30, bearing: 15, durationMs: 6000 },
  // Pull back, pan south to Visayas
  { longitude: 123.50, latitude: 10.70, zoom: 6.8, pitch: 30, bearing: 0, durationMs: 10000 },
  // Mindanao wide shot
  { longitude: 124.80, latitude: 7.50, zoom: 6.5, pitch: 20, bearing: -5, durationMs: 14000 },
  // Return to national — level pitch
  { longitude: 121.77, latitude: 12.88, zoom: 5.2, pitch: 0, bearing: 0, durationMs: 18000 },
];

/** Total fly-through duration in ms */
const FLY_THROUGH_TOTAL_MS = 18000;

// ── Data Fetching ────────────────────────────────────────────────────────────

/**
 * Fetch H3 hexagon data from FastAPI.
 * Returns array of { h3Index, jenksClass, indicatorValue, regionName }
 * @param {number} resolution H3 resolution
 * @param {number|null} year  null → single-vintage mode
 */
async function fetchH3Data(resolution, year = null) {
  const params = new URLSearchParams({ resolution });
  if (year !== null) params.set("year", year);
  const resp = await fetch(`${GEO_API}/h3/${resolution}?${params}`);
  if (!resp.ok) throw new Error(`H3 API error: ${resp.status}`);
  const geojson = await resp.json();
  return geojson.features.map((f) => ({
    h3Index: f.properties.h3_index,
    jenksClass: f.properties.jenks_class,
    indicatorValue: f.properties.indicator_value,
    regionName: f.properties.region_name,
  }));
}

/**
 * Detect available vintage years from metadata endpoint.
 * Returns sorted array e.g. [2015, 2018, 2021] or [] if single-vintage.
 */
async function detectVintageYears() {
  try {
    const resp = await fetch(`${GEO_API}/metadata`);
    const meta = await resp.json();
    return (meta.vintage_years || []).sort();
  } catch {
    return [];
  }
}

// ── Animation State ──────────────────────────────────────────────────────────

const state = {
  mode: null,           // "MULTI_VINTAGE" | "FLY_THROUGH"
  vintageYears: [],
  currentYearIdx: 0,
  data: {},             // { year: [...hexagons] } or { single: [...hexagons] }
  viewState: { ...INITIAL_VIEW_STATE },
  animationStartMs: null,
  animationFrameId: null,
  tooltipContent: null,
};

// ── Deck.gl Setup ────────────────────────────────────────────────────────────

const deck = new Deck({
  canvas: "deck-canvas",
  width: "100%",
  height: "100%",
  initialViewState: INITIAL_VIEW_STATE,
  controller: true,
  onViewStateChange: ({ viewState }) => {
    state.viewState = viewState;
  },
  getTooltip: ({ object }) => {
    if (!object) return null;
    return {
      html: `
        <div style="font-family:monospace;font-size:12px;padding:8px;background:#1a1a2e;
                    color:#eee;border-radius:4px;border:1px solid #444">
          <strong>${object.regionName || "Unknown region"}</strong><br/>
          Class: ${object.jenksClass} / ${JENKS_CLASSES}<br/>
          Value: ${object.indicatorValue?.toFixed(2) ?? "N/A"}
          ${state.mode === "MULTI_VINTAGE" ? `<br/>Year: ${state.vintageYears[state.currentYearIdx]}` : ""}
        </div>
      `,
      style: { background: "transparent", border: "none" },
    };
  },
});

/**
 * Build H3HexagonLayer for current frame data.
 */
function buildH3Layer(hexagons) {
  return new H3HexagonLayer({
    id: "h3-hexagon-layer",
    data: hexagons,
    getHexagon: (d) => d.h3Index,
    getFillColor: (d) => jenksToColor(d.jenksClass),
    getElevation: (d) => d.indicatorValue * 500, // extrusion optional — set pitch > 0 to see
    elevationScale: 0,  // set to 1 for 3D extrusion mode
    extruded: false,    // set to true for 3D mode
    wireframe: false,
    pickable: true,
    opacity: 0.85,
    coverage: 0.92,     // slight gap between hexagons for visual clarity
    transitions: {
      getFillColor: { duration: 800, type: "interpolation" },
      getElevation: { duration: 800, type: "interpolation" },
    },
    updateTriggers: {
      getFillColor: [state.currentYearIdx],
      getElevation: [state.currentYearIdx],
    },
  });
}

function renderCurrentFrame() {
  let hexagons;
  if (state.mode === "MULTI_VINTAGE") {
    const year = state.vintageYears[state.currentYearIdx];
    hexagons = state.data[year] || [];
  } else {
    hexagons = state.data.single || [];
  }
  deck.setProps({ layers: [buildH3Layer(hexagons)] });
}

// ── Multi-Vintage Time-Series Animation ─────────────────────────────────────

const VINTAGE_HOLD_MS = 3000;   // ms per vintage frame (adjust for GIF timing)
const VINTAGE_TRANSITION_MS = 800; // H3 layer transition (matches deck.gl transition)

let vintageTimer = null;

function startMultiVintageAnimation() {
  state.currentYearIdx = 0;
  renderCurrentFrame();

  function advanceYear() {
    state.currentYearIdx = (state.currentYearIdx + 1) % state.vintageYears.length;
    renderCurrentFrame();
    vintageTimer = setTimeout(advanceYear, VINTAGE_HOLD_MS + VINTAGE_TRANSITION_MS);
  }

  vintageTimer = setTimeout(advanceYear, VINTAGE_HOLD_MS);
}

function stopMultiVintageAnimation() {
  if (vintageTimer !== null) {
    clearTimeout(vintageTimer);
    vintageTimer = null;
  }
}

// ── Fly-Through Animation ───────────────────────────────────────────────────

/**
 * Linear interpolation between two view states at normalized t (0–1).
 */
function lerpViewState(a, b, t) {
  const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t; // ease in-out quad
  return {
    longitude: a.longitude + (b.longitude - a.longitude) * ease,
    latitude: a.latitude + (b.latitude - a.latitude) * ease,
    zoom: a.zoom + (b.zoom - a.zoom) * ease,
    pitch: a.pitch + (b.pitch - a.pitch) * ease,
    bearing: a.bearing + (b.bearing - a.bearing) * ease,
  };
}

function startFlyThroughAnimation() {
  state.animationStartMs = performance.now();

  function animate(now) {
    const elapsed = now - state.animationStartMs;
    const totalMs = FLY_THROUGH_TOTAL_MS;

    if (elapsed >= totalMs) {
      // Loop: restart
      state.animationStartMs = now;
      state.animationFrameId = requestAnimationFrame(animate);
      return;
    }

    // Find which segment we're in
    const keyframes = FLY_THROUGH_KEYFRAMES;
    let segStart = keyframes[0];
    let segEnd = keyframes[keyframes.length - 1];

    for (let i = 0; i < keyframes.length - 1; i++) {
      if (elapsed >= keyframes[i].durationMs && elapsed < keyframes[i + 1].durationMs) {
        segStart = keyframes[i];
        segEnd = keyframes[i + 1];
        break;
      }
    }

    const segDuration = segEnd.durationMs - segStart.durationMs;
    const segElapsed = elapsed - segStart.durationMs;
    const t = segDuration > 0 ? segElapsed / segDuration : 1;

    const viewState = lerpViewState(segStart, segEnd, t);
    deck.setProps({ viewState: { ...viewState, transitionDuration: 0 } });

    state.animationFrameId = requestAnimationFrame(animate);
  }

  state.animationFrameId = requestAnimationFrame(animate);
}

function stopFlyThroughAnimation() {
  if (state.animationFrameId !== null) {
    cancelAnimationFrame(state.animationFrameId);
    state.animationFrameId = null;
  }
}

// ── Initialization ───────────────────────────────────────────────────────────

async function init() {
  console.log("[geo-platform] Detecting vintage years...");
  const vintageYears = await detectVintageYears();

  if (vintageYears.length > 1) {
    state.mode = "MULTI_VINTAGE";
    state.vintageYears = vintageYears;
    console.log(`[geo-platform] MULTI_VINTAGE mode — years: ${vintageYears.join(", ")}`);

    // Pre-fetch all vintage data
    await Promise.all(
      vintageYears.map(async (year) => {
        state.data[year] = await fetchH3Data(H3_RESOLUTION, year);
        console.log(`[geo-platform] Loaded ${state.data[year].length} hexagons for ${year}`);
      })
    );

    renderCurrentFrame();
    startMultiVintageAnimation();
  } else {
    state.mode = "FLY_THROUGH";
    console.log("[geo-platform] FLY_THROUGH mode — single vintage");

    state.data.single = await fetchH3Data(H3_RESOLUTION, null);
    console.log(`[geo-platform] Loaded ${state.data.single.length} hexagons`);

    renderCurrentFrame();
    startFlyThroughAnimation();
  }
}

// ── Year Label Overlay ───────────────────────────────────────────────────────
// Injects a year label into the DOM for MULTI_VINTAGE mode screen recording.
// Call after each vintage advance.

function updateYearLabel() {
  if (state.mode !== "MULTI_VINTAGE") return;
  const label = document.getElementById("year-label");
  if (label) {
    label.textContent = state.vintageYears[state.currentYearIdx];
  }
}

// ── HTML Scaffold (inject into index.html) ───────────────────────────────────
/*
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>PH Geospatial Platform — Animation</title>
  <style>
    body { margin: 0; background: #0d1117; overflow: hidden; }
    #deck-canvas { position: absolute; inset: 0; }
    #year-label {
      position: absolute; top: 24px; left: 50%;
      transform: translateX(-50%);
      font-family: 'Courier New', monospace;
      font-size: 48px; font-weight: bold;
      color: rgba(255,255,255,0.9);
      text-shadow: 0 2px 12px rgba(0,0,0,0.8);
      pointer-events: none;
    }
    #legend {
      position: absolute; bottom: 24px; left: 24px;
      background: rgba(13,17,23,0.85);
      border: 1px solid #444; border-radius: 6px;
      padding: 12px 16px;
      font-family: monospace; font-size: 12px; color: #eee;
    }
    .legend-row { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
    .swatch { width: 18px; height: 18px; border-radius: 3px; }
  </style>
</head>
<body>
  <canvas id="deck-canvas"></canvas>
  <div id="year-label"></div>
  <div id="legend">
    <strong style="display:block;margin-bottom:8px">Jenks Classification</strong>
    <div class="legend-row"><div class="swatch" style="background:rgb(255,255,178)"></div>Class 1 — Lowest</div>
    <div class="legend-row"><div class="swatch" style="background:rgb(254,204,92)"></div>Class 2</div>
    <div class="legend-row"><div class="swatch" style="background:rgb(253,141,60)"></div>Class 3</div>
    <div class="legend-row"><div class="swatch" style="background:rgb(240,59,32)"></div>Class 4</div>
    <div class="legend-row"><div class="swatch" style="background:rgb(189,0,38)"></div>Class 5 — Highest</div>
    <div style="margin-top:8px;font-size:10px;color:#888">
      H3 Resolution ${H3_RESOLUTION} · PSA + NAMRIA data
    </div>
  </div>
  <script type="module" src="deckgl_animation_config.js"></script>
  <script type="module">
    import { init } from "./deckgl_animation_config.js";
    init();
  </script>
</body>
</html>
*/

export { init, stopMultiVintageAnimation, stopFlyThroughAnimation };
