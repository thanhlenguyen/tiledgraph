// =============================================================================
// GeoRouting Lab — scripts.js
// Feature 1: Multi-route comparison (Car / Bike / Walk, 3 alternatives)
// Feature 2: VRP / TSP  (VROOM, auto-compute after 2 s debounce)
// Feature 3: Isochrone  (configurable levels + interval, Valhalla)
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
// API
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
const ROUTE_NAMES  = ['Fastest', 'Alternative 1', 'Alternative 2'];

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
  NONE:       'none',
  ROUTE_PICK: 'route_pick',   // picking S then E for route comparison
  VRP_PICK:   'vrp_pick',     // adding stops for VRP/TSP
  ISO_PICK:   'iso_pick',     // single click for isochrone
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

  map.on('click', (e) => {
    const ll = [e.lngLat.lng, e.lngLat.lat];
    if      (activeMode === MODE.ROUTE_PICK) routeHandleClick(ll);
    else if (activeMode === MODE.VRP_PICK)   vrpHandleClick(ll);
    else if (activeMode === MODE.ISO_PICK)   isoHandleClick(ll);
  });

  map.on('error', (e) => console.error('[map]', e));
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
window.switchTab = (name) => {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
  exitMode();
};

// =============================================================================
// FEATURE 1 — ROUTE COMPARISON
// =============================================================================

let routeProfile = 'auto';
let routePoints  = [];       // [{lngLat}, {lngLat}]
let routeMarkers = [];

// Three route source/layer ids
const ROUTE_SRC   = (i) => `route-src-${i}`;
const ROUTE_LAYER_ID = (i) => `route-layer-${i}`;

window.routeSetProfile = (btn) => {
  document.querySelectorAll('#route-profile-seg .seg').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  routeProfile = btn.dataset.profile;
  if (routePoints.length === 2) _runRouteComparison(); // recompute if points already set
};

window.routeActivate = () => {
  routeClearLines();
  routePoints  = [];
  routeMarkers.forEach(m => m.remove());
  routeMarkers = [];
  _updateRoutePickStatus();
  document.getElementById('route-legend').classList.add('hidden');

  enterMode(MODE.ROUTE_PICK);
  const btn = document.querySelector('#tab-route .btn-action');
  btn.classList.add('picking');
  setStatus('Click the map to place the Start point.', 'pick');
};

window.routeClear = () => {
  exitMode();
  routeClearLines();
  routePoints = [];
  routeMarkers.forEach(m => m.remove());
  routeMarkers = [];
  _updateRoutePickStatus();
  document.getElementById('route-legend').classList.add('hidden');
  document.querySelector('#tab-route .btn-action').classList.remove('picking');
  setStatus('', 'info');
};

function routeHandleClick(lngLat) {
  if (routePoints.length >= 2) return;

  routePoints.push(lngLat);
  const isStart = routePoints.length === 1;
  const color   = isStart ? '#22d98a' : '#ff5c5c';
  const label   = isStart ? 'S' : 'E';

  const el = document.createElement('div');
  el.className = 'stop-marker';
  el.style.background = color;
  el.textContent = label;

  const marker = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
      `<b>${isStart ? 'Start' : 'End'}</b><br><span style="font-family:monospace;font-size:11px">${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</span>`
    ))
    .addTo(map);
  routeMarkers.push(marker);
  _updateRoutePickStatus();

  if (routePoints.length === 2) {
    exitMode();
    document.querySelector('#tab-route .btn-action').classList.remove('picking');
    _runRouteComparison();
  } else {
    setStatus('Now click the End point.', 'pick');
  }
}

function _updateRoutePickStatus() {
  const sDot = document.getElementById('route-s-dot');
  const eDot = document.getElementById('route-e-dot');
  sDot.classList.toggle('placed', routePoints.length >= 1);
  eDot.classList.toggle('placed', routePoints.length >= 2);
}

async function _runRouteComparison() {
  setStatus('Computing 3 routes…', 'loading');

  routeClearLines();
  document.getElementById('route-legend').classList.add('hidden');

  const [S, E] = routePoints;
  const baseLocations = [
    { lon: S[0], lat: S[1] },
    { lon: E[0], lat: E[1] }
  ];

  // Build 3 request variants using costing_options to get meaningful alternatives
  const variants = [
    {
      // Variant 0: default (fastest)
      locations: baseLocations,
      costing: routeProfile,
      costing_options: { [routeProfile]: {} },
      directions_options: { language: 'en-US' }
    },
    {
      // Variant 1: avoid highways / prefer local roads
      locations: baseLocations,
      costing: routeProfile,
      costing_options: {
        [routeProfile]: routeProfile === 'auto'
          ? { use_highways: 0.1, use_tolls: 0.0 }
          : routeProfile === 'bicycle'
          ? { use_roads: 0.1, use_hills: 0.3 }
          : {}
      },
      directions_options: { language: 'en-US' }
    },
    {
      // Variant 2: shortest distance (not fastest time)
      locations: baseLocations,
      costing: routeProfile,
      costing_options: {
        [routeProfile]: routeProfile === 'auto'
          ? { use_highways: 0.5, use_tolls: 0.5, shortest: true }
          : routeProfile === 'bicycle'
          ? { use_roads: 0.5, use_hills: 0.8, shortest: true }
          : { shortest: true }
      },
      directions_options: { language: 'en-US' }
    }
  ];

  const results = await Promise.allSettled(
    variants.map(payload => fetchJSON(API.route, payload))
  );

  const legendEl = document.getElementById('route-legend');
  legendEl.innerHTML = '';
  let drawn = 0;

  results.forEach((res, i) => {
    if (res.status !== 'fulfilled') {
      console.warn(`Route variant ${i} failed:`, res.reason);
      return;
    }
    const trip = res.value.trip;
    if (!trip) return;

    const coords = trip.legs.flatMap(leg => decodePolyline6(leg.shape));
    _addRouteLayer(i, coords, ROUTE_COLORS[i]);

    const km   = trip.summary.length.toFixed(1);
    const mins = Math.round(trip.summary.time / 60);

    // Legend entry
    const item = document.createElement('div');
    item.className = 'legend-item';
    item.innerHTML = `
      <div class="legend-swatch" style="background:${ROUTE_COLORS[i]}"></div>
      <div class="legend-label">${ROUTE_NAMES[i]}</div>
      <div class="legend-stat">${km} km · ${mins} min</div>`;
    legendEl.appendChild(item);
    drawn++;
  });

  if (drawn > 0) {
    legendEl.classList.remove('hidden');
    setStatus(`✅ ${drawn} routes drawn. See legend for details.`, 'success');
  } else {
    setStatus('No routes returned — check that points are within the routable area.', 'error');
  }
}

function routeClearLines() {
  [0,1,2].forEach(i => {
    if (map.getLayer(ROUTE_LAYER_ID(i)))  map.removeLayer(ROUTE_LAYER_ID(i));
    if (map.getSource(ROUTE_SRC(i)))      map.removeSource(ROUTE_SRC(i));
  });
}

function _addRouteLayer(i, coordinates, color) {
  // Remove if already exists
  if (map.getLayer(ROUTE_LAYER_ID(i)))  map.removeLayer(ROUTE_LAYER_ID(i));
  if (map.getSource(ROUTE_SRC(i)))      map.removeSource(ROUTE_SRC(i));

  map.addSource(ROUTE_SRC(i), {
    type: 'geojson',
    data: { type:'Feature', geometry:{ type:'LineString', coordinates } }
  });

  // Draw a wider shadow first, then the coloured line on top
  map.addLayer({
    id: ROUTE_LAYER_ID(i), type: 'line', source: ROUTE_SRC(i),
    layout: { 'line-join':'round', 'line-cap':'round' },
    paint: {
      'line-color': color,
      'line-width': i === 0 ? 5 : 3.5,
      'line-opacity': i === 0 ? 0.95 : 0.75,
      'line-dasharray': i === 1 ? ['literal',[4,3]] : i === 2 ? ['literal',[2,4]] : ['literal',[1,0]]
    }
  });
}

// =============================================================================
// FEATURE 2 — VRP / TSP
// =============================================================================

let vrpProfile = 'auto';
let vrpMode    = 'vrp';         // 'vrp' or 'tsp'
let vrpStops   = [];            // [{id, lngLat}]
let vrpMarkers = [];
let vrpDebounceTimer = null;
const VRP_DEBOUNCE_MS = 2000;

const VRP_ROUTE_SRC   = 'vrp-route-src';
const VRP_ROUTE_LAYER = 'vrp-route-layer';

window.vrpSetProfile = (btn) => {
  document.querySelectorAll('#vrp-profile-seg .seg').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  vrpProfile = btn.dataset.profile;
};

window.vrpSetMode = (btn) => {
  document.querySelectorAll('[data-vrpmode]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  vrpMode = btn.dataset.vrpmode;
  document.getElementById('vrp-mode-hint').textContent = vrpMode === 'vrp'
    ? 'VRP: first stop = depot, vehicle returns to start.'
    : 'TSP: open tour — visits all stops in optimal order.';
};

window.vrpActivate = () => {
  enterMode(MODE.VRP_PICK);
  document.querySelector('#tab-vrp .btn-action').classList.add('picking');
  setStatus('Click the map to add stops. Computing starts 2 s after last click.', 'pick');
};

window.vrpClear = () => {
  exitMode();
  clearTimeout(vrpDebounceTimer);
  vrpStops = []; 
  vrpMarkers.forEach(m => m.remove()); vrpMarkers = [];
  _vrpRemoveRoute();
  _vrpUpdateCounter();
  _vrpHideTimer();
  document.getElementById('vrp-legend').classList.add('hidden');
  document.querySelector('#tab-vrp .btn-action').classList.remove('picking');
  setStatus('', 'info');
};

function vrpHandleClick(lngLat) {
  const idx   = vrpStops.length;
  const color = STOP_COLORS[idx % STOP_COLORS.length];
  vrpStops.push({ id: idx + 1, lngLat, color });

  const el = document.createElement('div');
  el.className = 'stop-marker';
  el.style.background = color;
  el.textContent = idx + 1;

  const marker = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
      `<b>Stop ${idx + 1}</b><br><span style="font-family:monospace;font-size:11px">${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</span>`
    ))
    .addTo(map);
  vrpMarkers.push(marker);
  _vrpUpdateCounter();

  // Reset debounce
  clearTimeout(vrpDebounceTimer);
  if (vrpStops.length >= 2) {
    _vrpShowTimer();
    vrpDebounceTimer = setTimeout(() => {
      _vrpHideTimer();
      _runVRP();
    }, VRP_DEBOUNCE_MS);
  }
}

function _vrpUpdateCounter() {
  const n = vrpStops.length;
  document.getElementById('vrp-counter').textContent = `${n} stop${n !== 1 ? 's' : ''} added`;
}

function _vrpShowTimer() {
  const bar  = document.getElementById('vrp-timer-bar');
  const fill = document.getElementById('vrp-timer-fill');
  bar.classList.remove('hidden');
  fill.style.transition = 'none';
  fill.style.width = '0%';
  // Force reflow then animate
  fill.getBoundingClientRect();
  fill.style.transition = `width ${VRP_DEBOUNCE_MS}ms linear`;
  fill.style.width = '100%';
}

function _vrpHideTimer() {
  document.getElementById('vrp-timer-bar').classList.add('hidden');
  document.getElementById('vrp-timer-fill').style.width = '0%';
}

async function _runVRP() {
  if (vrpStops.length < 2) return;

  exitMode();
  document.querySelector('#tab-vrp .btn-action').classList.remove('picking');
  setStatus(`Optimising ${vrpStops.length} stops…`, 'loading');

  const isTSP = vrpMode === 'tsp';
  const depot = vrpStops[0].lngLat;

  const vehicle = isTSP
    ? { id: 1, start: depot, profile: vrpProfile }               // TSP: open, no return
    : { id: 1, start: depot, end: depot, profile: vrpProfile };  // VRP: return to depot

  const jobs = (isTSP ? vrpStops : vrpStops.slice(1)).map(s => ({
    id:       s.id,
    location: s.lngLat,
    service:  30
  }));

  const payload = {
    vehicles: [vehicle],
    jobs,
    // options.g is injected server-side by /optimize_route
  };

  try {
    const result = await fetchJSON(API.optimize, payload);
    console.log('[vrp] result', result);

    const features = result.geojson?.features ?? [];
    if (features.length === 0) {
      setStatus('No route returned — ensure stops are within the routable area.', 'warn');
      return;
    }

    _vrpRemoveRoute();

    // Use the primary stop color (first stop's color) for the route line
    const routeColor = vrpStops[0].color;
    const geojson = result.geojson;

    map.addSource(VRP_ROUTE_SRC, { type: 'geojson', data: geojson });
    map.addLayer({
      id: VRP_ROUTE_LAYER, type: 'line', source: VRP_ROUTE_SRC,
      layout: { 'line-join':'round', 'line-cap':'round' },
      paint: {
        // Color each feature by stop color if multi-vehicle, otherwise use first stop color
        'line-color': routeColor,
        'line-width': 4.5,
        'line-opacity': 0.9
      }
    });

    // Build legend
    const r    = result.vroom.routes[0];
    const km   = (r.distance / 1000).toFixed(1);
    const mins = Math.round(r.duration / 60);

    const legendEl = document.getElementById('vrp-legend');
    legendEl.innerHTML = `
      <div class="legend-item">
        <div class="legend-swatch" style="background:${routeColor}"></div>
        <div class="legend-label">${isTSP ? 'TSP' : 'VRP'} · ${vrpStops.length} stops</div>
        <div class="legend-stat">${km} km · ${mins} min</div>
      </div>`;

    // Add per-stop order to legend
    const steps = r.steps?.filter(s => s.type === 'job') ?? [];
    if (steps.length) {
      const orderLine = document.createElement('div');
      orderLine.className = 'legend-item';
      orderLine.style.flexWrap = 'wrap';
      orderLine.style.gap = '3px';
      const orderLabel = document.createElement('div');
      orderLabel.style.cssText = 'font-size:0.62rem;color:var(--text-faint);width:100%';
      orderLabel.textContent = 'Visit order:';
      orderLine.appendChild(orderLabel);
      steps.forEach(s => {
        const stopIdx = vrpStops.findIndex(st => st.id === s.id);
        const color   = stopIdx >= 0 ? vrpStops[stopIdx].color : '#888';
        const dot = document.createElement('span');
        dot.style.cssText = `display:inline-block;width:14px;height:14px;border-radius:50%;background:${color};font-size:8px;color:#fff;font-weight:700;text-align:center;line-height:14px;font-family:monospace`;
        dot.textContent = s.id;
        orderLine.appendChild(dot);
      });
      legendEl.appendChild(orderLine);
    }

    legendEl.classList.remove('hidden');
    setStatus(`✅ Optimal route: ${km} km · ${mins} min`, 'success');

  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`, 'error');
  }
}

function _vrpRemoveRoute() {
  if (map.getLayer(VRP_ROUTE_LAYER)) map.removeLayer(VRP_ROUTE_LAYER);
  if (map.getSource(VRP_ROUTE_SRC))  map.removeSource(VRP_ROUTE_SRC);
}

// =============================================================================
// FEATURE 3 — ISOCHRONE
// =============================================================================

let isoProfile  = 'auto';
let isoInterval = 10;    // minutes between contours
let isoOriginMarker = null;

const ISO_SRC          = 'iso-src';
const ISO_LAYER_FILL   = 'iso-fill';
const ISO_LAYER_BORDER = 'iso-border';

// Colour ramp for isochrone contours (outermost first = index 0)
const ISO_COLORS = ['#22d98a','#3b9eff','#f5a623','#f43f5e','#a855f7'];

window.isoSetProfile = (btn) => {
  document.querySelectorAll('#iso-profile-seg .seg').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  isoProfile = btn.dataset.profile;
};

window.isoSetInterval = (btn) => {
  document.querySelectorAll('[data-interval]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  isoInterval = parseInt(btn.dataset.interval, 10);
  document.getElementById('iso-interval-val').textContent = `${isoInterval} min`;
};

window.isoActivate = () => {
  enterMode(MODE.ISO_PICK);
  document.querySelector('#tab-isochrone .btn-action').classList.add('picking');
  setStatus('Click a point on the map to draw the isochrone.', 'pick');
};

window.isoClear = () => {
  exitMode();
  _isoRemove();
  if (isoOriginMarker) { isoOriginMarker.remove(); isoOriginMarker = null; }
  document.getElementById('iso-legend').classList.add('hidden');
  document.querySelector('#tab-isochrone .btn-action').classList.remove('picking');
  setStatus('', 'info');
};

async function isoHandleClick(lngLat) {
  exitMode();
  document.querySelector('#tab-isochrone .btn-action').classList.remove('picking');

  // Place / move origin marker
  if (isoOriginMarker) isoOriginMarker.remove();
  const el = document.createElement('div');
  el.className = 'stop-marker';
  el.style.background = '#3b9eff';
  el.textContent = '◉';
  el.style.fontSize = '12px';
  isoOriginMarker = new maplibregl.Marker({ element: el })
    .setLngLat(lngLat)
    .addTo(map);

  const levels   = parseInt(document.getElementById('iso-levels').value, 10);
  const contours = [];
  for (let i = levels; i >= 1; i--) {
    // Outermost contour first so Valhalla fills correctly
    const t = i * isoInterval;
    // Color index: outermost = 0
    contours.push({ time: t, color: ISO_COLORS[i - 1].replace('#','') });
  }

  setStatus(`Computing ${levels}-level isochrone (${isoInterval} min intervals)…`, 'loading');

  const payload = {
    locations: [{ lon: lngLat[0], lat: lngLat[1] }],
    costing:   isoProfile,
    contours,
    polygons:  true
  };

  try {
    const geojson = await fetchJSON(API.isochrone, payload);
    _isoRemove();

    map.addSource(ISO_SRC, { type: 'geojson', data: geojson });
    map.addLayer({
      id: ISO_LAYER_FILL, type: 'fill', source: ISO_SRC,
      paint: { 'fill-color': ['get','color'], 'fill-opacity': 0.12 }
    });
    map.addLayer({
      id: ISO_LAYER_BORDER, type: 'line', source: ISO_SRC,
      paint: { 'line-color': ['get','color'], 'line-width': 2, 'line-opacity': 0.85 }
    });

    // Build legend (innermost first = highest index)
    const legendEl = document.getElementById('iso-legend');
    legendEl.innerHTML = '';
    for (let i = 1; i <= levels; i++) {
      const t     = i * isoInterval;
      const color = ISO_COLORS[i - 1];
      const item  = document.createElement('div');
      item.className = 'legend-item';
      item.innerHTML = `
        <div class="legend-swatch" style="background:${color}; height:3px; border-radius:1px"></div>
        <div class="legend-label">${t} min</div>`;
      legendEl.appendChild(item);
    }
    legendEl.classList.remove('hidden');
    setStatus(`✅ Isochrone drawn — ${levels} levels × ${isoInterval} min`, 'success');

  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`, 'error');
  }
}

function _isoRemove() {
  if (map.getLayer(ISO_LAYER_BORDER)) map.removeLayer(ISO_LAYER_BORDER);
  if (map.getLayer(ISO_LAYER_FILL))   map.removeLayer(ISO_LAYER_FILL);
  if (map.getSource(ISO_SRC))          map.removeSource(ISO_SRC);
}

// =============================================================================
// Utilities
// =============================================================================

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

// Fetch + parse JSON with timeout
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

// =============================================================================
// Layer switcher control
// =============================================================================
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
        map.once('styledata', () => {
          map.easeTo({ center: currentCenter, zoom: s.zoom ?? currentZoom,
            pitch: s.pitch ?? currentPitch, bearing: s.bearing ?? currentBearing, duration: 900 });
        });
      });
      this._container.appendChild(btn);
    });
    return this._container;
  }
  onRemove() { this._container.parentNode?.removeChild(this._container); }
}

// =============================================================================
// Boot
// =============================================================================
window.addEventListener('load', initMap);