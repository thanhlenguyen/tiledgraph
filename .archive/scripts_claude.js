// =============================================================================
// GeoRouting Lab — scripts.js
//
// This file powers the interactive map application. It handles:
//   1. Map initialization & style switching
//   2. Route Comparison     — compare up to 3 routes between two points
//   3. VRP / TSP            — optimize multi-stop delivery or travel routes
//   4. Isochrone            — show how far you can travel in X minutes
//   5. Nearest Facility     — find the closest hospital, clinic, police, etc.
//   6. Building Search      — search indoor units and vertical addresses
//   7. Info Pointer         — click any map feature to inspect its raw data
// =============================================================================


// =============================================================================
// SECTION 1: CONFIGURATION
// All static settings live here. Change these values to customise the app.
// =============================================================================

// ---------------------------------------------------------------------------
// Map styles — each entry switches the entire visual look of the basemap.
// "pitch" tilts the camera (0 = flat top-down, 60 = steep angle).
// "bearing" rotates the map (0 = north-up).
// ---------------------------------------------------------------------------
const STYLES = [
  { id: 'default', name: 'Default',   url: '/styles/style.json',     pitch: 0,  zoom: 13, bearing: 0   },
  { id: 'sat',     name: 'Satellite', url: '/styles/style_sat.json', pitch: 0,  zoom: 13, bearing: 0   },
  { id: '3d',      name: '3D',        url: '/styles/style_3d.json',  pitch: 45, zoom: 14, bearing: 0   },
  { id: 'bdf',     name: 'BDF',       url: '/styles/style_bdf.json', pitch: 60, zoom: 17, bearing: -20 },
];

// ---------------------------------------------------------------------------
// API endpoints — all requests go through the Nginx /api/ reverse proxy,
// which forwards them to the routing-api backend service on port 5000.
// ---------------------------------------------------------------------------
const BACKEND_URL = '/api';
const API = {
  route:           `${BACKEND_URL}/route`,            // Valhalla turn-by-turn routing
  optimize:        `${BACKEND_URL}/optimize_route`,   // VROOM vehicle-route optimisation
  isochrone:       `${BACKEND_URL}/isochrone`,        // Valhalla reachability polygons
  nearestFacility: `${BACKEND_URL}/nearest_facility`, // PostGIS spatial query + Valhalla
};

// How long (in milliseconds) to wait for an API response before giving up.
const TIMEOUT = 120_000; // 2 minutes

// Elasticsearch base URL — used only by the Building Search feature.
const ES_URL = 'http://localhost:9200';

// ---------------------------------------------------------------------------
// Colour palettes
// ---------------------------------------------------------------------------

// One colour per route alternative (Route Comparison tab).
const ROUTE_COLORS = ['#3b9eff', '#ff9f3b', '#a855f7'];
const ROUTE_NAMES  = ['Fastest', 'Alt — avoid highways', 'Alt — shortest distance'];

// One colour per VRP stop (up to 10). The index wraps around if you add more.
const STOP_COLORS = [
  '#f43f5e', '#fb923c', '#facc15', '#4ade80', '#22d3ee',
  '#818cf8', '#e879f9', '#f9a8d4', '#86efac', '#fde68a',
];

// Isochrone ring colours — innermost ring uses index 0.
const ISO_COLORS = ['#22d98a', '#3b9eff', '#f5a623', '#f43f5e', '#a855f7'];

// Facility type → icon, accent colour, and human-readable label.
// Keys must match the _ALLOWED_FACILITY_TYPES list in app.py (the backend)
// AND the data-ftype attributes on the buttons in index.html.
const FACILITY_CONFIG = {
  'hospital':     { icon: '🏥', color: '#ef4444', label: 'Hospital'    },
  'fire station': { icon: '🚒', color: '#f97316', label: 'Fire Station' },
  'police':       { icon: '👮', color: '#8b5cf6', label: 'Police'       },
  'clinic':       { icon: '🩺', color: '#10b981', label: 'Clinic'       },
};


// =============================================================================
// SECTION 2: GLOBAL MAP STATE
// These variables track the current state of the map so the view can be
// restored after a style switch (which reloads the entire map style).
// =============================================================================

let map;                            // The MapLibre GL map instance
let currentCenter  = [46.597, 24.876]; // Default centre: Riyadh
let currentZoom    = 13;
let currentPitch   = 0;
let currentBearing = 0;
let currentStyleId = 'default';


// =============================================================================
// SECTION 3: MODE SYSTEM
// Only one interactive mode can be active at a time (e.g. placing route points
// OR adding VRP stops). The MODE object is an enum of named string constants.
// =============================================================================

const MODE = {
  NONE:     'none',
  ROUTE:    'route',    // user is picking start/end for route comparison
  VRP:      'vrp',      // user is adding stops for VRP/TSP
  ISO:      'iso',      // user is clicking an origin for the isochrone
  FACILITY: 'facility', // user is clicking a location to search for facilities
};

let activeMode = MODE.NONE;

// Switch the map into a picking mode — show the crosshair cursor.
function enterMode(mode) {
  activeMode = mode;
  if (mode !== MODE.NONE && !infoPointerActive) {
    map.getCanvas().style.cursor = 'crosshair';
    document.getElementById('map').classList.add('info-mode');
  }
}

// Return to the default mode — restore the normal cursor.
function exitMode() {
  activeMode = MODE.NONE;
  if (!infoPointerActive) {
    map.getCanvas().style.cursor = '';
    document.getElementById('map').classList.remove('info-mode');
  }
}


// =============================================================================
// SECTION 4: MAP INITIALISATION
// =============================================================================

function initMap() {
  // Create the MapLibre GL map and mount it in the <div id="map"> element.
  map = new maplibregl.Map({
    container: 'map',
    style:     STYLES[0].url,
    center:    currentCenter,
    zoom:      currentZoom,
    pitch:     currentPitch,
    bearing:   currentBearing,
    maxPitch:  85,
  });

  // Keep currentCenter/Zoom/Pitch/Bearing in sync so style switches can
  // restore the exact camera position.
  map.on('moveend', () => {
    currentCenter  = map.getCenter().toArray();
    currentZoom    = map.getZoom();
    currentPitch   = map.getPitch();
    currentBearing = map.getBearing();
  });

  // Add the built-in navigation controls (zoom +/-, compass) and a scale bar.
  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  map.addControl(new maplibregl.ScaleControl(),      'bottom-left');
  map.addControl(new LayerSwitcherControl(),         'bottom-right');

  // Once the map is fully loaded, inject the Info Pointer toggle button.
  map.once('load', () => {
    _injectInfoPointerBtn();
    console.log('[map] loaded');
  });

  // After every style switch, the map fires "styledata" when the new style
  // is applied. We wait for "idle" (all tiles rendered) before re-drawing
  // the active feature layers so they appear on top of the new basemap.
  map.on('styledata', () => {
    map.once('idle', _restoreActiveLayers);
  });

  // Route every map click through the central handler below.
  map.on('click', handleMapClick);

  // Listen for dataset radio button changes in the Building Search tab.
  document.querySelectorAll('input[name="dataset"]').forEach(r => {
    r.addEventListener('change', e => {
      currentDataset = e.target.value;
      document.getElementById('results').innerHTML = '';
    });
  });
}

// ---------------------------------------------------------------------------
// Central click dispatcher — decides which feature handles the click.
// ---------------------------------------------------------------------------
function handleMapClick(e) {
  // Info Pointer takes priority; when active, it inspects whatever was clicked.
  if (infoPointerActive) {
    handleInfoPointerClick(e);
    return;
  }

  const ll = [e.lngLat.lng, e.lngLat.lat]; // [longitude, latitude]

  if      (activeMode === MODE.ROUTE)    routeHandleClick(ll);
  else if (activeMode === MODE.VRP)      vrpHandleClick(ll);
  else if (activeMode === MODE.ISO)      isoHandleClick(ll);
  else if (activeMode === MODE.FACILITY) facilityHandleClick(ll);
}

// ---------------------------------------------------------------------------
// Re-draw every active overlay after a style switch.
// MapLibre wipes all custom sources/layers when setStyle() is called,
// so we need to re-add them from the state objects we kept in memory.
// ---------------------------------------------------------------------------
function _restoreActiveLayers() {
  // Route comparison lines
  routeState.drawn.forEach((d, i) => {
    _addRouteLayer(i, d.coordinates, d.color, d.dash);
  });

  // VRP per-segment coloured lines
  vrpState.drawnSegments.forEach(seg => {
    _addVrpSegmentLayer(seg.srcId, seg.layerId, seg.coordinates, seg.color);
  });

  // Isochrone polygon rings
  if (isoState.geojson) {
    _renderIso(isoState.geojson);
  }

  // Nearest-facility route lines
  if (facilityState.routeFeatures.length > 0) {
    _renderFacilityRoutes(facilityState.routeFeatures, facilityState.facilityType);
  }

  // Info Pointer highlight layers
  if (infoPointerActive) {
    _setupInfoPointerLayers();
  }
}


// =============================================================================
// SECTION 5: TAB SWITCHING
// Only one tab panel is visible at a time. Switching tabs also clears the
// data belonging to every OTHER tab so the map stays uncluttered.
// =============================================================================

window.switchTab = (name) => {
  // Toggle active class on tab buttons and panels
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p =>
    p.classList.toggle('active', p.id === `tab-${name}`));

  exitMode();

  // Clear map data for features not currently shown
  if (name !== 'route')     routeFullClear();
  if (name !== 'vrp')       vrpFullClear();
  if (name !== 'isochrone') isoFullClear();
  if (name !== 'facility')  facilityFullClear();
};


// =============================================================================
// SECTION 6: SHARED UTILITIES
// Small helper functions used across multiple features.
// =============================================================================

// ---------------------------------------------------------------------------
// Decode a Google/Valhalla encoded polyline (precision 6) into an array of
// [longitude, latitude] coordinate pairs. Valhalla routes come back as a
// compressed string; this function unpacks them into a GeoJSON-ready array.
// ---------------------------------------------------------------------------
function decodePolyline6(encoded) {
  const coords = [];
  let index = 0, lat = 0, lng = 0;
  const len = encoded.length;

  while (index < len) {
    let b, shift = 0, result = 0;
    do {
      b       = encoded.charCodeAt(index++) - 63;
      result |= (b & 0x1f) << shift;
      shift  += 5;
    } while (b >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);

    shift = 0; result = 0;
    do {
      b       = encoded.charCodeAt(index++) - 63;
      result |= (b & 0x1f) << shift;
      shift  += 5;
    } while (b >= 0x20);
    lng += (result & 1) ? ~(result >> 1) : (result >> 1);

    coords.push([lng / 1e6, lat / 1e6]);
  }
  return coords;
}

// ---------------------------------------------------------------------------
// Send a POST request with a JSON body and return the parsed JSON response.
// Throws an error if the server responds with a non-2xx status or times out.
// ---------------------------------------------------------------------------
async function fetchJSON(url, body) {
  const ctrl = new AbortController();
  const tid  = setTimeout(() => ctrl.abort(), TIMEOUT);

  try {
    const res = await fetch(url, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
      signal:  ctrl.signal,
    });
    clearTimeout(tid);

    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { const j = await res.json(); if (j.detail) detail = JSON.stringify(j.detail); } catch {}
      throw new Error(detail);
    }
    return res.json();
  } catch (err) {
    clearTimeout(tid);
    if (err.name === 'AbortError') throw new Error('Request timed out — please try again.');
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Update the status bar at the bottom of the sidebar.
// type: 'info' | 'pick' | 'loading' | 'success' | 'warn' | 'error'
// Success and info messages auto-clear after 7 seconds.
// ---------------------------------------------------------------------------
function setStatus(msg, type = 'info') {
  const el = document.getElementById('status-bar');
  if (!el) return;
  el.textContent = msg;
  el.className   = `status-${type}`;
  if (type === 'success' || type === 'info') {
    setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 7000);
  }
}

// ---------------------------------------------------------------------------
// Create a small circular marker element for route/VRP stop pins.
// label  — text shown inside the circle (e.g. 'S', 'E', or a stop number)
// color  — CSS colour string for the background
// ---------------------------------------------------------------------------
function _makeStopEl(label, color) {
  const el = document.createElement('div');
  el.className        = 'stop-marker';
  el.style.background = color;
  el.textContent      = label;
  return el;
}

// ---------------------------------------------------------------------------
// Activate exactly one button in a segmented-control group.
// groupSelector — CSS selector for the group container (e.g. '#route-profile-seg')
// btn           — the button element to mark active
// ---------------------------------------------------------------------------
function _segActivate(groupSelector, btn) {
  document.querySelectorAll(`${groupSelector} .seg`).forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// Toggle the pulsing "picking" visual state on an action button while the
// user is expected to click the map.
function _setBtnPicking(id, on) {
  const btn = document.getElementById(id);
  if (btn) btn.classList.toggle('picking', on);
}


// =============================================================================
// SECTION 7: ROUTE COMPARISON (Valhalla)
// Sends the same start/end pair to Valhalla three times with different
// costing options to produce a "fastest", "avoid highways", and
// "shortest distance" alternative for side-by-side comparison.
// =============================================================================

let routeProfile = 'auto'; // 'auto' | 'bicycle' | 'pedestrian'

// Persistent state for this feature (survives style switches).
const routeState = {
  points:  [],  // [[lng,lat], [lng,lat]] — start and end
  markers: [],  // maplibregl.Marker[] — the S and E pins on the map
  drawn:   [],  // [{coordinates, color, dash}] — decoded route lines
};

// MapLibre source/layer ID helpers — called with the route index (0, 1, 2).
const ROUTE_SRC = (i) => `route-src-${i}`;
const ROUTE_LYR = (i) => `route-lyr-${i}`;

// Called when the user clicks a travel-mode segment button (Car / Bike / Walk).
window.routeSetProfile = (btn) => {
  _segActivate('#route-profile-seg', btn);
  routeProfile = btn.dataset.profile;
  // If both points are already placed, recompute immediately.
  if (routeState.points.length === 2) _runRouteComparison();
};

// Start the two-click point-picking flow.
window.routeActivate = () => {
  _routeResetPicks();
  enterMode(MODE.ROUTE);
  _setBtnPicking('route-pick-btn', true);
  setStatus('Click the map to place the Start point.', 'pick');
};

// Cancel picking and clear map data.
window.routeClear = () => {
  exitMode();
  _routeResetPicks();
};

// Called only from switchTab — removes everything without touching the UI.
function routeFullClear() {
  exitMode();
  _routeRemoveLayers();
  routeState.drawn   = [];
  routeState.points  = [];
  routeState.markers.forEach(m => m.remove());
  routeState.markers = [];
  _setBtnPicking('route-pick-btn', false);
  document.getElementById('route-legend').classList.add('hidden');
}

// Reset picks, clear map lines and markers, hide the legend.
function _routeResetPicks() {
  _routeRemoveLayers();
  routeState.drawn   = [];
  routeState.points  = [];
  routeState.markers.forEach(m => m.remove());
  routeState.markers = [];
  _updateRoutePickStatus();
  _setBtnPicking('route-pick-btn', false);
  document.getElementById('route-legend').classList.add('hidden');
  setStatus('', 'info');
}

// Receive a map click while in ROUTE mode.
function routeHandleClick(lngLat) {
  if (routeState.points.length >= 2) return; // ignore extra clicks

  routeState.points.push(lngLat);
  const isStart = routeState.points.length === 1;

  // Place a coloured pin (green = Start, red = End).
  const el = _makeStopEl(isStart ? 'S' : 'E', isStart ? '#22d98a' : '#ff5c5c');
  const m  = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
      `<b>${isStart ? 'Start' : 'End'}</b><br>
       <code>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</code>`))
    .addTo(map);
  routeState.markers.push(m);
  _updateRoutePickStatus();

  if (routeState.points.length === 2) {
    // Both points placed — run the comparison.
    exitMode();
    _setBtnPicking('route-pick-btn', false);
    _runRouteComparison();
  } else {
    setStatus('Now click the End point.', 'pick');
  }
}

// Refresh the sidebar pick-status indicators (dots + coordinate text).
function _updateRoutePickStatus() {
  document.getElementById('route-s-dot').classList.toggle('placed', routeState.points.length >= 1);
  document.getElementById('route-e-dot').classList.toggle('placed', routeState.points.length >= 2);
  document.getElementById('route-s-label').textContent = routeState.points.length >= 1
    ? `Start — ${routeState.points[0][1].toFixed(4)}, ${routeState.points[0][0].toFixed(4)}`
    : 'Start — click map';
  document.getElementById('route-e-label').textContent = routeState.points.length >= 2
    ? `End — ${routeState.points[1][1].toFixed(4)}, ${routeState.points[1][0].toFixed(4)}`
    : 'End — click map';
}

// Swap the start and end points, then recompute the routes.
window.routeSwapPoints = () => {
  if (routeState.points.length < 2) return;
  [routeState.points[0], routeState.points[1]] = [routeState.points[1], routeState.points[0]];
  routeState.markers[0].setLngLat(routeState.points[0]);
  routeState.markers[1].setLngLat(routeState.points[1]);
  _updateRoutePickStatus();
  _runRouteComparison();
};

// Fire three parallel Valhalla requests and draw all results.
async function _runRouteComparison() {
  setStatus('Computing 3 route alternatives…', 'loading');
  _routeRemoveLayers();
  routeState.drawn = [];
  document.getElementById('route-legend').classList.add('hidden');

  const [S, E] = routeState.points;
  const base   = [{ lat: S[1], lon: S[0] }, { lat: E[1], lon: E[0] }];
  const p      = routeProfile;

  // Three request payloads — same origin/destination, different costing options.
  const variants = [
    // 0 — default fastest route
    { locations: base, costing: p, costing_options: { [p]: {} },
      directions_options: { language: 'en-US' } },

    // 1 — prefer local roads, avoid highways and tolls
    { locations: base, costing: p,
      costing_options: {
        [p]: p === 'auto'    ? { use_highways: 0.05, use_tolls: 0.0 }
           : p === 'bicycle' ? { use_roads: 0.1 }
           :                   {},
      },
      directions_options: { language: 'en-US' } },

    // 2 — shortest total distance (may be slower)
    { locations: base, costing: p,
      costing_options: { [p]: { shortest: true } },
      directions_options: { language: 'en-US' } },
  ];

  // Solid / dashed / dotted line styles for each alternative.
  const DASHES = [[1, 0], [6, 4], [2, 4]];

  // Run all three requests at the same time; handle failures individually.
  const results = await Promise.allSettled(
    variants.map(v => fetchJSON(API.route, v))
  );

  const legendEl = document.getElementById('route-legend');
  legendEl.innerHTML = '';
  let drawn = 0;

  results.forEach((res, i) => {
    if (res.status !== 'fulfilled') {
      console.warn(`Route ${i} failed:`, res.reason);
      return;
    }
    const trip = res.value?.trip;
    if (!trip) return;

    // Decode the polyline shape from every leg and concatenate into one array.
    const coords = trip.legs.flatMap(leg => decodePolyline6(leg.shape));
    const color  = ROUTE_COLORS[i];
    const dash   = DASHES[i];

    routeState.drawn.push({ coordinates: coords, color, dash });
    _addRouteLayer(i, coords, color, dash);

    const km   = trip.summary.length.toFixed(1);
    const mins = Math.round(trip.summary.time / 60);

    const item = document.createElement('div');
    item.className = 'legend-item';
    item.innerHTML = `
      <div class="legend-swatch" style="background:${color}"></div>
      <div class="legend-label">${ROUTE_NAMES[i]}</div>
      <div class="legend-stat">${km} km · ${mins} min</div>`;
    legendEl.appendChild(item);
    drawn++;
  });

  if (drawn > 0) {
    legendEl.classList.remove('hidden');
    setStatus(`✅ ${drawn} routes drawn. Legend shows distance and time.`, 'success');
  } else {
    setStatus('No routes returned — are the points within the routable area?', 'error');
  }
}

// Remove all three route sources and layers from the map.
function _routeRemoveLayers() {
  [0, 1, 2].forEach(i => {
    if (map.getLayer(ROUTE_LYR(i)))  map.removeLayer(ROUTE_LYR(i));
    if (map.getSource(ROUTE_SRC(i))) map.removeSource(ROUTE_SRC(i));
  });
}

// Add (or update) one route line on the map.
// If the source already exists we call setData() to avoid a visual flash.
function _addRouteLayer(i, coordinates, color, dash) {
  const geojson = { type: 'Feature', geometry: { type: 'LineString', coordinates } };

  if (map.getSource(ROUTE_SRC(i))) {
    map.getSource(ROUTE_SRC(i)).setData(geojson);
    return;
  }

  map.addSource(ROUTE_SRC(i), { type: 'geojson', data: geojson });
  map.addLayer({
    id: ROUTE_LYR(i), type: 'line', source: ROUTE_SRC(i),
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color':     color,
      'line-width':     i === 0 ? 5 : 3.5,       // primary route is thicker
      'line-opacity':   i === 0 ? 0.95 : 0.80,
      'line-dasharray': dash,
    },
  });
}


// =============================================================================
// SECTION 8: VRP / TSP (VROOM + Valhalla)
// VRP (Vehicle Routing Problem) finds the optimal visit order for a set of
// stops, returning the vehicle to the depot at the end (closed loop).
// TSP (Travelling Salesman Problem) mode is an open tour — no fixed return.
//
// Flow:
//   1. User clicks stops on the map.
//   2. After a 2-second debounce we POST to /optimize_route (VROOM).
//   3. VROOM returns the optimal sequence.
//   4. We then fetch individual road geometries from Valhalla for each leg
//      and draw them in the colour of the origin stop.
// =============================================================================

let vrpProfile = 'auto';  // travel mode
let vrpMode    = 'vrp';   // 'vrp' = closed loop | 'tsp' = open tour

// Delay (ms) between the last click and sending the optimisation request,
// so rapidly-added stops don't each trigger a new request.
const VRP_DEBOUNCE_MS = 2000;
let vrpDebounceTimer  = null;

const vrpState = {
  stops:         [],  // [{ id, lngLat, color }]
  markers:       [],  // maplibregl.Marker[]
  drawnSegments: [],  // [{ srcId, layerId, coordinates, color }]
};

window.vrpSetProfile = (btn) => {
  _segActivate('#vrp-profile-seg', btn);
  vrpProfile = btn.dataset.profile;
  if (vrpState.stops.length >= 2) _runVRP();
};

window.vrpSetMode = (btn) => {
  document.querySelectorAll('[data-vrpmode]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  vrpMode = btn.dataset.vrpmode;
  if (vrpState.stops.length >= 2) _runVRP();
};

window.vrpActivate = () => {
  enterMode(MODE.VRP);
  _setBtnPicking('vrp-pick-btn', true);
  setStatus('Click the map to add stops. Optimal route draws 2 s after last click.', 'pick');
};

window.vrpClear = () => {
  exitMode();
  clearTimeout(vrpDebounceTimer);
  _vrpRemoveAllSegments();
  vrpState.stops = [];
  vrpState.markers.forEach(m => m.remove());
  vrpState.markers = [];
  _vrpUpdateCounter();
  _vrpHideTimer();
  _setBtnPicking('vrp-pick-btn', false);
  document.getElementById('vrp-legend').classList.add('hidden');
  setStatus('', 'info');
};

// Called from switchTab — clears everything silently.
function vrpFullClear() {
  exitMode();
  clearTimeout(vrpDebounceTimer);
  _vrpRemoveAllSegments();
  vrpState.stops         = [];
  vrpState.markers.forEach(m => m.remove());
  vrpState.markers       = [];
  vrpState.drawnSegments = [];
  _vrpUpdateCounter();
  _vrpHideTimer();
  _setBtnPicking('vrp-pick-btn', false);
  document.getElementById('vrp-legend').classList.add('hidden');
}

// Receive a map click while in VRP mode — add a new stop.
function vrpHandleClick(lngLat) {
  const idx   = vrpState.stops.length;
  const color = STOP_COLORS[idx % STOP_COLORS.length];
  vrpState.stops.push({ id: idx + 1, lngLat, color });

  const el = _makeStopEl(idx + 1, color);
  const m  = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
      `<b>Stop ${idx + 1}</b><br>
       <code>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</code>`))
    .addTo(map);
  vrpState.markers.push(m);
  _vrpUpdateCounter();

  // Reset the debounce timer every time a stop is added.
  clearTimeout(vrpDebounceTimer);
  if (vrpState.stops.length >= 2) {
    _vrpShowTimer();
    vrpDebounceTimer = setTimeout(() => {
      _vrpHideTimer();
      _runVRP();
    }, VRP_DEBOUNCE_MS);
  }
}

// Update the "N stops added" counter in the sidebar.
function _vrpUpdateCounter() {
  const n = vrpState.stops.length;
  document.getElementById('vrp-counter').textContent =
    `${n} stop${n !== 1 ? 's' : ''} added`;
}

// Show an animated progress bar that counts down to the optimisation request.
function _vrpShowTimer() {
  const bar  = document.getElementById('vrp-timer-bar');
  const fill = document.getElementById('vrp-timer-fill');
  bar.classList.remove('hidden');
  fill.style.transition = 'none';
  fill.style.width      = '0%';
  fill.getBoundingClientRect(); // force browser reflow so the transition starts fresh
  fill.style.transition = `width ${VRP_DEBOUNCE_MS}ms linear`;
  fill.style.width      = '100%';
}

function _vrpHideTimer() {
  document.getElementById('vrp-timer-bar').classList.add('hidden');
  document.getElementById('vrp-timer-fill').style.width = '0%';
}

// Send the optimisation request and draw the resulting route.
async function _runVRP() {
  if (vrpState.stops.length < 2) return;

  exitMode();
  _setBtnPicking('vrp-pick-btn', false);
  setStatus(`Optimising ${vrpState.stops.length} stops…`, 'loading');

  const isTSP = vrpMode === 'tsp';
  const depot = vrpState.stops[0].lngLat;

  // In VRP mode the vehicle starts AND ends at the depot (closed loop).
  // In TSP mode the vehicle only has a start (open tour).
  const vehicle = isTSP
    ? { id: 1, start: depot, profile: vrpProfile }
    : { id: 1, start: depot, end: depot, profile: vrpProfile };

  // Every stop except the depot becomes a "job" in the VROOM payload.
  const jobStops = isTSP ? vrpState.stops : vrpState.stops.slice(1);
  const jobs     = jobStops.map(s => ({ id: s.id, location: s.lngLat, service: 30 }));

  try {
    const result = await fetchJSON(API.optimize, { vehicles: [vehicle], jobs });
    console.log('[vrp] result', result);

    const route = result.vroom?.routes?.[0];
    if (!route) {
      setStatus('No route returned — ensure stops are within routable area.', 'warn');
      return;
    }

    // VROOM returns steps in optimal visit order; filter to only the "job" steps.
    const jobSteps = route.steps?.filter(s => s.type === 'job') ?? [];

    // Build a lookup map so we can find a stop by its id quickly.
    const idToStop = {};
    vrpState.stops.forEach(s => { idToStop[s.id] = s; });

    // Build the ordered list of coordinates to visit.
    let orderedLngLats = [depot];
    jobSteps.forEach(step => {
      const stop = idToStop[step.id];
      if (stop) orderedLngLats.push(stop.lngLat);
    });
    if (!isTSP) orderedLngLats.push(depot); // return to depot for VRP

    // Fetch road geometry for every leg in parallel.
    _vrpRemoveAllSegments();
    vrpState.drawnSegments = [];
    setStatus('Drawing route segments…', 'loading');

    const segmentRequests = orderedLngLats.slice(0, -1).map((from, i) => {
      const to = orderedLngLats[i + 1];

      // Color each leg by the color of the stop it departs from.
      let color;
      if (i === 0) {
        color = vrpState.stops[0].color; // depot leg
      } else {
        const fromStop = vrpState.stops.find(s =>
          Math.abs(s.lngLat[0] - from[0]) < 1e-8 &&
          Math.abs(s.lngLat[1] - from[1]) < 1e-8);
        color = fromStop ? fromStop.color : STOP_COLORS[i % STOP_COLORS.length];
      }
      return { from, to, color, index: i };
    });

    const segResults = await Promise.allSettled(
      segmentRequests.map(seg =>
        fetchJSON(API.route, {
          locations: [
            { lat: seg.from[1], lon: seg.from[0] },
            { lat: seg.to[1],   lon: seg.to[0]   },
          ],
          costing: vrpProfile,
          directions_options: { language: 'en-US' },
        }).then(r => ({ ...seg, trip: r.trip }))
      )
    );

    let drawnSegs = 0;
    segResults.forEach(res => {
      if (res.status !== 'fulfilled') return;
      const { index, color, trip } = res.value;
      if (!trip) return;

      const coords  = trip.legs.flatMap(leg => decodePolyline6(leg.shape));
      const srcId   = `vrp-seg-src-${index}`;
      const layerId = `vrp-seg-lyr-${index}`;

      vrpState.drawnSegments.push({ srcId, layerId, coordinates: coords, color });
      _addVrpSegmentLayer(srcId, layerId, coords, color);
      drawnSegs++;
    });

    // Build the sidebar legend — summary + visit-order dots.
    const legendEl = document.getElementById('vrp-legend');
    legendEl.innerHTML = '';

    const km   = (route.distance / 1000).toFixed(1);
    const mins = Math.round(route.duration / 60);

    const summaryRow = document.createElement('div');
    summaryRow.className = 'legend-item';
    summaryRow.innerHTML = `
      <div class="legend-label" style="font-weight:600">
        ${isTSP ? 'TSP' : 'VRP'} · ${vrpState.stops.length} stops
      </div>
      <div class="legend-stat">${km} km · ${mins} min</div>`;
    legendEl.appendChild(summaryRow);

    // Visit-order row — one coloured dot per stop.
    const orderRow = document.createElement('div');
    orderRow.className = 'legend-item';
    orderRow.style.cssText = 'flex-wrap:wrap;gap:3px';

    const orderLbl = document.createElement('div');
    orderLbl.style.cssText = 'font-size:0.6rem;color:var(--text-faint);width:100%;margin-bottom:2px';
    orderLbl.textContent   = 'Visit order →';
    orderRow.appendChild(orderLbl);
    orderRow.appendChild(_makeLegendDot(vrpState.stops[0].color, vrpState.stops[0].id));

    jobSteps.forEach(step => {
      const stop = idToStop[step.id];
      if (stop) orderRow.appendChild(_makeLegendDot(stop.color, stop.id));
    });

    if (!isTSP) {
      const arr = document.createElement('span');
      arr.style.cssText  = 'font-size:10px;color:var(--text-faint);align-self:center';
      arr.textContent    = '↩';
      orderRow.appendChild(arr);
    }

    legendEl.appendChild(orderRow);
    legendEl.classList.remove('hidden');
    setStatus(`✅ ${drawnSegs} segments drawn — ${km} km · ${mins} min`, 'success');

  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`, 'error');
  }
}

// Create a small coloured circle for the visit-order row in the VRP legend.
function _makeLegendDot(color, label) {
  const dot = document.createElement('span');
  dot.style.cssText = `
    display:inline-flex;align-items:center;justify-content:center;
    width:16px;height:16px;border-radius:50%;background:${color};
    font-size:8px;color:#fff;font-weight:700;font-family:var(--font-mono)`;
  dot.textContent = label;
  return dot;
}

// Remove every VRP segment source and layer from the map.
function _vrpRemoveAllSegments() {
  vrpState.drawnSegments.forEach(({ srcId, layerId }) => {
    if (map.getLayer(layerId)) map.removeLayer(layerId);
    if (map.getSource(srcId))  map.removeSource(srcId);
  });
  vrpState.drawnSegments = [];
}

// Add (or update) a single VRP route segment on the map.
function _addVrpSegmentLayer(srcId, layerId, coordinates, color) {
  const geojson = { type: 'Feature', geometry: { type: 'LineString', coordinates } };

  if (map.getSource(srcId)) {
    map.getSource(srcId).setData(geojson); // update in-place to avoid flash
    return;
  }

  map.addSource(srcId, { type: 'geojson', data: geojson });
  map.addLayer({
    id: layerId, type: 'line', source: srcId,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': color, 'line-width': 4.5, 'line-opacity': 0.92 },
  });
}


// =============================================================================
// SECTION 9: ISOCHRONE (Valhalla)
// An isochrone shows all locations reachable within a given travel time.
// The result is a set of polygon rings — one per configured time interval.
//
// The user can adjust:
//   • Travel mode  (Car / Bike / Walk)
//   • Interval     (minutes per ring, e.g. every 10 minutes)
//   • Levels       (how many rings to draw, e.g. 3 rings = 10, 20, 30 min)
// =============================================================================

let isoProfile  = 'auto';  // travel mode
let isoInterval = 10;      // minutes per ring

const isoState = {
  geojson:      null,  // the last GeoJSON returned by Valhalla
  originMarker: null,  // the blue origin pin on the map
  originLngLat: null,  // stored so changing a parameter re-runs without a new click
};

// Stable MapLibre source/layer IDs for the isochrone layers.
const ISO_SRC    = 'iso-src';
const ISO_FILL   = 'iso-fill';
const ISO_BORDER = 'iso-border';

window.isoSetProfile = (btn) => {
  _segActivate('#iso-profile-seg', btn);
  isoProfile = btn.dataset.profile;
  if (isoState.originLngLat) isoHandleClick(isoState.originLngLat);
};

window.isoSetInterval = (btn) => {
  document.querySelectorAll('[data-interval]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  isoInterval = parseInt(btn.dataset.interval, 10);
  document.getElementById('iso-interval-val').textContent = `${isoInterval} min`;
  if (isoState.originLngLat) isoHandleClick(isoState.originLngLat);
};

window.isoActivate = () => {
  enterMode(MODE.ISO);
  _setBtnPicking('iso-pick-btn', true);
  setStatus('Click a location on the map to compute the isochrone.', 'pick');
};

window.isoClear = () => {
  exitMode();
  isoFullClear();
  setStatus('', 'info');
};

// Called from switchTab — remove everything silently.
function isoFullClear() {
  exitMode();
  _isoRemoveLayers();
  isoState.geojson      = null;
  isoState.originLngLat = null;
  if (isoState.originMarker) { isoState.originMarker.remove(); isoState.originMarker = null; }
  _setBtnPicking('iso-pick-btn', false);
  document.getElementById('iso-legend').classList.add('hidden');
}

// Handle a map click (or a parameter-change re-run).
async function isoHandleClick(lngLat) {
  exitMode();
  _setBtnPicking('iso-pick-btn', false);

  // Only move the origin marker when the click position has changed.
  const isNewOrigin = !isoState.originLngLat
    || isoState.originLngLat[0] !== lngLat[0]
    || isoState.originLngLat[1] !== lngLat[1];

  if (isNewOrigin) {
    if (isoState.originMarker) isoState.originMarker.remove();
    const el = _makeStopEl('◉', '#3b9eff');
    el.style.fontSize = '11px';
    isoState.originMarker  = new maplibregl.Marker({ element: el }).setLngLat(lngLat).addTo(map);
    isoState.originLngLat  = lngLat;
  }

  const levels = parseInt(document.getElementById('iso-levels').value, 10);

  // Build the contour array — Valhalla expects ascending time values.
  // Strip the '#' from colour values because Valhalla embeds them in the GeoJSON.
  const contours = [];
  for (let i = 1; i <= levels; i++) {
    contours.push({ time: i * isoInterval, color: ISO_COLORS[i - 1].replace('#', '') });
  }

  setStatus(`Computing ${levels}-level isochrone (${isoInterval} min intervals)…`, 'loading');

  const payload = {
    locations:  [{ lat: lngLat[1], lon: lngLat[0] }],
    costing:    isoProfile,
    contours,
    polygons:   true,
    denoise:    0.5,  // merge small disconnected blobs
    generalize: 50,   // simplify geometry by ~50 m for faster rendering
  };

  try {
    const geojson = await fetchJSON(API.isochrone, payload);
    isoState.geojson = geojson;
    _isoRemoveLayers();
    _renderIso(geojson);

    // Build the sidebar legend (innermost ring shown first).
    const legendEl = document.getElementById('iso-legend');
    legendEl.innerHTML = '';
    for (let i = 1; i <= levels; i++) {
      const item = document.createElement('div');
      item.className = 'legend-item';
      item.innerHTML = `
        <div class="legend-swatch"
             style="background:${ISO_COLORS[i - 1]};height:3px;border-radius:1px"></div>
        <div class="legend-label">${i * isoInterval} min</div>`;
      legendEl.appendChild(item);
    }
    legendEl.classList.remove('hidden');
    setStatus(`✅ ${levels} isochrone ring${levels > 1 ? 's' : ''} — every ${isoInterval} min`, 'success');
  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`, 'error');
  }
}

// Add (or update) the isochrone polygon layers.
function _renderIso(geojson) {
  if (map.getSource(ISO_SRC)) {
    map.getSource(ISO_SRC).setData(geojson); // update in-place to avoid flash
    return;
  }

  map.addSource(ISO_SRC, { type: 'geojson', data: geojson });

  // Semi-transparent fill coloured by the 'color' property in each feature.
  map.addLayer({
    id: ISO_FILL, type: 'fill', source: ISO_SRC,
    paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.13 },
  });

  // Crisp border on each ring.
  map.addLayer({
    id: ISO_BORDER, type: 'line', source: ISO_SRC,
    paint: { 'line-color': ['get', 'color'], 'line-width': 2, 'line-opacity': 0.88 },
  });
}

// Remove isochrone layers and source from the map.
function _isoRemoveLayers() {
  if (map.getLayer(ISO_BORDER)) map.removeLayer(ISO_BORDER);
  if (map.getLayer(ISO_FILL))   map.removeLayer(ISO_FILL);
  if (map.getSource(ISO_SRC))   map.removeSource(ISO_SRC);
}


// =============================================================================
// SECTION 10: NEAREST FACILITY (PostGIS + Valhalla)
// The user clicks a point on the map; the backend performs a PostGIS spatial
// query to find the N closest facilities of the chosen type within a radius,
// then Valhalla computes the actual road route to each one.
//
// Flash-free rendering:
//   Route lines are updated via source.setData() rather than being removed
//   and re-added, so there is no visible flicker between searches.
//
// Stale-request guard:
//   facilitySearchId increments on every click. Each async chain captures its
//   own ID; if the user clicks again before the response arrives, the old
//   response is silently discarded.
// =============================================================================

let facilityType     = 'hospital';
let facilitySearchId = 0; // incremented on every search to discard stale responses

const facilityState = {
  originMarker:  null,
  originLngLat:  null,
  facMarkers:    [],       // maplibregl.Marker[] for each found facility
  routeFeatures: [],       // GeoJSON features for all route lines
  facilityType:  'hospital',
};

// Stable MapLibre source/layer IDs for the facility route lines.
const FAC_ROUTE_SRC  = 'fac-routes';
const FAC_ROUTE_LYR  = 'fac-routes-lyr';
const FAC_ROUTE_GLOW = 'fac-routes-glow'; // wide blurred halo for visual depth

window.facilitySetType = btn => {
  document.querySelectorAll('#facility-type-seg .seg').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  facilityType = btn.dataset.ftype;
};

window.facilityActivate = () => {
  enterMode(MODE.FACILITY);
  _setBtnPicking('fac-pick-btn', true);
  setStatus('Click the map to search for nearby facilities.', 'pick');
};

window.facilityClear = () => {
  exitMode();
  facilityFullClear();
  setStatus('', 'info');
};

// Called from switchTab — clears everything silently.
function facilityFullClear() {
  exitMode();
  facilitySearchId++;   // invalidate any in-flight request
  if (facilityState.originMarker) {
    facilityState.originMarker.remove();
    facilityState.originMarker = null;
  }
  facilityState.facMarkers.forEach(m => { if (m._popup?.isOpen()) m._popup.remove(); m.remove(); });
  facilityState.facMarkers    = [];
  facilityState.originLngLat  = null;
  facilityState.routeFeatures = [];
  facilityState.facilityType  = 'hospital';
  _removeFacilityRouteLayers();
  document.getElementById('fac-legend').classList.add('hidden');
  document.getElementById('fac-legend').innerHTML  = '';
  document.getElementById('fac-status').textContent = '';
  _setBtnPicking('fac-pick-btn', false);
}

// Fully remove the route-line source and layers (used on clear and style switch).
function _removeFacilityRouteLayers() {
  if (map.getLayer(FAC_ROUTE_GLOW)) map.removeLayer(FAC_ROUTE_GLOW);
  if (map.getLayer(FAC_ROUTE_LYR))  map.removeLayer(FAC_ROUTE_LYR);
  if (map.getSource(FAC_ROUTE_SRC)) map.removeSource(FAC_ROUTE_SRC);
}

// Receive a map click while in FACILITY mode.
function facilityHandleClick(lngLat) {
  // Clear previous facility markers and legend.
  facilityState.facMarkers.forEach(m => { if (m._popup?.isOpen()) m._popup.remove(); m.remove(); });
  facilityState.facMarkers = [];
  document.getElementById('fac-legend').classList.add('hidden');
  document.getElementById('fac-legend').innerHTML  = '';
  document.getElementById('fac-status').textContent = '';

  // Place the origin "📍" pin.
  if (facilityState.originMarker) facilityState.originMarker.remove();
  const pinEl = document.createElement('div');
  pinEl.style.cssText = 'font-size:22px;line-height:1;cursor:default;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.6))';
  pinEl.textContent   = '📍';
  facilityState.originMarker  = new maplibregl.Marker({ element: pinEl }).setLngLat(lngLat).addTo(map);
  facilityState.originLngLat  = lngLat;
  facilityState.facilityType  = facilityType;

  exitMode();
  _setBtnPicking('fac-pick-btn', false);
  _runFacilitySearch(lngLat, facilityType);
}

// Query the backend for nearby facilities and draw the results.
async function _runFacilitySearch(lngLat, ftype) {
  const myId     = ++facilitySearchId;   // capture this search's unique ID
  const cfg      = FACILITY_CONFIG[ftype] || FACILITY_CONFIG['hospital'];
  const count    = parseInt(document.getElementById('fac-count').value,  10) || 5;
  const radiusKm = parseInt(document.getElementById('fac-radius').value, 10) || 5;

  setStatus(`Searching for ${cfg.label}s within ${radiusKm} km…`, 'loading');

  let data;
  try {
    const params = new URLSearchParams({
      lon:             lngLat[0],
      lat:             lngLat[1],
      type:            ftype,
      limit:           count,
      max_distance_km: radiusKm,
      routes:          'true',
    });

    const res = await fetch(`${API.nearestFacility}?${params}`, {
      signal: AbortSignal.timeout(120_000),
    });

    if (myId !== facilitySearchId) return; // superseded — discard

    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { const j = await res.json(); detail = j.detail || detail; } catch {}
      throw new Error(detail);
    }

    data = await res.json();
    console.log('%c Facility API response:', 'color:#22d98a;font-weight:bold', data);
    console.table(data.facilities);

  } catch (err) {
    if (myId !== facilitySearchId) return;
    setStatus(`Error: ${err.message}`, 'error');
    return;
  }

  if (myId !== facilitySearchId) return; // superseded after await

  if (!data.facilities || data.facilities.length === 0) {
    setStatus(`No ${cfg.label} found within ${radiusKm} km.`, 'warn');
    _renderFacilityRoutes([], ftype);
    return;
  }

  const facilities = data.facilities;

  // Flatten all route features from every facility into one array.
  // Each feature gets a facility_rank property so the line-width expression
  // can draw the nearest facility's route thicker than farther ones.
  const allFeatures = [];
  facilities.forEach((f, index) => {
    (f.route?.features ?? []).forEach(feat => {
      const rawCoords = feat.geometry?.coordinates;

      // Route geometries may arrive as an encoded polyline string or as a
      // plain array of coordinates — handle both cases.
      let coords;
      if (typeof rawCoords === 'string' && rawCoords.length > 0) {
        coords = decodePolyline6(rawCoords);
      } else if (Array.isArray(rawCoords) && rawCoords.length >= 2) {
        coords = rawCoords;
      } else {
        return; // skip malformed feature
      }

      if (coords.length < 2) return; // degenerate line — skip

      allFeatures.push({
        type: 'Feature',
        geometry:   { type: 'LineString', coordinates: coords },
        properties: { ...feat.properties, facility_rank: index + 1 },
      });
    });
  });

  facilityState.routeFeatures = allFeatures;
  _renderFacilityRoutes(allFeatures, ftype);

  // Place a marker for each facility.
  facilityState.facMarkers.forEach(m => { if (m._popup?.isOpen()) m._popup.remove(); m.remove(); });
  facilityState.facMarkers = [];

  facilities.forEach((f, index) => {
    const rank = index + 1;
    const el   = document.createElement('div');
    el.className  = 'fac-marker';
    el.style.background = cfg.color;
    el.innerHTML  = cfg.icon;

    // Rank badge (small number in the corner of the marker).
    const badge = document.createElement('div');
    badge.className        = 'fac-badge';
    badge.style.borderColor = cfg.color;
    badge.style.color       = cfg.color;
    badge.textContent       = rank;
    el.appendChild(badge);

    const popup = new maplibregl.Popup({ offset: 20, closeButton: true, maxWidth: '240px' })
      .setHTML(`
        <div style="font-size:12px;line-height:1.6;color:var(--text)">
          <div style="font-size:20px;margin-bottom:4px">${cfg.icon}</div>
          <strong style="color:var(--text)">${f.name}</strong>
          ${f.address ? `<br><span style="color:var(--text-dim)">${f.address}</span>` : ''}
          <br><span style="color:#22d98a">⏱ ${f.travel_minutes} min</span>
          &nbsp;·&nbsp;<span style="color:var(--text-dim)">${f.crow_distance_km} km</span>
          <br><small style="color:var(--text-faint)">Rank #${rank}</small>
        </div>`);

    const marker = new maplibregl.Marker({ element: el, anchor: 'center', offset: [0, -4] })
      .setLngLat([parseFloat(f.facility_lon), parseFloat(f.facility_lat)])
      .setPopup(popup)
      .addTo(map);

    marker._popup = popup;

    // Show popup on hover; hide with a short delay so cursor can reach it.
    let closeTimeout;
    el.addEventListener('mouseenter', () => { clearTimeout(closeTimeout); if (!popup.isOpen()) marker.togglePopup(); });
    el.addEventListener('mouseleave', () => { closeTimeout = setTimeout(() => { if (popup.isOpen()) marker.togglePopup(); }, 200); });

    facilityState.facMarkers.push(marker);
  });

  // Build the sidebar legend list.
  const legendEl = document.getElementById('fac-legend');
  legendEl.innerHTML = '';
  facilities.forEach((f, i) => {
    const item = document.createElement('div');
    item.className = 'fac-item';
    item.onclick   = () => facilityState.facMarkers[i]?.togglePopup();
    item.innerHTML = `
      <div class="fac-rank" style="background:${cfg.color}">${i + 1}</div>
      <div class="fac-info">
        <div class="fac-name">${f.name}</div>
        <div class="fac-meta">⏱ ${f.travel_minutes} min · ${f.crow_distance_km} km straight-line</div>
      </div>`;
    legendEl.appendChild(item);
  });
  legendEl.classList.remove('hidden');

  // Fit the map view to show all results and the origin pin.
  const bounds = new maplibregl.LngLatBounds();
  bounds.extend(lngLat);
  facilities.forEach(f => bounds.extend([parseFloat(f.facility_lon), parseFloat(f.facility_lat)]));
  const sidebarW = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w')) + 20;
  map.fitBounds(bounds, {
    padding: { top: 80, bottom: 80, left: sidebarW, right: 80 },
    maxZoom: 14, duration: 1200,
  });

  const closest = facilities[0];
  setStatus(`✅ ${facilities.length} ${cfg.label}(s) found — nearest is ${closest.name} (${closest.travel_minutes} min)`, 'success');
  document.getElementById('fac-status').textContent =
    `${facilities.length} result${facilities.length > 1 ? 's' : ''} — hover markers for details`;
}

// Draw (or update) the facility route lines.
// Using setData() when the source already exists prevents any visual flash
// between searches, even when switching facility type.
function _renderFacilityRoutes(features, ftype) {
  const cfg         = FACILITY_CONFIG[ftype] || FACILITY_CONFIG['hospital'];
  const color       = cfg.color;
  const geojsonData = { type: 'FeatureCollection', features };

  if (map.getSource(FAC_ROUTE_SRC)) {
    map.getSource(FAC_ROUTE_SRC).setData(geojsonData);
    if (map.getLayer(FAC_ROUTE_GLOW)) map.setPaintProperty(FAC_ROUTE_GLOW, 'line-color', color);
    if (map.getLayer(FAC_ROUTE_LYR))  map.setPaintProperty(FAC_ROUTE_LYR,  'line-color', color);
    return;
  }

  map.addSource(FAC_ROUTE_SRC, { type: 'geojson', data: geojsonData });

  // Wide blurred halo — gives depth and makes the route stand out on busy maps.
  map.addLayer({
    id:     FAC_ROUTE_GLOW,
    type:   'line',
    source: FAC_ROUTE_SRC,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color':   color,
      'line-width':   ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 14, 5, 8],
      'line-opacity': 0.15,
      'line-blur':    6,
    },
  });

  // Crisp route line — thicker for higher-ranked (closer) facilities.
  map.addLayer({
    id:     FAC_ROUTE_LYR,
    type:   'line',
    source: FAC_ROUTE_SRC,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color':   color,
      'line-width':   ['interpolate', ['linear'], ['get', 'facility_rank'], 1, 5.5, 5, 2.5],
      'line-opacity': 0.85,
    },
  });
}


// =============================================================================
// SECTION 11: BUILDING SEARCH (Elasticsearch)
// Searches two Elasticsearch indices:
//   • building_floors  — vertical address / floor-level units (va_building)
//   • building_unit    — SPL unit polygons (spl_unit)
//
// On click, the matching unit is highlighted as a 3D fill-extrusion and the
// camera animates to a strong 3D view. The BDF map style is automatically
// activated because it contains the indoor building layers.
// =============================================================================

let currentDataset   = 'va_building'; // 'va_building' | 'spl_unit'
let searchHighlights = [];             // IDs of sources/layers added for highlights
let searchPopup      = null;           // active maplibregl.Popup

// Toggle the search panel open or closed.
window.toggleSearchPanel = function() {
  document.getElementById('search-section').classList.toggle('open');
};

// Run a search — called by the button click or pressing Enter.
window.searchUnits = async function() {
  const q         = document.getElementById('searchInput').value.trim();
  const resultsEl = document.getElementById('results');

  if (!q) { resultsEl.innerHTML = ''; resultsEl.classList.remove('has-results'); return; }

  resultsEl.innerHTML = '<div class="search-loading">Searching…</div>';
  resultsEl.classList.add('has-results');
  _openSearchPanel();
  _searchClearHighlight();

  // Choose the index and fields to search based on the selected dataset.
  const index  = currentDataset === 'va_building' ? 'building_floors' : 'building_unit';
  const fields = currentDataset === 'va_building'
    ? ['properties.VerticalAddress^3', 'properties.ShortAddress^1.8']
    : ['properties.UNIT_ID', 'properties.NAME', 'properties.NAME_LONG',
       'properties.UnitAddres^3', 'properties.LabelNames'];

  // multi_match with AUTO fuzziness handles typos and partial matches.
  const esQuery = {
    query: {
      multi_match: { query: q, fields, type: 'best_fields', fuzziness: 'AUTO' },
    },
    size: 20,
  };

  try {
    const res = await fetch(`${ES_URL}/${index}/_search`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(esQuery),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const data = await res.json();
    resultsEl.innerHTML = '';

    if (data.hits.hits.length === 0) {
      resultsEl.innerHTML = '<div class="search-no-results">No results found</div>';
      return;
    }

    data.hits.hits.forEach(hit => {
      const feat  = hit._source;
      const props = feat.properties || {};
      const item  = document.createElement('div');
      item.className = 'result-item';

      const isUnit = currentDataset === 'va_building';
      if (isUnit) {
        item.innerHTML = `
          <strong>${props.VerticalAddress || props.ShortAddress || '—'}</strong>
          Floor ${props.FloorID ?? '—'} · ${props.FloorUsage || '—'}<br>
          <small>Bldg height: ${props.BuildingHeight ? props.BuildingHeight.toFixed(1) + 'm' : '—'}</small>`;
      } else {
        item.innerHTML = `
          <strong>${props.UNIT_ID || 'N/A'}</strong>
          ${props.LabelNames || props.NAME_LONG || 'Unnamed'}
          ${props.UnitAddres ? '· ' + props.UnitAddres : ''}<br>
          <small>Floor: ${props.Base != null ? props.Base.toFixed(2) + 'm' : '—'} · Type: ${props.USE_TYPE || '—'}</small>`;
      }
      item.onclick = () => _zoomToFeature(feat, item, isUnit);
      resultsEl.appendChild(item);
    });

    _expandSearchBody(true);

  } catch (err) {
    resultsEl.innerHTML = `<div class="search-error">Error: ${err.message}</div>`;
  }
};

function _openSearchPanel() {
  document.getElementById('search-section').classList.add('open');
}

function _expandSearchBody(hasResults) {
  const body = document.getElementById('search-body');
  if (hasResults) {
    document.getElementById('results').classList.add('has-results');
    document.getElementById('search-section').style.setProperty('--search-body-max', '420px');
    body.style.maxHeight = '420px';
  } else {
    document.getElementById('results').classList.remove('has-results');
    body.style.maxHeight = '';
  }
}

// Remove all search-highlight layers/sources and close any open popup.
function _searchClearHighlight() {
  searchHighlights.forEach(id => {
    if (map?.getLayer(id))  map.removeLayer(id);
    if (map?.getSource(id)) map.removeSource(id);
  });
  searchHighlights = [];
  if (searchPopup) { searchPopup.remove(); searchPopup = null; }
  document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
}

// Zoom to a search result feature and highlight it as a 3D extrusion.
async function _zoomToFeature(feature, clickedElement, isUnit = true) {
  if (!map) { console.warn('Map not ready'); return; }

  document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
  clickedElement.classList.add('active');
  _searchClearHighlight();

  if (!feature.geometry) { setStatus('No geometry for this feature.', 'warn'); return; }

  // Compute the bounding box of the feature's geometry.
  const bounds = new maplibregl.LngLatBounds();
  const flatten = (arr) => {
    if (typeof arr[0] === 'number') bounds.extend([arr[0], arr[1]]);
    else arr.forEach(flatten);
  };
  flatten(feature.geometry.coordinates);
  if (bounds.isEmpty()) { setStatus('Invalid geometry.', 'warn'); return; }

  const props  = feature.properties || {};
  const safeId = (props.UNIT_ID || props.fkFloorGUID || props.UnitAddress || 'feat')
    .replace(/[^a-z0-9]/gi, '-');
  const id = `highlight-${safeId}`;
  searchHighlights.push(id);

  const doHighlight = () => {
    if (map.getSource(id)) map.removeSource(id);
    map.addSource(id, { type: 'geojson', data: { type: 'Feature', geometry: feature.geometry, properties: props } });

    // Calculate extrusion heights so the 3D highlight sits at the correct floor.
    let extrusionBase, extrusionHeight;
    if (currentDataset === 'va_building') {
      const floorH       = (props.BuildingHeight || 0) / (props.FloorsAboveGround || 1);
      extrusionBase      = floorH * (props.FloorID || 0);
      extrusionHeight    = extrusionBase + floorH;
    } else {
      extrusionBase      = props.Base || 0;
      extrusionHeight    = extrusionBase + (props.HEIGHT || 4.25);
    }

    map.addLayer({
      id, type: 'fill-extrusion', source: id,
      paint: {
        'fill-extrusion-color':   '#ff5c00',
        'fill-extrusion-opacity': 0.92,
        'fill-extrusion-base':    extrusionBase,
        'fill-extrusion-height':  extrusionHeight,
      },
    });

    const sidebarW = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w') || '340') + 20;
    map.fitBounds(bounds, {
      padding: { top: 100, bottom: 100, left: sidebarW, right: 100 },
      maxZoom: 19.5, pitch: 58, bearing: -18, duration: 1600, essential: true,
    });

    // Show the info popup after the camera animation starts.
    setTimeout(() => {
      const popupHTML = buildPopupHTML(props, isUnit);
      const popupPos  = getPopupAnchorPosition(bounds);
      searchPopup = new maplibregl.Popup({ offset: 15, closeButton: true, maxWidth: '300px', className: 'unit-popup' })
        .setLngLat(popupPos)
        .setHTML(popupHTML)
        .addTo(map);
      searchPopup.on('close', () => { searchPopup = null; });
    }, 900);
  };

  // Always switch to the BDF style before highlighting because indoor data
  // is only present in that style.
  const bdf = STYLES.find(s => s.id === 'bdf');
  if (bdf && currentStyleId !== 'bdf') {
    currentStyleId = 'bdf';
    map.setStyle(bdf.url);
    document.querySelectorAll('.layer-switcher button').forEach(b =>
      b.classList.toggle('active', b.textContent === 'BDF'));
    map.once('idle', () => {
      _restoreActiveLayers();
      map.once('idle', doHighlight);
    });
  } else {
    doHighlight();
  }
}

// Build the HTML string shown inside a search-result popup.
function buildPopupHTML(props, isUnit) {
  let html = `
    <div style="font-size:14px;line-height:1.6;max-width:280px;color:var(--text)">
      <strong style="font-size:16px;color:var(--text)">
        ${props.NAME || props.FloorUsage || props.VerticalAddress || props.UNIT_ID || 'Feature'}
      </strong><br>`;

  if (isUnit) {
    html += `
      <b>Address:</b> ${props.VerticalAddress || props.UnitVerticalAddress || '—'}<br>
      <b>Floor:</b>   ${props.FloorID ?? (props.Base != null ? props.Base.toFixed(2) + 'm' : '—')}<br>
      <b>Height:</b>  ${props.HEIGHT != null ? props.HEIGHT.toFixed(2) + 'm' : '—'}<br>
      <b>Usage:</b>   ${props.FloorUsage || props.USE_TYPE || '—'}`;
  } else {
    html += `
      <b>ID:</b>    ${props.UNIT_ID || '—'}<br>
      <b>Type:</b>  ${props.USE_TYPE || '—'}<br>
      <b>Floor:</b> ${props.Base != null ? props.Base.toFixed(2) + 'm' : '—'}`;
  }

  html += '</div>';
  return html;
}

// Return a [lng, lat] position for the popup anchor — slightly outside the
// feature's bounding box so the popup doesn't cover it.
function getPopupAnchorPosition(bounds) {
  const ne = bounds.getNorthEast();
  const sw = bounds.getSouthWest();
  return [
    ne.lng + (ne.lng - sw.lng) * 0.2,
    sw.lat + (ne.lat - sw.lat) * 0.65,
  ];
}


// =============================================================================
// SECTION 12: INFO POINTER
// When active, clicking any visible map feature opens a floating panel
// showing the feature's raw GeoJSON properties. Useful for inspecting
// road types, building attributes, or any other layer data.
// =============================================================================

let infoPointerActive    = false;
let _isDragging          = false;

// Inject the ℹ️ toggle button into the top-right control group.
// This is called once after the map loads.
function _injectInfoPointerBtn() {
  const topRight = document.querySelector('.maplibregl-ctrl-top-right');
  if (!topRight) return;

  const group = document.createElement('div');
  group.className    = 'maplibregl-ctrl maplibregl-ctrl-group';
  group.style.marginTop = '8px';

  const btn       = document.createElement('button');
  btn.className   = 'info-pointer-btn';
  btn.type        = 'button';
  btn.innerHTML   = 'ℹ️';
  btn.title       = 'Toggle Info Pointer';
  btn.onclick     = () => toggleInfoPointer();

  group.appendChild(btn);
  topRight.appendChild(group);
}

// Turn the Info Pointer on or off.
window.toggleInfoPointer = function() {
  infoPointerActive = !infoPointerActive;
  const btn   = document.querySelector('.info-pointer-btn');
  const panel = document.getElementById('feature-info-panel');
  const mapEl = document.getElementById('map');

  if (infoPointerActive) {
    _setupInfoPointerLayers();
    btn?.classList.add('active');
    mapEl.classList.add('info-mode');
    map.getCanvas().style.cursor = 'crosshair';
    panel.classList.remove('hidden');
    _resetInfoPanelPosition();
    _setupDraggablePanel();
    _updateFeatureInfo('<p class="info-hint">Click on any feature to inspect its properties</p>');
  } else {
    btn?.classList.remove('active');
    mapEl.classList.remove('info-mode');
    map.getCanvas().style.cursor = activeMode !== MODE.NONE ? 'crosshair' : '';
    panel.classList.add('hidden');
    _clearFeatureHighlight();
  }
};

// Wire up the close button on the info panel.
document.addEventListener('DOMContentLoaded', () => {
  const closeBtn = document.getElementById('close-feature-info');
  if (closeBtn) closeBtn.onclick = () => { if (infoPointerActive) toggleInfoPointer(); };
});

// Add the three highlight layers (fill, line, circle) that visually mark
// whichever feature the user clicked. Called once — skipped if already present.
function _setupInfoPointerLayers() {
  if (map.getSource('feature-highlight')) return;

  map.addSource('feature-highlight', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });

  const layers = [
    { id: 'hl-fill',   type: 'fill',   filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'fill-color': '#3b82f6', 'fill-opacity': 0.3 } },
    { id: 'hl-line',   type: 'line',
      filter: ['any', ['==', ['geometry-type'], 'LineString'], ['==', ['geometry-type'], 'Polygon']],
      paint: { 'line-color': '#3b82f6', 'line-width': 2.5 } },
    { id: 'hl-circle', type: 'circle', filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-radius': 7, 'circle-color': '#3b82f6', 'circle-opacity': 0.7 } },
  ];
  layers.forEach(l => { if (!map.getLayer(l.id)) map.addLayer({ ...l, source: 'feature-highlight' }); });
}

// Handle a map click while Info Pointer is active.
function handleInfoPointerClick(e) {
  if (_isDragging) return; // ignore click at end of a drag

  const features = map.queryRenderedFeatures(e.point);
  if (!features || features.length === 0) {
    _clearFeatureHighlight();
    _updateFeatureInfo('<p class="info-hint">No features at this location</p>');
    return;
  }

  // Filter out all the overlay layers we added ourselves (routes, VRP, etc.)
  // so only basemap features are inspected.
  const valid = features.filter(f => {
    const id = f.layer.id;
    return !id.startsWith('hl-')        && !id.startsWith('route-src-') &&
           !id.startsWith('vrp-')       && !id.startsWith('iso-') &&
           !id.startsWith('fac-')       && !id.startsWith('search-hl');
  });

  if (!valid.length) {
    _clearFeatureHighlight();
    _updateFeatureInfo('<p class="info-hint">No base-layer features here</p>');
    return;
  }

  const feat = valid[0];
  _highlightFeature(feat);
  _displayFeatureInfo(feat);
}

// Update the 'feature-highlight' source so the blue overlay appears on the map.
function _highlightFeature(feature) {
  _clearFeatureHighlight();
  const src = map.getSource('feature-highlight');
  if (src) src.setData({ type: 'FeatureCollection', features: [feature] });
}

// Clear the blue highlight overlay.
function _clearFeatureHighlight() {
  const src = map.getSource('feature-highlight');
  if (src) src.setData({ type: 'FeatureCollection', features: [] });
}

// Render the feature's properties in the floating info panel.
function _displayFeatureInfo(feature) {
  const props    = feature.properties || {};
  const geomType = feature.geometry.type;

  // Header row — layer ID, source layer name, geometry type.
  let html = `
    <div class="feature-layer-info">
      <strong>Layer:</strong> ${feature.layer.id}<br>
      <strong>Source layer:</strong> ${feature.sourceLayer || 'N/A'}<br>
      <strong>Geometry:</strong> ${geomType}
    </div>`;

  const keys = Object.keys(props).sort();
  if (keys.length > 0) {
    keys.forEach(k => {
      const val = props[k];
      if (val === null || val === undefined) return;

      const formatted = typeof val === 'object' ? JSON.stringify(val)
                      : typeof val === 'number'  ? val.toLocaleString()
                      : String(val);

      // Convert snake_case / UPPER_CASE property keys into readable labels.
      const label = k === k.toUpperCase()
        ? k.replace(/_/g, ' ')
        : k.replace(/_/g, ' ').replace(/([A-Z])/g, ' $1')
            .replace(/^./, s => s.toUpperCase()).trim();

      html += `
        <div class="feature-property">
          <span class="property-key">${label}</span>
          <span class="property-value">${formatted}</span>
        </div>`;
    });
  } else {
    html += '<p class="info-hint" style="margin-top:8px">No properties available</p>';
  }

  _updateFeatureInfo(html);
}

// Set the HTML content of the info panel body.
function _updateFeatureInfo(html) {
  const el = document.getElementById('feature-info-content');
  if (el) el.innerHTML = html;
}

// Reset the floating panel to its default top-right position.
function _resetInfoPanelPosition() {
  const panel = document.getElementById('feature-info-panel');
  if (!panel) return;
  panel.style.left      = 'auto';
  panel.style.right     = '1rem';
  panel.style.top       = '1rem';
  panel.style.transform = 'none';
}

// Make the info panel draggable by attaching mousedown/mousemove/mouseup
// listeners to its header bar. Stored references allow removal if called again.
function _setupDraggablePanel() {
  const panel  = document.getElementById('feature-info-panel');
  const header = document.querySelector('.feature-info-header');
  if (!panel || !header) return;

  // Remove listeners from a previous activation to avoid stacking handlers.
  if (panel._dragStart) {
    header.removeEventListener('mousedown',   panel._dragStart);
    document.removeEventListener('mousemove', panel._drag);
    document.removeEventListener('mouseup',   panel._dragEnd);
  }

  let dragging = false;
  let ox = 0, oy = 0; // cursor offset from top-left of panel at drag start

  const dragStart = e => {
    if (e.target.closest('.close-btn')) return; // don't drag when clicking close
    dragging    = true;
    _isDragging = false;
    const rect  = panel.getBoundingClientRect();
    ox = e.clientX - rect.left;
    oy = e.clientY - rect.top;
    // Switch from right/top anchoring to left/top so we can reposition freely.
    panel.style.right     = 'auto';
    panel.style.left      = rect.left + 'px';
    panel.style.top       = rect.top  + 'px';
    panel.style.transform = 'none';
    e.preventDefault();
  };

  const drag = e => {
    if (!dragging) return;
    _isDragging = true;
    // Clamp within the viewport so the panel can't be dragged off-screen.
    const x = Math.max(0, Math.min(e.clientX - ox, window.innerWidth  - panel.offsetWidth));
    const y = Math.max(0, Math.min(e.clientY - oy, window.innerHeight - panel.offsetHeight));
    panel.style.left = x + 'px';
    panel.style.top  = y + 'px';
  };

  const dragEnd = () => {
    dragging = false;
    setTimeout(() => { _isDragging = false; }, 50); // short delay prevents a click firing on mouseup
  };

  header.addEventListener('mousedown', dragStart);
  document.addEventListener('mousemove', drag);
  document.addEventListener('mouseup',   dragEnd);

  // Store references so they can be removed on the next call.
  panel._dragStart = dragStart;
  panel._drag      = drag;
  panel._dragEnd   = dragEnd;
}


// =============================================================================
// SECTION 13: LAYER SWITCHER CONTROL
// A custom MapLibre GL control that renders a row of style buttons.
// Clicking a button calls map.setStyle() and then restores all active layers.
// =============================================================================

class LayerSwitcherControl {
  onAdd(map) {
    this._map       = map;
    this._container = document.createElement('div');
    this._container.className = 'maplibregl-ctrl maplibregl-ctrl-group layer-switcher';

    STYLES.forEach((s, i) => {
      const btn   = document.createElement('button');
      btn.type        = 'button';
      btn.title       = s.name;
      btn.textContent = s.name;
      if (i === 0) btn.classList.add('active');

      btn.addEventListener('click', () => {
        this._container.querySelectorAll('button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentStyleId = s.id;
        map.setStyle(s.url);

        map.once('idle', () => {
          // Restore camera position from the style's defaults.
          map.easeTo({
            center:   currentCenter,
            zoom:     s.zoom    ?? currentZoom,
            pitch:    s.pitch   ?? currentPitch,
            bearing:  s.bearing ?? currentBearing,
            duration: 800,
          });
          map.once('idle', _restoreActiveLayers);
        });
      });

      this._container.appendChild(btn);
    });

    return this._container;
  }

  onRemove() {
    this._container.parentNode?.removeChild(this._container);
  }
}


// =============================================================================
// SECTION 14: BOOT
// Start the application once the browser has fully loaded the page.
// =============================================================================

window.addEventListener('load', initMap);

// Expose symbols that are referenced by inline event handlers in index.html.
window.isoState       = isoState;
window.isoHandleClick = isoHandleClick;
