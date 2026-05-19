// =============================================================================
// GeoRouting Lab — scripts.js
// Feature 1: Multi-route comparison (Car / Bike / Walk, 3 alternatives, persists across style switches
// Feature 2: VRP/TSP           — per-segment coloring by origin stop color
// Feature 3: Isochrone         — configurable levels + interval
//
// Tab switching clears the OTHER feature's data from the map.
// Routes survive style (layer) switches by re-drawing from stored GeoJSON.
// =============================================================================

// ---------------------------------------------------------------------------
// Map styles
// ---------------------------------------------------------------------------
const STYLES = [
  { id: 'default', name: 'Default',   url: '/styles/style.json',     pitch: 0,  zoom: 13, bearing: 0  },
  { id: 'sat',     name: 'Satellite', url: '/styles/style_sat.json', pitch: 0,  zoom: 13, bearing: 0  },
  { id: '3d',      name: '3D',        url: '/styles/style_3d.json',  pitch: 45, zoom: 14, bearing: 0  },
  { id: 'bdf',     name: 'BDF',       url: '/styles/style_bdf.json', pitch: 60, zoom: 17, bearing: -20}
];

// ---------------------------------------------------------------------------
// API endpoints  (via Nginx /api/ proxy)
// ---------------------------------------------------------------------------
const API = {
  route:    '/api/route',
  optimize: '/api/optimize_route',
  isochrone:'/api/isochrone',
};
const TIMEOUT = 120_000;

// ---------------------------------------------------------------------------
// Colour palettes
// ---------------------------------------------------------------------------
// 3 alternative routes
const ROUTE_COLORS = ['#3b9eff', '#ff9f3b', '#a855f7'];
const ROUTE_NAMES = ['Fastest', 'Alt — avoid highways', 'Alt — shortest distance'];

// Up to 10 VRP stops (css variables mirror these)
const STOP_COLORS = [
  '#f43f5e','#fb923c','#facc15','#4ade80','#22d3ee',
  '#818cf8','#e879f9','#f9a8d4','#86efac','#fde68a'
];

// ---------------------------------------------------------------------------
// Global map state
// ---------------------------------------------------------------------------
let map;
let currentCenter  = [46.597, 24.876];  // Default center Riyahd;
let currentZoom    = 13;
let currentPitch   = 0;
let currentBearing = 0;

// ---------------------------------------------------------------------------
// Mode system — only one mode active at a time
// ---------------------------------------------------------------------------
const MODE = {
  NONE:  'none',
  ROUTE: 'route',   // picking S then E for route comparison
  VRP:   'vrp',     // adding stops for VRP/TSP
  ISO:   'iso',     // single click for isochrone
};
let activeMode = MODE.NONE;

function enterMode(mode) {
  activeMode = mode;
  map.getCanvas().style.cursor = mode === MODE.NONE ? '' : 'crosshair';
}
function exitMode() { enterMode(MODE.NONE); }

// ---------------------------------------------------------------------------
// Map init
// ---------------------------------------------------------------------------
function initMap() {
  map = new maplibregl.Map({
    container: 'map',
    style:     STYLES[0].url,
    center:    currentCenter,
    zoom:      currentZoom, pitch: currentPitch, bearing: currentBearing,
    maxPitch:  85
  });

  map.on('moveend', () => {
    currentCenter  = map.getCenter().toArray();
    currentZoom    = map.getZoom();
    currentPitch   = map.getPitch();
    currentBearing = map.getBearing();
  });

  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  map.addControl(new maplibregl.ScaleControl(),      'bottom-left');
  map.addControl(new LayerSwitcherControl(),         'bottom-right');

  map.on('load', () => console.log('[map] loaded'));

  // After a style change, re-draw whatever is currently active
  map.on('styledata', () => {
    // styledata fires on initial load too — skip if map not fully loaded
    if (!map.isStyleLoaded()) return;
    _restoreActiveLayers();
  });

  map.on('click', (e) => {
    const ll = [e.lngLat.lng, e.lngLat.lat];
    if      (activeMode === MODE.ROUTE) routeHandleClick(ll);
    else if (activeMode === MODE.VRP)   vrpHandleClick(ll);
    else if (activeMode === MODE.ISO)   isoHandleClick(ll);
  });

  map.on('error', (e) => console.error('[map]', e));
}

// ---------------------------------------------------------------------------
// Restore all active layers after a style switch
// ---------------------------------------------------------------------------
function _restoreActiveLayers() {
  // Re-draw route comparison lines
  routeState.drawn.forEach((d, i) => {
    _addRouteLayer(i, d.coordinates, d.color, d.dash);
  });
  // Re-draw VRP segments
  vrpState.drawnSegments.forEach((seg) => {
    _addVrpSegmentLayer(seg.srcId, seg.layerId, seg.coordinates, seg.color);
  });
  // Re-draw isochrone
  if (isoState.geojson) {
    _renderIso(isoState.geojson);
  }
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
window.switchTab = (name) => {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p =>
    p.classList.toggle('active', p.id === `tab-${name}`));

  exitMode();

  // Clear data belonging to the OTHER features when switching
  if (name === 'route') {
    vrpFullClear();
    isoFullClear();
  } else if (name === 'vrp') {
    routeFullClear();
    isoFullClear();
  } else if (name === 'isochrone') {
    routeFullClear();
    vrpFullClear();
  }
};

// =============================================================================
// FEATURE 1 — ROUTE COMPARISON
// =============================================================================

let routeProfile = 'auto';

// All drawn route state (survives style switches)
const routeState = {
  points:  [],    // [[lng,lat], [lng,lat]]
  markers: [],    // maplibregl.Marker[]
  drawn:   []     // [{coordinates, color, dash}]
};

const ROUTE_SRC = (i) => `route-src-${i}`;
const ROUTE_LYR = (i) => `route-lyr-${i}`;

window.routeSetProfile = (btn) => {
  _segActivate('#route-profile-seg', btn);
  routeProfile = btn.dataset.profile;
  // Recompute if both points already placed
  if (routeState.points.length === 2) _runRouteComparison();
};

window.routeActivate = () => {
  _routeResetPicks();
  enterMode(MODE.ROUTE);
  _setBtnPicking('route-pick-btn', true);
  setStatus('Click the map to place the Start point.', 'pick');
};

window.routeClear = () => {
  exitMode();
  _routeResetPicks();
};

// Called only from tab-switch — removes map data without touching UI
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

function routeHandleClick(lngLat) {
  if (routeState.points.length >= 2) return;
  routeState.points.push(lngLat);

  const isStart = routeState.points.length === 1;
  const color   = isStart ? '#22d98a' : '#ff5c5c';
  const label   = isStart ? 'S' : 'E';

  const el = _makeStopEl(label, color);
  const m  = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
      `<b>${isStart ? 'Start' : 'End'}</b><br><code>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</code>`))
    .addTo(map);
  routeState.markers.push(m);
  _updateRoutePickStatus();

  if (routeState.points.length === 2) {
    exitMode();
    _setBtnPicking('route-pick-btn', false);
    _runRouteComparison();
  } else {
    setStatus('Now click the End point.', 'pick');
  }
}

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

async function _runRouteComparison() {
  setStatus('Computing 3 route alternatives…', 'loading');
  _routeRemoveLayers();
  routeState.drawn = [];
  document.getElementById('route-legend').classList.add('hidden');

  const [S, E] = routeState.points;
  const base   = [{ lon: S[0], lat: S[1] }, { lon: E[0], lat: E[1] }];
  const p      = routeProfile;

  const variants = [
    // 0 — default fastest
    { locations: base, costing: p,
      costing_options: { [p]: {} },
      directions_options: { language: 'en-US' } },
    // 1 — avoid highways / prefer local
    { locations: base, costing: p,
      costing_options: { [p]: p === 'auto'     ? { use_highways: 0.05, use_tolls: 0.0 }
                                : p === 'bicycle'  ? { use_roads: 0.1, use_hills: 0.3 }
                                :                    {} },
      directions_options: { language: 'en-US' } },
    // 2 — shortest distance
    { locations: base, costing: p,
      costing_options: { [p]: p === 'auto'     ? { shortest: true }
                                : p === 'bicycle'  ? { shortest: true }
                                :                    { shortest: true } },
      directions_options: { language: 'en-US' } },
  ];

  const DASHES = [
    [1, 0],     // solid
    [6, 4],     // dashed
    [2, 4],     // dotted
  ];

  const results = await Promise.allSettled(
    variants.map(v => fetchJSON(API.route, v))
  );

  const legendEl = document.getElementById('route-legend');
  legendEl.innerHTML = '';
  let drawn = 0;

  results.forEach((res, i) => {
    if (res.status !== 'fulfilled') { console.warn(`Route ${i} failed:`, res.reason); return; }
    const trip = res.value?.trip;
    if (!trip) return;

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

function _routeRemoveLayers() {
  [0, 1, 2].forEach(i => {
    if (map.getLayer(ROUTE_LYR(i)))  map.removeLayer(ROUTE_LYR(i));
    if (map.getSource(ROUTE_SRC(i))) map.removeSource(ROUTE_SRC(i));
  });
}

function _addRouteLayer(i, coordinates, color, dash) {
  // Guard: remove if already exists (can happen on styledata restore)
  if (map.getLayer(ROUTE_LYR(i)))  map.removeLayer(ROUTE_LYR(i));
  if (map.getSource(ROUTE_SRC(i))) map.removeSource(ROUTE_SRC(i));

  map.addSource(ROUTE_SRC(i), {
    type: 'geojson',
    data: { type: 'Feature', geometry: { type: 'LineString', coordinates } }
  });
  map.addLayer({
    id: ROUTE_LYR(i), type: 'line', source: ROUTE_SRC(i),
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color':     color,
      'line-width':     i === 0 ? 5 : 3.5,
      'line-opacity':   i === 0 ? 0.95 : 0.80,
      'line-dasharray': dash
    }
  });
}

// =============================================================================
// FEATURE 2 — VRP / TSP
// =============================================================================

let vrpProfile = 'auto';
let vrpMode    = 'vrp';        // 'vrp' (closed) | 'tsp' (open)
let vrpDebounceTimer = null;
const VRP_DEBOUNCE_MS = 2000;

const vrpState = {
  stops:          [],   // [{id, lngLat, color}]
  markers:        [],   // maplibregl.Marker[]
  drawnSegments:  []    // [{srcId, layerId, coordinates, color}]  — for style restore
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
  vrpState.stops   = [];
  vrpState.markers.forEach(m => m.remove());
  vrpState.markers = [];
  _vrpUpdateCounter();
  _vrpHideTimer();
  _setBtnPicking('vrp-pick-btn', false);
  document.getElementById('vrp-legend').classList.add('hidden');
  setStatus('', 'info');
};

function vrpFullClear() {
  exitMode();
  clearTimeout(vrpDebounceTimer);
  _vrpRemoveAllSegments();
  vrpState.stops   = [];
  vrpState.markers.forEach(m => m.remove());
  vrpState.markers       = [];
  vrpState.drawnSegments = [];
  _vrpUpdateCounter();
  _vrpHideTimer();
  _setBtnPicking('vrp-pick-btn', false);
  document.getElementById('vrp-legend').classList.add('hidden');
}

function vrpHandleClick(lngLat) {
  const idx   = vrpState.stops.length;
  const color = STOP_COLORS[idx % STOP_COLORS.length];
  vrpState.stops.push({ id: idx + 1, lngLat, color });

  const el = _makeStopEl(idx + 1, color);
  const m  = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
      `<b>Stop ${idx + 1}</b><br><code>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</code>`))
    .addTo(map);
  vrpState.markers.push(m);
  _vrpUpdateCounter();

  // Reset debounce
  clearTimeout(vrpDebounceTimer);
  if (vrpState.stops.length >= 2) {
    _vrpShowTimer();
    vrpDebounceTimer = setTimeout(() => {
      _vrpHideTimer();
      _runVRP();
    }, VRP_DEBOUNCE_MS);
  }
}

function _vrpUpdateCounter() {
  const n = vrpState.stops.length;
  document.getElementById('vrp-counter').textContent =
    `${n} stop${n !== 1 ? 's' : ''} added`;
}

function _vrpShowTimer() {
  const bar  = document.getElementById('vrp-timer-bar');
  const fill = document.getElementById('vrp-timer-fill');
  bar.classList.remove('hidden');
  fill.style.transition = 'none';
  fill.style.width = '0%';
  fill.getBoundingClientRect(); // force reflow
  fill.style.transition = `width ${VRP_DEBOUNCE_MS}ms linear`;
  fill.style.width = '100%';
}
function _vrpHideTimer() {
  document.getElementById('vrp-timer-bar').classList.add('hidden');
  document.getElementById('vrp-timer-fill').style.width = '0%';
}

async function _runVRP() {
  if (vrpState.stops.length < 2) return;

  exitMode();
  _setBtnPicking('vrp-pick-btn', false);
  setStatus(`Optimising ${vrpState.stops.length} stops…`, 'loading');

  // ── Build VROOM payload ──────────────────────────────────
  // VRP mode: first stop is depot, vehicle returns to it (closed loop)
  // TSP mode: open tour — no fixed end (vehicle keeps going)
  const isTSP  = vrpMode === 'tsp';
  const depot  = vrpState.stops[0].lngLat;
  const jobStops = isTSP ? vrpState.stops : vrpState.stops.slice(1);

  const vehicle = isTSP
    ? { id: 1, start: depot, profile: vrpProfile }
    : { id: 1, start: depot, end: depot, profile: vrpProfile };

  const jobs = jobStops.map(s => ({
    id:       s.id,
    location: s.lngLat,
    service:  30
  }));

  try {
    const result = await fetchJSON(API.optimize, { vehicles: [vehicle], jobs });
    console.log('[vrp] result', result);

    const route = result.vroom?.routes?.[0];
    if (!route) {
      setStatus('No route returned — ensure stops are within routable area.', 'warn');
      return;
    }

    // ── Extract ordered stop sequence from VROOM steps ─────
    // VROOM returns steps with type "job" in optimal visit order
    const jobSteps = route.steps?.filter(s => s.type === 'job') ?? [];

    // Map job id → stop color
    const idToStop = {};
    vrpState.stops.forEach(s => { idToStop[s.id] = s; });

    // Build ordered visit list: [depot, stop_a, stop_b, …, (depot if VRP)]
    let orderedLngLats = [depot];
    jobSteps.forEach(step => {
      const stop = idToStop[step.id];
      if (stop) orderedLngLats.push(stop.lngLat);
    });
    if (!isTSP) orderedLngLats.push(depot); // return to depot

    // ── Fetch individual segments from Valhalla ────────────
    // Each segment is colored by the color of the ORIGIN stop
    _vrpRemoveAllSegments();
    vrpState.drawnSegments = [];

    setStatus('Drawing route segments…', 'loading');

    const segmentRequests = [];
    for (let i = 0; i < orderedLngLats.length - 1; i++) {
      const from  = orderedLngLats[i];
      const to    = orderedLngLats[i + 1];

      // Origin stop color: for the leg from depot, use depot/first stop color
      let color;
      if (i === 0) {
        // depot → first job: color of depot (first stop in VRP) or first stop in TSP
        color = vrpState.stops[0].color;
      } else {
        // Find the stop at position `from`
        const fromStop = vrpState.stops.find(s =>
          Math.abs(s.lngLat[0] - from[0]) < 1e-8 &&
          Math.abs(s.lngLat[1] - from[1]) < 1e-8
        );
        color = fromStop ? fromStop.color : STOP_COLORS[i % STOP_COLORS.length];
      }

      segmentRequests.push({ from, to, color, index: i });
    }

    const segResults = await Promise.allSettled(
      segmentRequests.map(seg =>
        fetchJSON(API.route, {
          locations: [{ lon: seg.from[0], lat: seg.from[1] }, { lon: seg.to[0], lat: seg.to[1] }],
          costing:   vrpProfile,
          directions_options: { language: 'en-US' }
        }).then(r => ({ ...seg, trip: r.trip }))
      )
    );

    let drawnSegs = 0;
    segResults.forEach((res) => {
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

    // ── Build legend ───────────────────────────────────────
    const legendEl = document.getElementById('vrp-legend');
    legendEl.innerHTML = '';

    // Summary row
    const km   = (route.distance / 1000).toFixed(1);
    const mins = Math.round(route.duration / 60);
    const summaryRow = document.createElement('div');
    summaryRow.className = 'legend-item';
    summaryRow.innerHTML = `
      <div class="legend-label" style="font-weight:600">${isTSP ? 'TSP' : 'VRP'} · ${vrpState.stops.length} stops</div>
      <div class="legend-stat">${km} km · ${mins} min</div>`;
    legendEl.appendChild(summaryRow);

    // Visit order row
    const orderRow = document.createElement('div');
    orderRow.className = 'legend-item';
    orderRow.style.flexWrap = 'wrap';
    orderRow.style.gap = '3px';
    const orderLbl = document.createElement('div');
    orderLbl.style.cssText = 'font-size:0.6rem;color:var(--text-faint);width:100%;margin-bottom:2px';
    orderLbl.textContent = 'Visit order →';
    orderRow.appendChild(orderLbl);

    // Depot dot
    const depotDot = _makeLegendDot(vrpState.stops[0].color, vrpState.stops[0].id);
    orderRow.appendChild(depotDot);

    jobSteps.forEach(step => {
      const stop = idToStop[step.id];
      if (!stop) return;
      const dot = _makeLegendDot(stop.color, stop.id);
      orderRow.appendChild(dot);
    });

    if (!isTSP) {
      // Return arrow + depot
      const arr = document.createElement('span');
      arr.style.cssText = 'font-size:10px;color:var(--text-faint);align-self:center';
      arr.textContent = '↩';
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

function _makeLegendDot(color, label) {
  const dot = document.createElement('span');
  dot.style.cssText = `
    display:inline-flex;align-items:center;justify-content:center;
    width:16px;height:16px;border-radius:50%;
    background:${color};font-size:8px;color:#fff;
    font-weight:700;font-family:var(--font-mono)`;
  dot.textContent = label;
  return dot;
}

function _vrpRemoveAllSegments() {
  vrpState.drawnSegments.forEach(({ srcId, layerId }) => {
    if (map.getLayer(layerId)) map.removeLayer(layerId);
    if (map.getSource(srcId))  map.removeSource(srcId);
  });
  vrpState.drawnSegments = [];
}

function _addVrpSegmentLayer(srcId, layerId, coordinates, color) {
  if (map.getLayer(layerId)) map.removeLayer(layerId);
  if (map.getSource(srcId))  map.removeSource(srcId);

  map.addSource(srcId, {
    type: 'geojson',
    data: { type: 'Feature', geometry: { type: 'LineString', coordinates } }
  });
  map.addLayer({
    id: layerId, type: 'line', source: srcId,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': color, 'line-width': 4.5, 'line-opacity': 0.92 }
  });
}

// =============================================================================
// FEATURE 3 — ISOCHRONE
// =============================================================================

let isoProfile  = 'auto';
let isoInterval = 10;

// Colours: innermost ring (shortest time) → outermost (longest time)
// We render outermost first so inner rings paint on top
const ISO_COLORS = ['#22d98a', '#3b9eff', '#f5a623', '#f43f5e', '#a855f7'];

const isoState = {
  geojson:      null,
  originMarker: null,
  originLngLat: null   // stored so parameter changes can re-run without a new click
};

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

function isoFullClear() {
  exitMode();
  _isoRemoveLayers();
  isoState.geojson = null;
  isoState.originLngLat = null;
  if (isoState.originMarker) { isoState.originMarker.remove(); isoState.originMarker = null; }
  _setBtnPicking('iso-pick-btn', false);
  document.getElementById('iso-legend').classList.add('hidden');
}

async function isoHandleClick(lngLat) {
  exitMode();
  _setBtnPicking('iso-pick-btn', false);

  // Place / move origin marker only when lngLat differs from stored origin
  const isNewOrigin = !isoState.originLngLat
    || isoState.originLngLat[0] !== lngLat[0]
    || isoState.originLngLat[1] !== lngLat[1];

  if (isNewOrigin) {
    if (isoState.originMarker) isoState.originMarker.remove();
    const el = _makeStopEl('◉', '#3b9eff');
    el.style.fontSize = '11px';
    isoState.originMarker = new maplibregl.Marker({ element: el })
      .setLngLat(lngLat)
      .addTo(map);
    isoState.originLngLat = lngLat;
  }

  const levels   = parseInt(document.getElementById('iso-levels').value, 10);

  // Build contours: Valhalla expects them sorted by ascending time
  // Outermost (largest) time first gives better polygon fill in some versions,
  // but Valhalla docs say ascending is fine — use ascending.
  const contours = [];
  for (let i = 1; i <= levels; i++) {
    const t     = i * isoInterval;
    // Color index 0 = innermost (smallest time)
    const color = ISO_COLORS[i - 1].replace('#', '');
    contours.push({ time: t, color });
  }

  setStatus(`Computing ${levels}-level isochrone (${isoInterval} min intervals)…`, 'loading');

  const payload = {
    locations: [{ lon: lngLat[0], lat: lngLat[1] }],
    costing:   isoProfile,
    contours,
    polygons:  true,
    denoise:   0.5,       // smooth noisy edges
    generalize: 50        // metres — simplify geometry for faster rendering
  };

  try {
    const geojson = await fetchJSON(API.isochrone, payload);
    isoState.geojson = geojson;
    _isoRemoveLayers();
    _renderIso(geojson);

    // Build legend (innermost first)
    const legendEl = document.getElementById('iso-legend');
    legendEl.innerHTML = '';
    for (let i = 1; i <= levels; i++) {
      const t     = i * isoInterval;
      const color = ISO_COLORS[i - 1];
      const item  = document.createElement('div');
      item.className = 'legend-item';
      item.innerHTML = `
        <div class="legend-swatch" style="background:${color};height:3px;border-radius:1px"></div>
        <div class="legend-label">${t} min</div>`;
      legendEl.appendChild(item);
    }
    legendEl.classList.remove('hidden');
    setStatus(`✅ ${levels} isochrone ring${levels > 1 ? 's' : ''} — every ${isoInterval} min`, 'success');
  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`, 'error');
  }
}

function _renderIso(geojson) {
  if (map.getLayer(ISO_BORDER)) map.removeLayer(ISO_BORDER);
  if (map.getLayer(ISO_FILL))   map.removeLayer(ISO_FILL);
  if (map.getSource(ISO_SRC))   map.removeSource(ISO_SRC);

  map.addSource(ISO_SRC, { type: 'geojson', data: geojson });
  map.addLayer({
    id: ISO_FILL, type: 'fill', source: ISO_SRC,
    paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.13 }
  });
  map.addLayer({
    id: ISO_BORDER, type: 'line', source: ISO_SRC,
    paint: { 'line-color': ['get', 'color'], 'line-width': 2, 'line-opacity': 0.88 }
  });
}

function _isoRemoveLayers() {
  if (map.getLayer(ISO_BORDER)) map.removeLayer(ISO_BORDER);
  if (map.getLayer(ISO_FILL))   map.removeLayer(ISO_FILL);
  if (map.getSource(ISO_SRC))   map.removeSource(ISO_SRC);
}

// ===========================================================================
// LAYER SWITCHER CONTROL
// ===========================================================================
class LayerSwitcherControl {
  onAdd(map) {
    this._map = map;
    this._container = document.createElement('div');
    this._container.className = 'maplibregl-ctrl maplibregl-ctrl-group layer-switcher';
    STYLES.forEach((s, i) => {
      const btn = document.createElement('button');
      btn.type = 'button'; btn.title = s.name; btn.textContent = s.name;
      if (i === 0) btn.classList.add('active');
      btn.addEventListener('click', () => {
        this._container.querySelectorAll('button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        map.setStyle(s.url);
        // styledata event will call _restoreActiveLayers()
        map.once('styledata', () => {
          map.easeTo({
            center: currentCenter, zoom: s.zoom ?? currentZoom,
            pitch: s.pitch ?? currentPitch, bearing: s.bearing ?? currentBearing,
            duration: 900
          });
        });
      });
      this._container.appendChild(btn);
    });
    return this._container;
  }
  onRemove() { this._container.parentNode?.removeChild(this._container); }
}

// ===========================================================================
// SHARED UTILITIES
// ===========================================================================

// Polyline decoder precision 6 → [[lng,lat],…]
function decodePolyline6(encoded) {
  const coords = [];
  let index = 0, lat = 0, lng = 0;
  const len = encoded.length;
  while (index < len) {
    let b, shift = 0, result = 0;
    do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);
    shift = 0; result = 0;
    do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
    lng += (result & 1) ? ~(result >> 1) : (result >> 1);
    coords.push([lng / 1e6, lat / 1e6]);
  }
  return coords;
}

// POST JSON with timeout → returns parsed response
async function fetchJSON(url, body) {
  const ctrl = new AbortController();
  const tid  = setTimeout(() => ctrl.abort(), TIMEOUT);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body:   JSON.stringify(body),
      signal: ctrl.signal
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

// Status bar
function setStatus(msg, type = 'info') {
  const el = document.getElementById('status-bar');
  if (!el) return;
  el.textContent = msg;
  el.className   = `status-${type}`;
  if (type === 'success' || type === 'info') {
    setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 7000);
  }
}

// Create a circular stop marker element
function _makeStopEl(label, color) {
  const el = document.createElement('div');
  el.className   = 'stop-marker';
  el.style.background = color;
  el.textContent = label;
  return el;
}

// Activate a single seg button within a group selector
function _segActivate(groupSelector, btn) {
  document.querySelectorAll(`${groupSelector} .seg`).forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// Toggle the "picking" pulsing state on an action button
function _setBtnPicking(id, on) {
  const btn = document.getElementById(id);
  if (btn) btn.classList.toggle('picking', on);
}

// =============================================================================
// Boot
// =============================================================================
window.addEventListener('load', initMap);
// Expose internals needed by inline oninput handlers in index.html
window.isoState       = isoState;
window.isoHandleClick = isoHandleClick;