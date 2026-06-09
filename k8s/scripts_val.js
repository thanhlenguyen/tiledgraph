// =============================================================================
// ROUTING & SERVICES ANALYSIS APPLICATION
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
    { id: 'basic-style',name: 'Default',   url: '/styles/mt-basic-style.json', pitch: 0, zoom: 10, bearing: 0 },
    { id: 'sat-style',  name: 'Satellite', url: '/styles/mt-sat-style.json', pitch: 0, zoom: 10, bearing: 0 },
    { id: '3d-style',   name: '3D',        url: '/styles/mt-3d-style.json', pitch: 45, zoom: 13, bearing: 0 },
    { id: 'bdf-style',  name: 'BDF',       url: '/styles/mt-bdf-style.json', pitch: 60, zoom: 17, bearing: -20 }
];

// ---------------------------------------------------------------------------
// API endpoints — all requests go through the Nginx /api/ reverse proxy,
// which forwards them to the routing-api backend service on port 5000.
// ---------------------------------------------------------------------------
const BACKEND_URL = '/api';
const API = {
    route:              `${BACKEND_URL}/route_val`,               // Valhalla turn-by-turn routing
    optimize:           `${BACKEND_URL}/optimize_route`,      // VROOM vehicle-route optimisation
    isochrone:          `${BACKEND_URL}/isochrone`,           // Valhalla reachability polygons
    nearestFacility:    `${BACKEND_URL}/nearest_facility_val`,    // Elastic Search + Valhalla
    nearestFacilityPg:  `${BACKEND_URL}/nearest_facility_pg`, // PostGIS spatial query + Valhalla
};

// How long (in milliseconds) to wait for an API response before giving up.
const TIMEOUT = 120_000; // 2 minutes

// Elasticsearch base URL — used only by the Building Search feature.
const ES_URL = '/es';

// ---------------------------------------------------------------------------
// Map defaults
// ---------------------------------------------------------------------------
const DEFAULT_CENTER = [46.597, 24.876]; // Riyadh
const DEFAULT_ZOOM   = 13;

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

// Delay (ms) between the last VRP click and sending the optimisation request.
const VRP_DEBOUNCE_MS = 2000;


// =============================================================================
// SECTION 2: APPLICATION STATE
// All mutable data lives here so any function can read or update it
// without relying on scattered global variables.
// =============================================================================

const state = {
    // --- Core map properties ---
    map:            null,           // The MapLibre GL map instance
    currentCenter:  DEFAULT_CENTER, // Current map centre [lng, lat]
    currentZoom:    DEFAULT_ZOOM,
    currentPitch:   0,
    currentBearing: 0,
    currentStyleId: 'default',

    // --- Active interaction mode ---
    // Only one mode can be active at a time (placing route points, VRP stops, etc.).
    activeMode: 'none', // 'none' | 'route' | 'vrp' | 'iso' | 'facility'

    // --- Route Comparison ---
    route: {
        profile: 'auto', // 'auto' | 'bicycle' | 'pedestrian'
        points:  [],     // [[lng,lat], [lng,lat]] — start and end
        markers: [],     // maplibregl.Marker[] — the S and E pins
        drawn:   [],     // [{ coordinates, color, dash }] — decoded route lines
    },

    // --- VRP / TSP ---
    vrp: {
        profile:        'auto',  // travel mode
        mode:           'vrp',   // 'vrp' = closed loop | 'tsp' = open tour
        stops:          [],      // [{ id, lngLat, color }]
        markers:        [],      // maplibregl.Marker[]
        drawnSegments:  [],      // [{ srcId, layerId, coordinates, color }]
        debounceTimer:  null,
    },

    // --- Isochrone ---
    iso: {
        profile:      'auto',  // travel mode
        interval:     10,      // minutes per ring
        geojson:      null,    // last GeoJSON returned by Valhalla
        originMarker: null,    // the blue origin pin
        originLngLat: null,    // stored so parameter changes can re-run without a new click
    },

    // --- Nearest Facility ---
    facility: {
        type:          'hospital',
        searchId:      0,        // incremented each search to discard stale responses
        originMarker:  null,
        originLngLat:  null,
        facMarkers:    [],       // maplibregl.Marker[] for each found facility
        routeFeatures: [],       // GeoJSON features for all route lines
        facilityType:  'hospital',
    },

    // --- Info Pointer ---
    infoPointerActive: false,
    isDragging:        false,    // true while user is dragging the info panel

    // --- Building Search ---
    searchHighlights: [], // IDs of sources/layers added for highlights
    searchPopup:      null,
};

// Module-level variable for the active dataset (not inside state as it
// mirrors a DOM radio button rather than map interaction data).
let currentDataset = 'va_building'; // 'va_building' | 'spl_unit'


// =============================================================================
// SECTION 3: MAP INITIALISATION
// =============================================================================

/**
 * Create the MapLibre map and wire up all controls and event handlers.
 * This is the application entry point — called once when the page loads.
 */
function initMap() {
    state.map = new maplibregl.Map({
        container: 'map',
        style:     STYLES[0].url,
        center:    state.currentCenter,
        zoom:      state.currentZoom,
        pitch:     state.currentPitch,
        bearing:   state.currentBearing,
        maxPitch:  85,
    });

    // Keep camera state in sync so style switches can restore the exact view.
    state.map.on('moveend', () => {
        state.currentCenter  = state.map.getCenter().toArray();
        state.currentZoom    = state.map.getZoom();
        state.currentPitch   = state.map.getPitch();
        state.currentBearing = state.map.getBearing();
    });

    // Add built-in navigation controls (zoom +/-, compass) and a scale bar.
    state.map.addControl(new maplibregl.NavigationControl(), 'top-right');
    state.map.addControl(new maplibregl.ScaleControl(),      'bottom-left');
    state.map.addControl(new LayerSwitcherControl(),         'bottom-right');

    // Once fully loaded, inject the Info Pointer toggle button.
    state.map.once('load', () => {
        injectInfoPointerBtn();
        console.log('[map] loaded');
    });

    // After every style switch, wait for idle before re-drawing active layers.
    // state.map.on('styledata', () => {
    //     state.map.once('idle', restoreActiveLayers);
    // });

    // Route every map click through the central handler.
    state.map.on('click', handleMapClick);

    // Dataset radio buttons in the Building Search tab.
    document.querySelectorAll('input[name="dataset"]').forEach(r => {
        r.addEventListener('change', e => {
            currentDataset = e.target.value;
            document.getElementById('results').innerHTML = '';
        });
    });

    // Close button on the info panel.
    document.getElementById('close-feature-info')
        ?.addEventListener('click', () => {
            if (state.infoPointerActive) toggleInfoPointer();
        });
}


// =============================================================================
// SECTION 4: MODE SYSTEM
// Only one interactive mode can be active at a time.
// =============================================================================

/**
 * Switch the map into a picking mode — show the crosshair cursor.
 * @param {string} mode - 'route' | 'vrp' | 'iso' | 'facility'
 */
function enterMode(mode) {
    state.activeMode = mode;
    if (!state.infoPointerActive) {
        state.map.getCanvas().style.cursor = 'crosshair';
        document.getElementById('map').classList.add('info-mode');
    }
}

/** Return to the default mode — restore the normal cursor. */
function exitMode() {
    state.activeMode = 'none';
    if (!state.infoPointerActive) {
        state.map.getCanvas().style.cursor = '';
        document.getElementById('map').classList.remove('info-mode');
    }
}

/**
 * Central click dispatcher — routes clicks to the correct feature handler.
 * @param {Object} e - MapLibre click event
 */
function handleMapClick(e) {
    // Info Pointer takes priority; when active it inspects whatever was clicked.
    if (state.infoPointerActive) {
        handleInfoPointerClick(e);
        return;
    }

    const ll = [e.lngLat.lng, e.lngLat.lat]; // [longitude, latitude]

    const handlers = {
        route:    () => routeHandleClick(ll),
        vrp:      () => vrpHandleClick(ll),
        iso:      () => isoHandleClick(ll),
        facility: () => facilityHandleClick(ll),
    };

    handlers[state.activeMode]?.();
}

/**
 * Re-draw every active overlay after a style switch.
 * MapLibre wipes all custom sources/layers when setStyle() is called,
 * so we re-add them from the state objects kept in memory.
 */
function restoreActiveLayers() {
    // Route comparison lines
    state.route.drawn.forEach((d, i) => {
        addRouteLayer(i, d.coordinates, d.color, d.dash);
    });

    // VRP per-segment coloured lines
    state.vrp.drawnSegments.forEach(seg => {
        addVrpSegmentLayer(seg.srcId, seg.layerId, seg.coordinates, seg.color);
    });

    // Isochrone polygon rings
    if (state.iso.geojson) {
        renderIso(state.iso.geojson);
    }

    // Nearest-facility route lines
    if (state.facility.routeFeatures.length > 0) {
        renderFacilityRoutes(state.facility.routeFeatures, state.facility.facilityType);
    }

    // Info Pointer highlight layers
    if (state.infoPointerActive) {
        setupInfoPointerLayers();
    }
}


// =============================================================================
// SECTION 5: TAB SWITCHING
// Only one tab panel is visible at a time. Switching tabs also clears the
// data belonging to every other tab so the map stays uncluttered.
// =============================================================================

window.switchTab = (name) => {
    document.querySelectorAll('.tab').forEach(t =>
        t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.tab-panel').forEach(p =>
        p.classList.toggle('active', p.id === `tab-${name}`));

    exitMode();

    if (name !== 'route')     routeFullClear();
    if (name !== 'vrp')       vrpFullClear();
    if (name !== 'isochrone') isoFullClear();
    if (name !== 'facility')  facilityFullClear();
};


// =============================================================================
// SECTION 6: SHARED UTILITIES
// =============================================================================

/**
 * Decode a Google/Valhalla encoded polyline (precision 6) into an array of
 * [longitude, latitude] coordinate pairs.
 * @param {string} encoded - Encoded polyline string
 * @returns {Array} Array of [lng, lat] pairs
 */
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

/**
 * Send a POST request with a JSON body and return the parsed JSON response.
 * Throws an error if the server responds with a non-2xx status or times out.
 *
 * @param {string} url  - Request URL
 * @param {Object} body - JSON-serialisable request body
 * @returns {Promise<Object>} Parsed JSON response
 */
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
            try {
                const j = await res.json();
                if (j.detail) detail = JSON.stringify(j.detail);
            } catch { /* body wasn't JSON — fall back to status text */ }
            throw new Error(detail);
        }
        return res.json();

    } catch (err) {
        clearTimeout(tid);
        if (err.name === 'AbortError') throw new Error('Request timed out — please try again.');
        throw err;
    }
}

/**
 * Update the status bar at the bottom of the sidebar.
 * Success and info messages auto-clear after 7 seconds.
 *
 * @param {string} msg  - Message to display
 * @param {string} type - 'info' | 'pick' | 'loading' | 'success' | 'warn' | 'error'
 */
function setStatus(msg, type = 'info') {
    const el = document.getElementById('status-bar');
    if (!el) return;
    el.textContent = msg;
    el.className   = `status-${type}`;
    if (type === 'success' || type === 'info') {
        setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 7000);
    }
}

/**
 * Create a small circular marker element for route/VRP stop pins.
 * @param {string} label - Text shown inside the circle (e.g. 'S', 'E', or stop number)
 * @param {string} color - CSS colour string for the background
 * @returns {HTMLElement}
 */
function makeStopEl(label, color) {
    const el = document.createElement('div');
    el.className        = 'stop-marker';
    el.style.background = color;
    el.textContent      = label;
    return el;
}

/**
 * Activate exactly one button in a segmented-control group.
 * @param {string}      groupSelector - CSS selector for the group container
 * @param {HTMLElement} btn           - The button element to mark active
 */
function segActivate(groupSelector, btn) {
    document.querySelectorAll(`${groupSelector} .seg`).forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
}

/**
 * Toggle the pulsing "picking" visual state on an action button.
 * @param {string}  id - Element ID of the button
 * @param {boolean} on - True to enable picking state, false to disable
 */
function setBtnPicking(id, on) {
    document.getElementById(id)?.classList.toggle('picking', on);
}

/**
 * Format a raw GeoJSON property key into a human-readable label.
 * Examples: "building_height" → "Building height"
 *           "UNIT_ID"         → "UNIT ID"
 *
 * @param {string} key - Raw property key
 * @returns {string}   Human-friendly label
 */
function formatPropertyKey(key) {
    if (key === key.toUpperCase()) {
        return key.replace(/_/g, ' ').trim();
    }
    return key
        .replace(/_/g, ' ')
        .replace(/([A-Z])/g, ' $1')
        .replace(/^./, s => s.toUpperCase())
        .trim();
}


// =============================================================================
// SECTION 7: ROUTE COMPARISON (Valhalla)
// Sends the same start/end pair to Valhalla three times with different
// costing options to produce a "fastest", "avoid highways", and
// "shortest distance" alternative for side-by-side comparison.
// =============================================================================

// MapLibre source/layer ID helpers — called with the route index (0, 1, 2).
const ROUTE_SRC = (i) => `route-src-${i}`;
const ROUTE_LYR = (i) => `route-lyr-${i}`;

/** Called when the user clicks a travel-mode segment button (Car / Bike / Walk). */
window.routeSetProfile = (btn) => {
    segActivate('#route-profile-seg', btn);
    state.route.profile = btn.dataset.profile;
    if (state.route.points.length === 2) runRouteComparison();
};

/** Start the two-click point-picking flow. */
window.routeActivate = () => {
    routeResetPicks();
    enterMode('route');
    setBtnPicking('route-pick-btn', true);
    setStatus('Click the map to place the Start point.', 'pick');
};

/** Cancel picking and clear map data. */
window.routeClear = () => {
    exitMode();
    routeResetPicks();
};

/** Called only from switchTab — removes everything without touching mode state. */
function routeFullClear() {
    exitMode();
    routeRemoveLayers();
    state.route.drawn   = [];
    state.route.points  = [];
    state.route.markers.forEach(m => m.remove());
    state.route.markers = [];
    setBtnPicking('route-pick-btn', false);
    document.getElementById('route-legend').classList.add('hidden');
}

/** Reset picks, clear map lines and markers, hide the legend. */
function routeResetPicks() {
    routeRemoveLayers();
    state.route.drawn   = [];
    state.route.points  = [];
    state.route.markers.forEach(m => m.remove());
    state.route.markers = [];
    updateRoutePickStatus();
    setBtnPicking('route-pick-btn', false);
    document.getElementById('route-legend').classList.add('hidden');
    setStatus('', 'info');
}

/**
 * Receive a map click while in Route mode.
 * @param {Array} lngLat - [longitude, latitude]
 */
function routeHandleClick(lngLat) {
    if (state.route.points.length >= 2) return; // ignore extra clicks

    state.route.points.push(lngLat);
    const isStart = state.route.points.length === 1;

    const el = makeStopEl(isStart ? 'S' : 'E', isStart ? '#22d98a' : '#ff5c5c');
    const m  = new maplibregl.Marker({ element: el })
        .setLngLat(lngLat)
        .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
            `<b>${isStart ? 'Start' : 'End'}</b><br>
             <code>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</code>`))
        .addTo(state.map);

    state.route.markers.push(m);
    updateRoutePickStatus();

    if (state.route.points.length === 2) {
        exitMode();
        setBtnPicking('route-pick-btn', false);
        runRouteComparison();
    } else {
        setStatus('Now click the End point.', 'pick');
    }
}

/** Refresh the sidebar pick-status indicators (dots + coordinate text). */
function updateRoutePickStatus() {
    document.getElementById('route-s-dot').classList.toggle('placed', state.route.points.length >= 1);
    document.getElementById('route-e-dot').classList.toggle('placed', state.route.points.length >= 2);
    document.getElementById('route-s-label').textContent = state.route.points.length >= 1
        ? `Start — ${state.route.points[0][1].toFixed(4)}, ${state.route.points[0][0].toFixed(4)}`
        : 'Start — click map';
    document.getElementById('route-e-label').textContent = state.route.points.length >= 2
        ? `End — ${state.route.points[1][1].toFixed(4)}, ${state.route.points[1][0].toFixed(4)}`
        : 'End — click map';
}

/** Swap the start and end points, then recompute the routes. */
window.routeSwapPoints = () => {
    if (state.route.points.length < 2) return;
    [state.route.points[0], state.route.points[1]] = [state.route.points[1], state.route.points[0]];
    state.route.markers[0].setLngLat(state.route.points[0]);
    state.route.markers[1].setLngLat(state.route.points[1]);
    updateRoutePickStatus();
    runRouteComparison();
};

/** Fire three parallel Valhalla requests and draw all results. */
async function runRouteComparison() {
    setStatus('Computing 3 route alternatives…', 'loading');
    routeRemoveLayers();
    state.route.drawn = [];
    document.getElementById('route-legend').classList.add('hidden');

    const [S, E] = state.route.points;
    const base   = [{ lat: S[1], lon: S[0] }, { lat: E[1], lon: E[0] }];
    const p      = state.route.profile;

    // Three request payloads — same origin/destination, different costing options.
    const variants = [
        // 0 — default fastest route
        { locations: base, costing: p,
          costing_options: { [p]: {} },
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

        const coords = trip.legs.flatMap(leg => decodePolyline6(leg.shape));
        const color  = ROUTE_COLORS[i];
        const dash   = DASHES[i];

        state.route.drawn.push({ coordinates: coords, color, dash });
        addRouteLayer(i, coords, color, dash);

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

/** Remove all three route sources and layers from the map. */
function routeRemoveLayers() {
    [0, 1, 2].forEach(i => {
        if (state.map.getLayer(ROUTE_LYR(i)))  state.map.removeLayer(ROUTE_LYR(i));
        if (state.map.getSource(ROUTE_SRC(i))) state.map.removeSource(ROUTE_SRC(i));
    });
}

/**
 * Add (or update) one route line on the map.
 * If the source already exists, setData() is used to avoid a visual flash.
 *
 * @param {number} i           - Route index (0–2)
 * @param {Array}  coordinates - Array of [lng, lat] pairs
 * @param {string} color       - Line colour
 * @param {Array}  dash        - MapLibre dasharray value
 */
function addRouteLayer(i, coordinates, color, dash) {
    const geojson = { type: 'Feature', geometry: { type: 'LineString', coordinates } };

    if (state.map.getSource(ROUTE_SRC(i))) {
        state.map.getSource(ROUTE_SRC(i)).setData(geojson);
        return;
    }

    state.map.addSource(ROUTE_SRC(i), { type: 'geojson', data: geojson });
    state.map.addLayer({
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

window.vrpSetProfile = (btn) => {
    segActivate('#vrp-profile-seg', btn);
    state.vrp.profile = btn.dataset.profile;
    if (state.vrp.stops.length >= 2) runVRP();
};

window.vrpSetMode = (btn) => {
    document.querySelectorAll('[data-vrpmode]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.vrp.mode = btn.dataset.vrpmode;
    if (state.vrp.stops.length >= 2) runVRP();
};

window.vrpActivate = () => {
    enterMode('vrp');
    setBtnPicking('vrp-pick-btn', true);
    setStatus('Click the map to add stops. Optimal route draws 2 s after last click.', 'pick');
};

window.vrpClear = () => {
    exitMode();
    clearTimeout(state.vrp.debounceTimer);
    vrpRemoveAllSegments();
    state.vrp.stops = [];
    state.vrp.markers.forEach(m => m.remove());
    state.vrp.markers = [];
    vrpUpdateCounter();
    vrpHideTimer();
    setBtnPicking('vrp-pick-btn', false);
    document.getElementById('vrp-legend').classList.add('hidden');
    setStatus('', 'info');
};

/** Called from switchTab — clears everything silently. */
function vrpFullClear() {
    exitMode();
    clearTimeout(state.vrp.debounceTimer);
    vrpRemoveAllSegments();
    state.vrp.stops         = [];
    state.vrp.markers.forEach(m => m.remove());
    state.vrp.markers       = [];
    state.vrp.drawnSegments = [];
    vrpUpdateCounter();
    vrpHideTimer();
    setBtnPicking('vrp-pick-btn', false);
    document.getElementById('vrp-legend').classList.add('hidden');
}

/**
 * Receive a map click while in VRP mode — add a new stop.
 * @param {Array} lngLat - [longitude, latitude]
 */
function vrpHandleClick(lngLat) {
    const idx   = state.vrp.stops.length;
    const color = STOP_COLORS[idx % STOP_COLORS.length];
    state.vrp.stops.push({ id: idx + 1, lngLat, color });

    const el = makeStopEl(idx + 1, color);
    const m  = new maplibregl.Marker({ element: el })
        .setLngLat(lngLat)
        .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(
            `<b>Stop ${idx + 1}</b><br>
             <code>${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)}</code>`))
        .addTo(state.map);

    state.vrp.markers.push(m);
    vrpUpdateCounter();

    // Reset the debounce timer every time a stop is added.
    clearTimeout(state.vrp.debounceTimer);
    if (state.vrp.stops.length >= 2) {
        vrpShowTimer();
        state.vrp.debounceTimer = setTimeout(() => {
            vrpHideTimer();
            runVRP();
        }, VRP_DEBOUNCE_MS);
    }
}

/** Update the "N stops added" counter in the sidebar. */
function vrpUpdateCounter() {
    const n = state.vrp.stops.length;
    document.getElementById('vrp-counter').textContent =
        `${n} stop${n !== 1 ? 's' : ''} added`;
}

/** Show an animated progress bar that counts down to the optimisation request. */
function vrpShowTimer() {
    const bar  = document.getElementById('vrp-timer-bar');
    const fill = document.getElementById('vrp-timer-fill');
    bar.classList.remove('hidden');
    fill.style.transition = 'none';
    fill.style.width      = '0%';
    fill.getBoundingClientRect(); // force reflow so transition starts fresh
    fill.style.transition = `width ${VRP_DEBOUNCE_MS}ms linear`;
    fill.style.width      = '100%';
}

function vrpHideTimer() {
    document.getElementById('vrp-timer-bar').classList.add('hidden');
    document.getElementById('vrp-timer-fill').style.width = '0%';
}

/** Send the optimisation request and draw the resulting route. */
async function runVRP() {
    if (state.vrp.stops.length < 2) return;

    exitMode();
    setBtnPicking('vrp-pick-btn', false);
    setStatus(`Optimising ${state.vrp.stops.length} stops…`, 'loading');

    const isTSP = state.vrp.mode === 'tsp';
    const depot  = state.vrp.stops[0].lngLat;

    // In VRP mode the vehicle starts AND ends at the depot (closed loop).
    // In TSP mode the vehicle only has a start (open tour).
    const vehicle = isTSP
        ? { id: 1, start: depot, profile: state.vrp.profile }
        : { id: 1, start: depot, end: depot, profile: state.vrp.profile };

    const jobStops = isTSP ? state.vrp.stops : state.vrp.stops.slice(1);
    const jobs     = jobStops.map(s => ({ id: s.id, location: s.lngLat, service: 30 }));

    try {
        const result = await fetchJSON(API.optimize, { vehicles: [vehicle], jobs });
        console.log('[vrp] result', result);

        const route = result.vroom?.routes?.[0];
        if (!route) {
            setStatus('No route returned — ensure stops are within routable area.', 'warn');
            return;
        }

        // Filter to only the "job" steps in optimal visit order.
        const jobSteps = route.steps?.filter(s => s.type === 'job') ?? [];

        const idToStop = {};
        state.vrp.stops.forEach(s => { idToStop[s.id] = s; });

        let orderedLngLats = [depot];
        jobSteps.forEach(step => {
            const stop = idToStop[step.id];
            if (stop) orderedLngLats.push(stop.lngLat);
        });
        if (!isTSP) orderedLngLats.push(depot); // return to depot for VRP

        // Fetch road geometry for every leg in parallel.
        vrpRemoveAllSegments();
        state.vrp.drawnSegments = [];
        setStatus('Drawing route segments…', 'loading');

        const segmentRequests = orderedLngLats.slice(0, -1).map((from, i) => {
            const to = orderedLngLats[i + 1];

            let color;
            if (i === 0) {
                color = state.vrp.stops[0].color;
            } else {
                const fromStop = state.vrp.stops.find(s =>
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
                    costing: state.vrp.profile,
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

            state.vrp.drawnSegments.push({ srcId, layerId, coordinates: coords, color });
            addVrpSegmentLayer(srcId, layerId, coords, color);
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
                ${isTSP ? 'TSP' : 'VRP'} · ${state.vrp.stops.length} stops
            </div>
            <div class="legend-stat">${km} km · ${mins} min</div>`;
        legendEl.appendChild(summaryRow);

        const orderRow = document.createElement('div');
        orderRow.className = 'legend-item';
        orderRow.style.cssText = 'flex-wrap:wrap;gap:3px';

        const orderLbl = document.createElement('div');
        orderLbl.style.cssText = 'font-size:0.6rem;color:var(--text-faint);width:100%;margin-bottom:2px';
        orderLbl.textContent   = 'Visit order →';
        orderRow.appendChild(orderLbl);
        orderRow.appendChild(makeLegendDot(state.vrp.stops[0].color, state.vrp.stops[0].id));

        jobSteps.forEach(step => {
            const stop = idToStop[step.id];
            if (stop) orderRow.appendChild(makeLegendDot(stop.color, stop.id));
        });

        if (!isTSP) {
            const arr = document.createElement('span');
            arr.style.cssText = 'font-size:10px;color:var(--text-faint);align-self:center';
            arr.textContent   = '↩';
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

/**
 * Create a small coloured circle for the visit-order row in the VRP legend.
 * @param {string}        color - CSS colour string
 * @param {string|number} label - Text displayed inside the dot
 * @returns {HTMLElement}
 */
function makeLegendDot(color, label) {
    const dot = document.createElement('span');
    dot.style.cssText = `
        display:inline-flex;align-items:center;justify-content:center;
        width:16px;height:16px;border-radius:50%;background:${color};
        font-size:8px;color:#fff;font-weight:700;font-family:var(--font-mono)`;
    dot.textContent = label;
    return dot;
}

/** Remove every VRP segment source and layer from the map. */
function vrpRemoveAllSegments() {
    state.vrp.drawnSegments.forEach(({ srcId, layerId }) => {
        if (state.map.getLayer(layerId)) state.map.removeLayer(layerId);
        if (state.map.getSource(srcId))  state.map.removeSource(srcId);
    });
    state.vrp.drawnSegments = [];
}

/**
 * Add (or update) a single VRP route segment on the map.
 * setData() is used when the source already exists to avoid a visual flash.
 *
 * @param {string} srcId       - MapLibre source ID
 * @param {string} layerId     - MapLibre layer ID
 * @param {Array}  coordinates - Array of [lng, lat] pairs
 * @param {string} color       - Line colour
 */
function addVrpSegmentLayer(srcId, layerId, coordinates, color) {
    const geojson = { type: 'Feature', geometry: { type: 'LineString', coordinates } };

    if (state.map.getSource(srcId)) {
        state.map.getSource(srcId).setData(geojson);
        return;
    }

    state.map.addSource(srcId, { type: 'geojson', data: geojson });
    state.map.addLayer({
        id: layerId, type: 'line', source: srcId,
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': color, 'line-width': 4.5, 'line-opacity': 0.92 },
    });
}


// =============================================================================
// SECTION 9: ISOCHRONE (Valhalla)
// An isochrone shows all locations reachable within a given travel time.
// The result is a set of polygon rings — one per configured time interval.
// =============================================================================

// Stable MapLibre source/layer IDs for the isochrone layers.
const ISO_SRC    = 'iso-src';
const ISO_FILL   = 'iso-fill';
const ISO_BORDER = 'iso-border';

window.isoSetProfile = (btn) => {
    segActivate('#iso-profile-seg', btn);
    state.iso.profile = btn.dataset.profile;
    if (state.iso.originLngLat) isoHandleClick(state.iso.originLngLat);
};

window.isoSetInterval = (btn) => {
    document.querySelectorAll('[data-interval]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.iso.interval = parseInt(btn.dataset.interval, 10);
    document.getElementById('iso-interval-val').textContent = `${state.iso.interval} min`;
    if (state.iso.originLngLat) isoHandleClick(state.iso.originLngLat);
};

window.isoActivate = () => {
    enterMode('iso');
    setBtnPicking('iso-pick-btn', true);
    setStatus('Click a location on the map to compute the isochrone.', 'pick');
};

window.isoClear = () => {
    exitMode();
    isoFullClear();
    setStatus('', 'info');
};

/** Called from switchTab — remove everything silently. */
function isoFullClear() {
    exitMode();
    isoRemoveLayers();
    state.iso.geojson      = null;
    state.iso.originLngLat = null;
    if (state.iso.originMarker) {
        state.iso.originMarker.remove();
        state.iso.originMarker = null;
    }
    setBtnPicking('iso-pick-btn', false);
    document.getElementById('iso-legend').classList.add('hidden');
}

/**
 * Handle a map click (or a parameter-change re-run).
 * @param {Array} lngLat - [longitude, latitude]
 */
async function isoHandleClick(lngLat) {
    exitMode();
    setBtnPicking('iso-pick-btn', false);

    // Only move the origin marker when the click position has changed.
    const isNewOrigin = !state.iso.originLngLat
        || state.iso.originLngLat[0] !== lngLat[0]
        || state.iso.originLngLat[1] !== lngLat[1];

    if (isNewOrigin) {
        if (state.iso.originMarker) state.iso.originMarker.remove();
        const el = makeStopEl('◉', '#3b9eff');
        el.style.fontSize      = '11px';
        state.iso.originMarker = new maplibregl.Marker({ element: el }).setLngLat(lngLat).addTo(state.map);
        state.iso.originLngLat = lngLat;
    }

    const levels = parseInt(document.getElementById('iso-levels').value, 10);

    // Valhalla expects ascending time values; strip '#' from colours.
    const contours = [];
    for (let i = 1; i <= levels; i++) {
        contours.push({ time: i * state.iso.interval, color: ISO_COLORS[i - 1].replace('#', '') });
    }

    setStatus(`Computing ${levels}-level isochrone (${state.iso.interval} min intervals)…`, 'loading');

    const payload = {
        locations:  [{ lat: lngLat[1], lon: lngLat[0] }],
        costing:    state.iso.profile,
        contours,
        polygons:   true,
        denoise:    0.5,  // merge small disconnected blobs
        generalize: 50,   // simplify geometry by ~50 m for faster rendering
    };

    try {
        const geojson = await fetchJSON(API.isochrone, payload);
        state.iso.geojson = geojson;
        isoRemoveLayers();
        renderIso(geojson);

        const legendEl = document.getElementById('iso-legend');
        legendEl.innerHTML = '';
        for (let i = 1; i <= levels; i++) {
            const item = document.createElement('div');
            item.className = 'legend-item';
            item.innerHTML = `
                <div class="legend-swatch"
                     style="background:${ISO_COLORS[i - 1]};height:3px;border-radius:1px"></div>
                <div class="legend-label">${i * state.iso.interval} min</div>`;
            legendEl.appendChild(item);
        }
        legendEl.classList.remove('hidden');
        setStatus(`✅ ${levels} isochrone ring${levels > 1 ? 's' : ''} — every ${state.iso.interval} min`, 'success');

    } catch (err) {
        console.error(err);
        setStatus(`Error: ${err.message}`, 'error');
    }
}

/**
 * Add (or update) the isochrone polygon layers.
 * setData() is used when the source already exists to avoid a visual flash.
 * @param {Object} geojson - GeoJSON FeatureCollection from Valhalla
 */
function renderIso(geojson) {
    if (state.map.getSource(ISO_SRC)) {
        state.map.getSource(ISO_SRC).setData(geojson);
        return;
    }

    state.map.addSource(ISO_SRC, { type: 'geojson', data: geojson });

    // Semi-transparent fill coloured by the 'color' property in each feature.
    state.map.addLayer({
        id: ISO_FILL, type: 'fill', source: ISO_SRC,
        paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.13 },
    });

    // Crisp border on each ring.
    state.map.addLayer({
        id: ISO_BORDER, type: 'line', source: ISO_SRC,
        paint: { 'line-color': ['get', 'color'], 'line-width': 2, 'line-opacity': 0.88 },
    });
}

/** Remove isochrone layers and source from the map. */
function isoRemoveLayers() {
    if (state.map.getLayer(ISO_BORDER)) state.map.removeLayer(ISO_BORDER);
    if (state.map.getLayer(ISO_FILL))   state.map.removeLayer(ISO_FILL);
    if (state.map.getSource(ISO_SRC))   state.map.removeSource(ISO_SRC);
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
//   facility.searchId increments on every click. Each async chain captures
//   its own ID; if the user clicks again before the response arrives, the old
//   response is silently discarded.
// =============================================================================

// Stable MapLibre source/layer IDs for the facility route lines.
const FAC_ROUTE_SRC  = 'fac-routes';
const FAC_ROUTE_LYR  = 'fac-routes-lyr';
const FAC_ROUTE_GLOW = 'fac-routes-glow'; // wide blurred halo for visual depth

window.facilitySetType = (btn) => {
    document.querySelectorAll('#facility-type-seg .seg').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.facility.type = btn.dataset.ftype;
};

window.facilityActivate = () => {
    enterMode('facility');
    setBtnPicking('fac-pick-btn', true);
    setStatus('Click the map to search for nearby facilities.', 'pick');
};

window.facilityClear = () => {
    exitMode();
    facilityFullClear();
    setStatus('', 'info');
};

/** Called from switchTab — clears everything silently. */
function facilityFullClear() {
    exitMode();
    state.facility.searchId++; // invalidate any in-flight request
    if (state.facility.originMarker) {
        state.facility.originMarker.remove();
        state.facility.originMarker = null;
    }
    state.facility.facMarkers.forEach(m => {
        if (m._popup?.isOpen()) m._popup.remove();
        m.remove();
    });
    state.facility.facMarkers    = [];
    state.facility.originLngLat  = null;
    state.facility.routeFeatures = [];
    state.facility.facilityType  = 'hospital';
    removeFacilityRouteLayers();
    document.getElementById('fac-legend').classList.add('hidden');
    document.getElementById('fac-legend').innerHTML   = '';
    document.getElementById('fac-status').textContent = '';
    setBtnPicking('fac-pick-btn', false);
}

/** Fully remove the route-line source and layers (used on clear and style switch). */
function removeFacilityRouteLayers() {
    if (state.map.getLayer(FAC_ROUTE_GLOW)) state.map.removeLayer(FAC_ROUTE_GLOW);
    if (state.map.getLayer(FAC_ROUTE_LYR))  state.map.removeLayer(FAC_ROUTE_LYR);
    if (state.map.getSource(FAC_ROUTE_SRC)) state.map.removeSource(FAC_ROUTE_SRC);
}

/**
 * Receive a map click while in Facility mode.
 * @param {Array} lngLat - [longitude, latitude]
 */
function facilityHandleClick(lngLat) {
    // Clear previous facility markers and legend.
    state.facility.facMarkers.forEach(m => {
        if (m._popup?.isOpen()) m._popup.remove();
        m.remove();
    });
    state.facility.facMarkers = [];
    document.getElementById('fac-legend').classList.add('hidden');
    document.getElementById('fac-legend').innerHTML   = '';
    document.getElementById('fac-status').textContent = '';

    // Place the origin "📍" pin.
    if (state.facility.originMarker) state.facility.originMarker.remove();
    const pinEl = document.createElement('div');
    pinEl.style.cssText = 'font-size:22px;line-height:1;cursor:default;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.6))';
    pinEl.textContent   = '📍';
    state.facility.originMarker = new maplibregl.Marker({ element: pinEl }).setLngLat(lngLat).addTo(state.map);
    state.facility.originLngLat = lngLat;
    state.facility.facilityType = state.facility.type;

    exitMode();
    setBtnPicking('fac-pick-btn', false);
    runFacilitySearch(lngLat, state.facility.type);
}

/**
 * Query the backend for nearby facilities and draw the results.
 * @param {Array}  lngLat - [longitude, latitude]
 * @param {string} ftype  - Facility type key (e.g. 'hospital')
 */
async function runFacilitySearch(lngLat, ftype) {
    const myId     = ++state.facility.searchId; // capture this search's unique ID
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

        if (myId !== state.facility.searchId) return; // superseded — discard

        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { const j = await res.json(); detail = j.detail || detail; } catch { /* ignore */ }
            throw new Error(detail);
        }

        data = await res.json();
        console.log('%c Facility API response:', 'color:#22d98a;font-weight:bold', data);
        console.table(data.facilities);

    } catch (err) {
        if (myId !== state.facility.searchId) return;
        setStatus(`Error: ${err.message}`, 'error');
        return;
    }

    if (myId !== state.facility.searchId) return;

    if (!data.facilities || data.facilities.length === 0) {
        setStatus(`No ${cfg.label} found within ${radiusKm} km.`, 'warn');
        renderFacilityRoutes([], ftype);
        return;
    }

    const facilities = data.facilities;

    // Flatten all route features into one array. Each feature gets a
    // facility_rank property so the line-width expression can draw the nearest
    // facility's route thicker than farther ones.
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

    // Store for style-switch restore.
    state.facility.routeFeatures = allFeatures;
    renderFacilityRoutes(allFeatures, ftype);

    // Place a marker for each facility.
    state.facility.facMarkers.forEach(m => {
        if (m._popup?.isOpen()) m._popup.remove();
        m.remove();
    });
    state.facility.facMarkers = [];

    facilities.forEach((f, index) => {
        const rank = index + 1;
        const el   = document.createElement('div');
        el.className        = 'fac-marker';
        el.style.background = cfg.color;
        el.innerHTML        = cfg.icon;

        const badge = document.createElement('div');
        badge.className         = 'fac-badge';
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
            .addTo(state.map);

        marker._popup = popup;

        // Show popup on hover; hide with a short delay so cursor can reach it.
        let closeTimeout;
        el.addEventListener('mouseenter', () => {
            clearTimeout(closeTimeout);
            if (!popup.isOpen()) marker.togglePopup();
        });
        el.addEventListener('mouseleave', () => {
            closeTimeout = setTimeout(() => {
                if (popup.isOpen()) marker.togglePopup();
            }, 200);
        });

        state.facility.facMarkers.push(marker);
    });

    // Build the sidebar legend list.
    const legendEl = document.getElementById('fac-legend');
    legendEl.innerHTML = '';
    facilities.forEach((f, i) => {
        const item = document.createElement('div');
        item.className = 'fac-item';
        item.onclick   = () => state.facility.facMarkers[i]?.togglePopup();
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
    state.map.fitBounds(bounds, {
        padding: { top: 80, bottom: 80, left: sidebarW, right: 80 },
        maxZoom: 14, duration: 1200,
    });

    const closest = facilities[0];
    setStatus(`✅ ${facilities.length} ${cfg.label}(s) found — nearest is ${closest.name} (${closest.travel_minutes} min)`, 'success');
    document.getElementById('fac-status').textContent =
        `${facilities.length} result${facilities.length > 1 ? 's' : ''} — hover markers for details`;
}

/**
 * Draw (or update) the facility route lines.
 * setData() is used when the source already exists to prevent visual flash
 * between searches, even when switching facility type.
 *
 * @param {Array}  features - GeoJSON Feature array for all route lines
 * @param {string} ftype    - Facility type key
 */
function renderFacilityRoutes(features, ftype) {
    const cfg         = FACILITY_CONFIG[ftype] || FACILITY_CONFIG['hospital'];
    const color       = cfg.color;
    const geojsonData = { type: 'FeatureCollection', features };

    if (state.map.getSource(FAC_ROUTE_SRC)) {
        state.map.getSource(FAC_ROUTE_SRC).setData(geojsonData);
        if (state.map.getLayer(FAC_ROUTE_GLOW)) state.map.setPaintProperty(FAC_ROUTE_GLOW, 'line-color', color);
        if (state.map.getLayer(FAC_ROUTE_LYR))  state.map.setPaintProperty(FAC_ROUTE_LYR,  'line-color', color);
        return;
    }

    state.map.addSource(FAC_ROUTE_SRC, { type: 'geojson', data: geojsonData });

    // Wide blurred halo — gives depth and makes the route stand out on busy maps.
    state.map.addLayer({
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
    state.map.addLayer({
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

// Always-open: apply 'open' class as soon as the DOM is ready.
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('search-section')?.classList.add('open');
});

// Kept for any existing onclick="toggleSearchPanel()" in HTML — does nothing.
window.toggleSearchPanel = function () { /* search panel is always open */ };

window.searchUnits = async function() {
    const q         = document.getElementById('searchInput').value.trim();
    const resultsEl = document.getElementById('results');

    if (!q) {
        resultsEl.innerHTML = '';
        resultsEl.classList.remove('has-results');
        return;
    }

    resultsEl.innerHTML = '<div class="search-loading">Searching…</div>';
    resultsEl.classList.add('has-results');
    // openSearchPanel();
    searchClearHighlight(); // clear any previous state before a new search

    const index  = currentDataset === 'va_building' ? 'building_floors' : 'building_unit';
    const fields = currentDataset === 'va_building'
        ? ['properties.VerticalAddress^3', 'properties.ShortAddress^1.8']
        : ['properties.UnitAddres^3', 'properties.LabelNames^1.5', 'properties.NAME', ];

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
            item.onclick = () => zoomToFeature(feat, item, isUnit);
            resultsEl.appendChild(item);
        });

        expandSearchBody(true);

    } catch (err) {
        resultsEl.innerHTML = `<div class="search-error">Error: ${err.message}</div>`;
    }
};

// function openSearchPanel() {
//     document.getElementById('search-section').classList.add('open');
// }

function expandSearchBody(hasResults) {
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

/**
 * Zoom to a search result feature and highlight it as a 3D extrusion.
 * The BDF style is always activated because it contains the indoor data layers.
 *
 * @param {Object}      feature        - Elasticsearch document (_source)
 * @param {HTMLElement} clickedElement - The result list item that was clicked
 * @param {boolean}     isUnit         - True for va_building dataset
 */
async function zoomToFeature(feature, clickedElement, isUnit = true) {
    if (!state.map) { console.warn('Map not ready'); return; }

    // Destroy previous popup and highlight synchronously before any async work.
    searchClearHighlight();
    document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
    clickedElement.classList.add('active');

    if (!feature.geometry) { setStatus('No geometry for this feature.', 'warn'); return; }

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

    const doHighlight = () => {
        state.searchHighlights.push(id);

        // Clean up any previous layer/source with this ID to avoid
        // "already exists" errors on rapid same-result clicks.
        if (state.map.getLayer(id))  state.map.removeLayer(id);
        if (state.map.getSource(id)) state.map.removeSource(id);
        state.map.addSource(id, {
            type: 'geojson',
            data: { type: 'Feature', geometry: feature.geometry, properties: props },
        });

        let extrusionBase, extrusionHeight;
        if (currentDataset === 'va_building') {
            const floorH    = (props.BuildingHeight || 0) / (props.FloorsAboveGround || 1);
            extrusionBase   = floorH * (props.FloorID || 0);
            extrusionHeight = extrusionBase + floorH;
        } else {
            extrusionBase   = props.Base || 0;
            extrusionHeight = extrusionBase + (props.HEIGHT || 4.25);
        }

        // Find the first symbol layer so the extrusion renders beneath labels.
        const styleLayers    = state.map.getStyle().layers || [];
        const firstSymbolId  = styleLayers.find(l => l.type === 'symbol')?.id;

        state.map.addLayer(
            {
                id, type: 'fill-extrusion', source: id,
                paint: {
                    'fill-extrusion-color':   '#ff5c00',
                    'fill-extrusion-opacity': 0.92,
                    'fill-extrusion-base':    extrusionBase,
                    'fill-extrusion-height':  extrusionHeight,
                },
            },
            firstSymbolId // insert below symbol layers so labels stay on top
        );

        const sidebarW = parseInt(
            getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w') || '340'
        ) + 20;

        state.map.fitBounds(bounds, {
            padding: { top: 100, bottom: 100, left: sidebarW, right: 100 },
            maxZoom: 19.5,
            pitch:   58,
            bearing: -18,
            duration: 1600,
            essential: true,
        });

        setTimeout(() => {
            if (!state.map.getSource(id)) return; // highlight was superseded
            const popupHTML = buildPopupHTML(props, isUnit);
            const popupPos  = getPopupAnchorPosition(bounds);

            // Destroy any popup that slipped through (e.g. from a prior rapid click).
            if (state.searchPopup) {
                state.searchPopup.remove();
                state.searchPopup = null;
            }
            state.searchPopup = new maplibregl.Popup({
                offset: 15, closeButton: true, maxWidth: '300px', className: 'unit-popup',
            })
                .setLngLat(popupPos)
                .setHTML(popupHTML)
                .addTo(state.map);
            state.searchPopup.on('close', () => { state.searchPopup = null; });
        }, 900);
    };

    // Always switch to BDF style before highlighting indoor data.
    const bdf = STYLES.find(s => s.id === 'bdf');
    const currentUrl = state.map.getStyle()?.name ?? state.map.getStyle()?.sprite ?? '';
    const isBdfLoaded = state.currentStyleId === 'bdf';

    if (bdf && !isBdfLoaded) {
        state.currentStyleId = 'bdf';
        state.map.setStyle(bdf.url);
        document.querySelectorAll('.layer-switcher button').forEach(b =>
            b.classList.toggle('active', b.textContent === 'BDF'));
        state.map.once('idle', () => {
            restoreActiveLayers();
            doHighlight();      // ← single idle, not chained once('idle')
        });
    } else {
        doHighlight();          // already on BDF — highlight immediately
    }
}

/**
 * Build the HTML string shown inside a search-result popup.
 * @param {Object}  props  - Feature properties
 * @param {boolean} isUnit - True for va_building dataset
 * @returns {string} HTML string
 */
function buildPopupHTML(props, isUnit) {
    let html = `
        <div style="font-size:14px;line-height:1.6;max-width:280px;color:var(--text)">
            <strong style="font-size:16px;color:var(--text)">
                ${props.FloorUsage || props.VerticalAddress || props.LabelNames ||  props.UNIT_ID || 'Feature'}
            </strong><br>`;

    if (isUnit) {
        html += `
            <b>Address:</b>      ${props.VerticalAddress || props.ShortAddress || '—'}<br>
            <b>Floor:</b>        ${props.FloorNumber != null ? (props.FloorName || props.FloorID) : '—'}<br>
            <b>Height:</b>       ${props.BuildingHeight != null ? (props.FloorID != null ? (((props.FloorNumber +1 ) * 4).toFixed(2) + 'm') : (props.BuildingHeight.toFixed(2) + 'm')):'-'}<br>
            <b>NumberUnit:</b>   ${props.NoOfUnits || '—'}`;
    } else {
        html += `
            <b>ID:</b>            ${props.UNIT_ID || '—'}<br>
            <b>Unit Address:</b>  ${props.UnitAddres || '—'}<br>
            <b>Use Type:</b>      ${props.USE_TYPE || '—'}<br>
            <b>Floor:</b>         ${props.Base != null ? props.Base.toFixed(2) + 'm' : '—'}`;
    }

    html += '</div>';
    return html;
}

/**
 * Return a [lng, lat] position for the popup anchor — slightly outside the
 * feature's bounding box so the popup doesn't cover it.
 *
 * @param {Object} bounds - MapLibre LngLatBounds
 * @returns {Array} [lng, lat]
 */
function getPopupAnchorPosition(bounds) {
    const ne = bounds.getNorthEast();
    const sw = bounds.getSouthWest();
    return [
        ne.lng + (ne.lng - sw.lng) * 0.2,
        sw.lat + (ne.lat - sw.lat) * 0.65,
    ];
}

/** Remove all search-highlight layers/sources and close any open popup. */
function searchClearHighlight() {
    if (state.searchPopup) { state.searchPopup.remove(); state.searchPopup = null; }
    state.searchHighlights.forEach(id => {
        if (state.map?.getLayer(id))  state.map.removeLayer(id);
        if (state.map?.getSource(id)) state.map.removeSource(id);
    });
    state.searchHighlights = [];
    document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
}


// =============================================================================
// SECTION 12: INFO POINTER
// When active, clicking any visible map feature opens a floating panel
// showing the feature's raw GeoJSON properties. Useful for inspecting
// road types, building attributes, or any other layer data.
// =============================================================================

/**
 * Inject the ℹ️ toggle button into the top-right control group.
 * Called once after the map loads.
 */
function injectInfoPointerBtn() {
    const topRight = document.querySelector('.maplibregl-ctrl-top-right');
    if (!topRight) return;

    const group = document.createElement('div');
    group.className      = 'maplibregl-ctrl maplibregl-ctrl-group';
    group.style.marginTop = '8px';

    const btn     = document.createElement('button');
    btn.className = 'info-pointer-btn';
    btn.type      = 'button';
    btn.innerHTML = 'ℹ️';
    btn.title     = 'Toggle Info Pointer';
    btn.onclick   = () => toggleInfoPointer();

    group.appendChild(btn);
    topRight.appendChild(group);
}

/** Turn the Info Pointer on or off. */
window.toggleInfoPointer = function() {
    state.infoPointerActive = !state.infoPointerActive;
    const btn   = document.querySelector('.info-pointer-btn');
    const panel = document.getElementById('feature-info-panel');
    const mapEl = document.getElementById('map');

    if (state.infoPointerActive) {
        setupInfoPointerLayers();
        btn?.classList.add('active');
        mapEl.classList.add('info-mode');
        state.map.getCanvas().style.cursor = 'crosshair';
        panel.classList.remove('hidden');
        resetInfoPanelPosition();
        setupDraggablePanel();
        updateFeatureInfo('<p class="info-hint">Click on any feature to inspect its properties</p>');
    } else {
        btn?.classList.remove('active');
        mapEl.classList.remove('info-mode');
        state.map.getCanvas().style.cursor = state.activeMode !== 'none' ? 'crosshair' : '';
        panel.classList.add('hidden');
        clearFeatureHighlight();
    }
};

/**
 * Add the three highlight layers (fill, line, circle) that visually mark
 * whichever feature the user clicked. Skipped if already present.
 */
function setupInfoPointerLayers() {
    if (state.map.getSource('feature-highlight')) return;

    state.map.addSource('feature-highlight', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
    });

    const layers = [
        // Flat polygon fill (2D styles)
        { id: 'hl-fill',   type: 'fill',   filter: ['all', ['==', ['geometry-type'], 'Polygon'], ['!', ['to-boolean', ['get', 'hl_use_3d']]]],
          paint: { 'fill-color': '#3b82f6', 'fill-opacity': 0.3 } },

        // Flat line (all styles — outlines the footprint even in 3D)
        // { id: 'hl-line',   type: 'line',
        //   filter: ['any', ['==', ['geometry-type'], 'LineString'], ['==', ['geometry-type'], 'Polygon']],
        //   paint: { 'line-color': '#3b82f6', 'line-width': 2.5 } },

        // Point circle
        { id: 'hl-circle', type: 'circle', filter: ['==', ['geometry-type'], 'Point'],
          paint: { 'circle-radius': 7, 'circle-color': '#3b82f6', 'circle-opacity': 0.7 } },

        // 3D extrusion — only shown when hl_use_3d flag is set
        {
            id: 'hl-extrusion', type: 'fill-extrusion',
            filter: ['all', ['==', ['geometry-type'], 'Polygon'], ['to-boolean', ['get', 'hl_use_3d']]],
            paint: {
                'fill-extrusion-color':   '#3b82f6',
                'fill-extrusion-opacity': 0.65,
                'fill-extrusion-base':    ['coalesce', ['get', 'hl_base'],   0],
                'fill-extrusion-height':  ['coalesce', ['get', 'hl_height'], 0],
            },
        },
    ];

    layers.forEach(l => {
        if (!state.map.getLayer(l.id)) {
            state.map.addLayer({ ...l, source: 'feature-highlight' });
        }
    });
}

/**
 * Handle a map click while Info Pointer is active.
 * @param {Object} e - MapLibre click event
 */
function handleInfoPointerClick(e) {
    if (state.isDragging) return; // ignore click at end of a drag

    const features = state.map.queryRenderedFeatures(e.point);
    if (!features || features.length === 0) {
        clearFeatureHighlight();
        updateFeatureInfo('<p class="info-hint">No features at this location</p>');
        return;
    }

    // Filter out overlay layers we added ourselves so only basemap features are inspected.
    const valid = features.filter(f => {
        const id = f.layer.id;
        return !id.startsWith('hl-')        &&
               !id.startsWith('route-src-') &&
               !id.startsWith('vrp-')       &&
               !id.startsWith('iso-')       &&
               !id.startsWith('fac-')       &&
               !id.startsWith('search-hl');
    });

    if (!valid.length) {
        clearFeatureHighlight();
        updateFeatureInfo('<p class="info-hint">No base-layer features here</p>');
        return;
    }

    const feat = valid[0];
    highlightFeature(feat);
    displayFeatureInfo(feat);
}

/**
 * Update the 'feature-highlight' source so the blue overlay appears on the map.
 * @param {Object} feature - A GeoJSON feature from queryRenderedFeatures()
 */
function highlightFeature(feature) {
    // clearFeatureHighlight();
    const src = state.map.getSource('feature-highlight');
    if (!src) return;

    // Decide whether to show a 3D extrusion based on current style + geometry.
    const is3dStyle = ['3d', 'bdf'].includes(state.currentStyleId);
    const geomType  = feature.geometry?.type;
    const isPolygon = geomType === 'Polygon' || geomType === 'MultiPolygon';

    const props = { ...(feature.properties || {}) };

    if (is3dStyle && isPolygon) {
        const heights    = resolveExtrusionHeights(props);
        props.hl_use_3d  = true;
        props.hl_base    = heights.base;
        props.hl_height  = heights.height;
    } else {
        props.hl_use_3d  = false;
        props.hl_base    = 0;
        props.hl_height  = 0;
    }

    // queryRenderedFeatures returns MapLibre Feature objects whose geometry
    // must be extracted via toJSON() — direct spreading loses the geometry.
    const geojsonFeature = {
        type:       'Feature',
        geometry:   feature.geometry.toJSON ? feature.geometry.toJSON() : feature.geometry,
        properties: props,
    };

    src.setData({ type: 'FeatureCollection', features: [geojsonFeature] });
}

/**
 * Derive fill-extrusion base and height from a feature's raw properties.
 * Handles the three common schemas present in this application:
 *   • Standard OSM/Valhalla buildings  (height / building:height / levels)
 *   • va_building floor units          (BuildingHeight / FloorID / FloorsAboveGround)
 *   • SPL indoor units                 (Base / HEIGHT)
 *
 * @param {Object} props - Raw GeoJSON feature properties
 * @returns {{ base: number, height: number }}
 */
function resolveExtrusionHeights(props) {
    const num = (v) => (v !== null && v !== undefined && !isNaN(+v)) ? +v : null;

    // ── Schema 1: SPL indoor unit (Base + HEIGHT) ──────────────────────────
    if (num(props.Base) !== null && num(props.HEIGHT) !== null) {
        const base   = num(props.Base);
        const height = base + num(props.HEIGHT);
        return { base, height };
    }

    // ── Schema 2: va_building floor unit (BuildingHeight / FloorID) ────────
    if (num(props.BuildingHeight) !== null && num(props.FloorID) !== null) {
        const floors   = num(props.FloorsAboveGround) || Math.max(num(props.FloorID) + 1, 1);
        const floorH   = num(props.BuildingHeight) / floors;
        const base     = floorH * num(props.FloorID);
        const height   = base + floorH;
        return { base, height };
    }

    // ── Schema 3: OSM-style building (height / building:height / levels) ───
    const rawH =   num(props['height'])
                ?? num(props['render_height'])
                ?? num(props['building:height'])
                ?? num(props['building_height']);

    if (rawH !== null) {
        const base = num(props['min_height'])
                  ?? num(props['building:min_height'])
                  ?? 0;
        return { base, height: base + rawH };
    }

    // Estimate from floor count if no explicit height
    const levels = num(props['building:levels'])
                ?? num(props['levels'])
                ?? num(props['floors']);
    if (levels !== null) {
        return { base: 0, height: levels * 3.2 }; // ~3.2 m per floor
    }

    // ── Fallback: use whatever render_min_height is present, or 0/4 ────────
    const base = num(props['render_min_height']) ?? 0;
    return { base, height: base + 4 }; // 4 m default so something is visible
}

/** Clear the blue highlight overlay. */
function clearFeatureHighlight() {
    const src = state.map.getSource('feature-highlight');
    if (src) src.setData({ type: 'FeatureCollection', features: [] });
}

/**
 * Render the feature's properties in the floating info panel.
 * @param {Object} feature - GeoJSON feature from queryRenderedFeatures()
 */
function displayFeatureInfo(feature) {
    const props    = feature.properties || {};
    const geomType = feature.geometry.type;

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

            html += `
                <div class="feature-property">
                    <span class="property-key">${formatPropertyKey(k)}</span>
                    <span class="property-value">${formatted}</span>
                </div>`;
        });
    } else {
        html += '<p class="info-hint" style="margin-top:8px">No properties available</p>';
    }

    updateFeatureInfo(html);
}

/**
 * Set the HTML content of the info panel body.
 * @param {string} html - HTML string to insert
 */
function updateFeatureInfo(html) {
    const el = document.getElementById('feature-info-content');
    if (el) el.innerHTML = html;
}

/** Reset the floating panel to its default top-right corner position. */
function resetInfoPanelPosition() {
    const panel = document.getElementById('feature-info-panel');
    if (!panel) return;
    panel.style.left      = 'auto';
    panel.style.right     = '1rem';
    panel.style.top       = '1rem';
    panel.style.transform = 'none';
}

/**
 * Make the info panel draggable by attaching mousedown/mousemove/mouseup
 * listeners to its header bar. Stored references allow removal if called again.
 */
function setupDraggablePanel() {
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
    let ox = 0, oy = 0;

    const dragStart = e => {
        if (e.target.closest('.close-btn')) return; // don't drag when clicking close
        dragging         = true;
        state.isDragging = false;
        const rect = panel.getBoundingClientRect();
        ox = e.clientX - rect.left;
        oy = e.clientY - rect.top;
        // Switch from right/top anchoring to left/top for free positioning.
        panel.style.right     = 'auto';
        panel.style.left      = rect.left + 'px';
        panel.style.top       = rect.top  + 'px';
        panel.style.transform = 'none';
        e.preventDefault();
    };

    const drag = e => {
        if (!dragging) return;
        state.isDragging = true;
        // Clamp within the viewport so the panel can't be dragged off-screen.
        const x = Math.max(0, Math.min(e.clientX - ox, window.innerWidth  - panel.offsetWidth));
        const y = Math.max(0, Math.min(e.clientY - oy, window.innerHeight - panel.offsetHeight));
        panel.style.left = x + 'px';
        panel.style.top  = y + 'px';
    };

    const dragEnd = () => {
        dragging = false;
        setTimeout(() => { state.isDragging = false; }, 50);
    };

    header.addEventListener('mousedown', dragStart);
    document.addEventListener('mousemove', drag);
    document.addEventListener('mouseup',   dragEnd);

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
            const btn     = document.createElement('button');
            btn.type      = 'button';
            btn.title     = s.name;
            btn.textContent = s.name;
            if (i === 0) btn.classList.add('active');

            btn.addEventListener('click', () => {
                this._container.querySelectorAll('button').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.currentStyleId = s.id;
                map.setStyle(s.url);

                // Explicit restoreActiveLayers call — no generic styledata listener
                // means this is the only path that rebuilds layers after a manual
                // style switch. (zoomToFeature handles its own BDF switch separately.)
                map.once('idle', () => {
                    map.easeTo({
                        center:   state.currentCenter,
                        zoom:     state.currentZoom, //keep the current zoom, 
                        pitch:    s.pitch   ?? state.currentPitch,
                        bearing:  s.bearing ?? state.currentBearing,
                        duration: 800,
                    });
                    map.once('idle', restoreActiveLayers);
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

// Expose symbols referenced by inline event handlers in index.html.
window.isoState       = state.iso;
window.isoHandleClick = isoHandleClick;